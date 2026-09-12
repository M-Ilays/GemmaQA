"""ScenarioPlanningEngine -- the orchestrator.

Wires together goal validation, candidate generation, requirement
resolution, step/checkpoint/evidence/comparison/branch planning,
feasibility/risk analysis, dependency/conflict/gap detection,
deduplication, and scoring into one pipeline that converts a
`GoalGenerationEngine`'s current goals + an `ApplicationKnowledgeGraph`
into a `ScenarioPlanningResult`. This is the ONLY class other GemmaQA code
(`RunMemory`, the controller) talks to.

`generate()` runs the full pipeline as a single idempotency-tracked pass,
exactly like `ApplicationKnowledgeGraph.synchronize()` and
`GoalGenerationEngine.generate()`: re-running it against an UNCHANGED goal
set must leave every scenario's `created_at`/`observation_count`
untouched, and `scenario_plan_version` must not increment on a no-op pass.
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import (
    InvestigationScenario,
    ScenarioAlternative,
    ScenarioGap,
    ScenarioPlan,
    ScenarioPlanningResult,
)
from app.intelligence.scenario_planning.scenario_actor_resolver import resolve_actor_requirements, resolve_permission_requirements
from app.intelligence.scenario_planning.scenario_branch_builder import build_branches
from app.intelligence.scenario_planning.scenario_candidate_builder import build_candidates
from app.intelligence.scenario_planning.scenario_comparison_planner import plan_comparisons
from app.intelligence.scenario_planning.scenario_conflict_detector import detect_conflicts, detect_duplicate_alternatives
from app.intelligence.scenario_planning.scenario_data_requirement_builder import (
    resolve_data_requirements,
    resolve_entity_requirements,
    resolve_output_requirements,
    resolve_state_requirements,
    resolve_workflow_requirements,
)
from app.intelligence.scenario_planning.scenario_decomposer import validate_and_load
from app.intelligence.scenario_planning.scenario_dependency_resolver import resolve_dependencies
from app.intelligence.scenario_planning.scenario_deduplicator import deduplicate_scenarios
from app.intelligence.scenario_planning.scenario_evidence_planner import plan_evidence
from app.intelligence.scenario_planning.scenario_feasibility_analyzer import assess_feasibility
from app.intelligence.scenario_planning.scenario_gap_analyzer import analyze_gaps
from app.intelligence.scenario_planning.scenario_memory import ScenarioMemory
from app.intelligence.scenario_planning.scenario_query_engine import ScenarioQueryEngine
from app.intelligence.scenario_planning.scenario_risk_analyzer import assess_risk
from app.intelligence.scenario_planning.scenario_scoring import estimate_complexity, estimate_counts, score_scenario
from app.intelligence.scenario_planning.scenario_serializer import ScenarioSerializer
from app.intelligence.scenario_planning.scenario_step_builder import StepBuildContext, build_preconditions_postconditions, build_steps, to_observational, to_reduced_scope
from app.utils.exploration_trace import record as trace_record

_STATUS_ORDER = ["feasible", "conditionally_feasible", "draft", "incomplete", "blocked"]


class ScenarioPlanningEngine:
    def __init__(self, memory: ScenarioMemory | None = None) -> None:
        self.memory = memory or ScenarioMemory()
        self.query_engine = ScenarioQueryEngine(self.memory)
        self.serializer = ScenarioSerializer(self.memory)

    def generate(self, goal_engine, graph, *, iteration: int = 0) -> ScenarioPlanningResult:
        self.memory.begin_pass()

        goals = list(goal_engine.query_engine.all_goals()) if goal_engine is not None else []
        goals_by_id = {g.goal_id: g for g in goals}

        all_scenarios: list[InvestigationScenario] = []
        all_gaps: list[ScenarioGap] = []
        all_conflicts = []
        all_alternatives: list[ScenarioAlternative] = []
        plans: list[ScenarioPlan] = []

        for goal in sorted(goals, key=lambda g: g.goal_id):
            validation = validate_and_load(goal, graph)
            candidates = build_candidates(validation)
            built = [self._build_one_scenario(candidate, goal, graph, validation) for candidate in candidates]
            raw_scenarios = [b[0] for b in built]

            duplicate_conflicts = detect_duplicate_alternatives(raw_scenarios)
            deduped, _removed_ids = deduplicate_scenarios(raw_scenarios)
            deduped_ids = {s.scenario_id for s in deduped}
            kept = [b for b in built if b[0].scenario_id in deduped_ids]

            primary = next((s for s in deduped if s.scenario_id.endswith(":primary")), deduped[0] if deduped else None)
            sibling_ids = sorted(s.scenario_id for s in deduped)
            for scenario in deduped:
                scenario.alternatives = [sid for sid in sibling_ids if sid != scenario.scenario_id]
            if primary is not None:
                for scenario in deduped:
                    if scenario.scenario_id == primary.scenario_id:
                        continue
                    all_alternatives.append(
                        ScenarioAlternative(
                            alternative_id=f"alt:{primary.scenario_id}<->{scenario.scenario_id}", scenario_id=primary.scenario_id,
                            alternative_scenario_id=scenario.scenario_id, differentiation=_differentiation(primary, scenario),
                            rationale=scenario.description,
                        )
                    )

            all_scenarios.extend(s for s, _, _ in kept)
            all_gaps.extend(gap for _, gaps, _ in kept for gap in gaps)
            all_conflicts.extend(conflict for _, _, conflicts in kept for conflict in conflicts)
            all_conflicts.extend(duplicate_conflicts)

            plans.append(
                ScenarioPlan(
                    plan_id=f"plan:{goal.goal_id}", goal_id=goal.goal_id,
                    primary_scenario_id=primary.scenario_id if primary else "",
                    alternative_scenario_ids=[sid for sid in sibling_ids if not primary or sid != primary.scenario_id],
                    scenario_ids=sibling_ids, status=_aggregate_status(deduped),
                )
            )

        dependencies = resolve_dependencies(all_scenarios, goals_by_id)

        still_present_ids: set[str] = set()
        for scenario in sorted(all_scenarios, key=lambda s: s.scenario_id):
            stored = self.memory.upsert_scenario(scenario)
            still_present_ids.add(stored.scenario_id)
        self.memory.remove_scenarios_not_in(still_present_ids)
        self.memory.replace_plans(plans)
        self.memory.replace_dependencies(dependencies)
        self.memory.replace_conflicts(all_conflicts)
        self.memory.replace_gaps(all_gaps)
        self.memory.replace_alternatives(all_alternatives)

        self.memory.last_graph_version = graph.memory.graph_version if graph is not None else 0
        self.memory.last_goal_generation_count = getattr(goal_engine.memory, "generation_count", 0) if goal_engine is not None else 0

        version_bumped = self.memory.end_pass()

        statistics = self.query_engine.scenario_statistics()
        result = ScenarioPlanningResult(
            scenarios=self.query_engine.all_scenarios(), plans=list(self.memory.plans.values()),
            dependencies=self.query_engine.scenario_dependencies(), conflicts=self.query_engine.scenario_conflicts(),
            gaps=self.query_engine.scenario_gaps(), alternatives=list(self.memory.alternatives.values()),
            statistics=statistics, graph_version=self.memory.last_graph_version, scenario_plan_version=self.memory.scenario_plan_version,
        )
        trace_record(
            "scenario_planning.generation", scenario_plan_version=self.memory.scenario_plan_version, version_bumped=version_bumped,
            total_scenarios=statistics.total_scenarios, feasible_count=statistics.scenarios_by_feasibility.get("feasible", 0),
            blocked_count=statistics.scenarios_by_feasibility.get("blocked", 0), gap_count=statistics.gap_count, conflict_count=statistics.conflict_count,
        )
        return result

    # -- thin pass-throughs --------------------------------------------------------

    def statistics(self):
        return self.query_engine.scenario_statistics()

    def _build_one_scenario(self, candidate, goal, graph, validation) -> tuple[InvestigationScenario, list[ScenarioGap], list]:
        if not validation.can_plan:
            scenario = InvestigationScenario(
                scenario_id=candidate.candidate_id, goal_id=goal.goal_id, scenario_type="unknown",
                title=candidate.title, description=candidate.objective, objective=candidate.objective,
                status="incomplete", feasibility_status="incomplete", confidence=0.0, priority_hint=0.0,
                graph_version=graph.memory.graph_version if graph is not None else 0,
                source_goal_type=goal.goal_type, source_goal_priority=goal.priority_score,
            )
            gap = ScenarioGap(
                gap_id=f"{scenario.scenario_id}:gap:insufficient_information:0", scenario_id=scenario.scenario_id,
                gap_type="insufficient_information", description=candidate.rationale, blocking=True, severity="high",
                source_goal_id=goal.goal_id,
            )
            scenario.gaps = [gap.gap_id]
            return scenario, [gap], []

        actor_reqs = resolve_actor_requirements(goal, graph, scenario_type=candidate.scenario_type)
        permission_reqs = resolve_permission_requirements(goal, graph, scenario_type=candidate.scenario_type)
        entity_reqs = resolve_entity_requirements(goal, graph)
        state_reqs = resolve_state_requirements(goal, graph)
        workflow_reqs = resolve_workflow_requirements(goal, graph)
        output_reqs = resolve_output_requirements(goal, graph)
        data_reqs = resolve_data_requirements(goal, mutating=(candidate.mutation_level == "mutating"))

        ctx = StepBuildContext(
            candidate=candidate, goal=goal, graph=graph, actor_reqs=actor_reqs, permission_reqs=permission_reqs,
            entity_reqs=entity_reqs, state_reqs=state_reqs, workflow_reqs=workflow_reqs, output_reqs=output_reqs, data_reqs=data_reqs,
        )

        steps, checkpoints = build_steps(ctx)
        if candidate.variant == "observational":
            steps = to_observational(steps)
        elif candidate.variant == "reduced_scope":
            steps = to_reduced_scope(steps)
        evidence_requirements = plan_evidence(ctx, steps, checkpoints)
        comparisons = plan_comparisons(ctx, steps, checkpoints)
        branches = build_branches(ctx, steps)
        preconditions, postconditions = build_preconditions_postconditions(ctx, mutating=(candidate.mutation_level == "mutating"))

        feasibility = assess_feasibility(ctx, validation)
        risk, cleanup_plan, rollback_plan = assess_risk(ctx, steps, comparisons)
        conflicts = detect_conflicts(ctx, validation, steps, cleanup_plan)

        all_requirements = [*actor_reqs, *permission_reqs, *entity_reqs, *state_reqs, *workflow_reqs, *output_reqs, *data_reqs]
        confidence = (sum(r.confidence for r in all_requirements) / len(all_requirements)) if all_requirements else 0.3

        counts = estimate_counts(steps)
        complexity_score, complexity_class = estimate_complexity(
            steps=steps, actor_requirements=actor_reqs, branches=branches, comparisons=comparisons,
            data_requirements=data_reqs, cleanup_plan=cleanup_plan,
        )
        scores = score_scenario(
            scenario_confidence=confidence, goal_priority_score=goal.priority_score, evidence_requirements=evidence_requirements,
            feasibility_status=feasibility.feasibility_status, branches=branches, steps=steps, output_requirements=output_reqs,
        )

        source_nodes = sorted({n for r in all_requirements for n in r.supporting_graph_nodes} | set(goal.supporting_graph_nodes))
        source_edges = sorted(set(goal.supporting_graph_edges))
        status = _status_for(feasibility.feasibility_status, risk.risk_class)

        scenario = InvestigationScenario(
            scenario_id=candidate.candidate_id, goal_id=goal.goal_id, scenario_type=candidate.scenario_type,
            title=candidate.title, description=candidate.objective, objective=candidate.objective,
            status=status, feasibility_status=feasibility.feasibility_status, confidence=confidence,
            priority_hint=scores["priority_hint"], graph_version=graph.memory.graph_version if graph is not None else 0,
            source_goal_type=goal.goal_type, source_goal_priority=goal.priority_score,
            actor_requirements=actor_reqs, permission_requirements=permission_reqs, entity_requirements=entity_reqs,
            state_requirements=state_reqs, workflow_requirements=workflow_reqs, output_requirements=output_reqs,
            data_requirements=data_reqs, preconditions=preconditions, steps=steps, branches=branches,
            checkpoints=checkpoints, evidence_requirements=evidence_requirements, comparisons=comparisons,
            postconditions=postconditions, cleanup_plan=cleanup_plan, rollback_plan=rollback_plan,
            risk_assessment=risk, feasibility_assessment=feasibility,
            estimated_runtime_class=complexity_class, complexity_score=complexity_score, risk_score=risk.final_risk_score,
            information_gain_score=scores["information_gain_score"], confidence_gain_estimate=scores["confidence_gain_estimate"],
            reversibility_score=scores["reversibility_score"], determinism_score=scores["determinism_score"],
            source_graph_nodes=source_nodes, source_graph_edges=source_edges,
            **counts,
        )

        gaps = analyze_gaps(
            ctx, validation, steps=steps, comparisons=comparisons, evidence_requirements=evidence_requirements,
            cleanup_plan=cleanup_plan, risk_assessment=risk,
        )
        scenario.gaps = [g.gap_id for g in gaps]
        scenario.conflicts = [c.conflict_id for c in conflicts]

        return scenario, gaps, conflicts


def _status_for(feasibility_status: str, risk_class: str) -> str:
    if risk_class == "prohibited":
        return "blocked"
    return {"feasible": "feasible", "conditionally_feasible": "conditionally_feasible", "blocked": "blocked", "incomplete": "incomplete"}.get(feasibility_status, "draft")


def _aggregate_status(scenarios: list) -> str:
    if not scenarios:
        return "incomplete"
    present = {s.status for s in scenarios}
    for status in _STATUS_ORDER:
        if status in present:
            return status
    return next(iter(present))


def _differentiation(primary, alt) -> list[str]:
    diffs = []
    if abs(primary.risk_score - alt.risk_score) > 1e-9:
        diffs.append("risk")
    if len(primary.actor_requirements) != len(alt.actor_requirements):
        diffs.append("actor_count")
    if any(s.mutation_type != "none" for s in primary.steps) != any(s.mutation_type != "none" for s in alt.steps):
        diffs.append("mutation_level")
    if abs(primary.complexity_score - alt.complexity_score) > 1e-9:
        diffs.append("complexity")
    if abs(primary.information_gain_score - alt.information_gain_score) > 1e-9:
        diffs.append("evidence_strength")
    return diffs or ["variant"]

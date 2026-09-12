"""GoalGenerationEngine -- the orchestrator.

Wires together candidate building, deduplication, priority scoring,
dependency resolution, explanation, and grouping into one pipeline that
converts an `ApplicationKnowledgeGraph` into a `GoalGenerationResult`. This
is the ONLY class other GemmaQA code (`RunMemory`, the controller) talks
to -- everything else in this package is an internal collaborator.

`generate()` runs the full pipeline as a single idempotency-tracked pass:
re-running it against an UNCHANGED Knowledge Graph must leave every goal's
`created_at`/`observation_count` untouched, exactly like
`ApplicationKnowledgeGraph.synchronize()` does for nodes/edges.
"""

from __future__ import annotations

from app.intelligence.goal_generation import goal_grouping, goal_priority
from app.intelligence.goal_generation.goal_candidate_builder import GoalCandidateBuilder
from app.intelligence.goal_generation.goal_deduplicator import deduplicate
from app.intelligence.goal_generation.goal_dependency_resolver import GoalDependencyResolver
from app.intelligence.goal_generation.goal_explainer import explain
from app.intelligence.goal_generation.goal_memory import GoalMemory
from app.intelligence.goal_generation.goal_query_engine import GoalQueryEngine
from app.intelligence.goal_generation.schemas import GoalGenerationResult, InvestigationGoal
from app.utils.exploration_trace import record as trace_record

NO_USEFUL_GOAL_PRIORITY_FLOOR = 0.05
LOW_VALUE_BUSINESS_THRESHOLD = 0.2
COVERAGE_TARGET = 0.85
CONFIDENCE_TARGET = 0.8


class GoalGenerationEngine:
    def __init__(self, memory: GoalMemory | None = None) -> None:
        self.memory = memory or GoalMemory()
        self.query_engine = GoalQueryEngine(self.memory)

    def generate(self, graph, *, iteration: int = 0) -> GoalGenerationResult:
        self.memory.begin_pass()

        builder = GoalCandidateBuilder(graph)
        candidates = builder.build()
        candidates, conflicts = deduplicate(candidates)

        max_degree = goal_priority.max_node_degree(graph.memory)
        primary_node_goal_ids = _build_primary_node_map(candidates)

        goals = [_build_goal(candidate, graph=graph, max_degree=max_degree) for candidate in candidates]
        goals_by_id = {g.goal_id: g for g in goals}

        resolver = GoalDependencyResolver(graph)
        dependencies = resolver.resolve(goals, primary_node_goal_ids=primary_node_goal_ids)
        deps_by_goal: dict[str, list] = {}
        for dep in dependencies:
            deps_by_goal.setdefault(dep.goal_id, []).append(dep)

        for goal in goals:
            unmet = [d for d in deps_by_goal.get(goal.goal_id, []) if d.depends_on_goal_id in goals_by_id]
            goal.depends_on_goal_ids = sorted({d.depends_on_goal_id for d in unmet})
            if unmet:
                goal.goal_status = "blocked"
            explanation, recommended_goal_type = explain(goal, dependencies_by_goal=deps_by_goal, goals_by_id=goals_by_id)
            goal.explanation = explanation
            goal.recommended_goal_type = recommended_goal_type

        groups = goal_grouping.build_groups(goals, graph=graph)
        for group in groups:
            for goal_id in group.goal_ids:
                if goals_by_id[goal_id].group_id is None:
                    goals_by_id[goal_id].group_id = group.group_id

        still_present_ids: set[str] = set()
        for goal in sorted(goals, key=lambda g: g.goal_id):
            stored = self.memory.upsert_goal(goal)
            still_present_ids.add(stored.goal_id)
        self.memory.remove_goals_not_in(still_present_ids)
        self.memory.replace_groups(groups)
        self.memory.replace_dependencies(dependencies)
        self.memory.replace_conflicts(conflicts)

        self.memory.last_graph_version = graph.memory.graph_version
        version_bumped = self.memory.end_pass()

        statistics = self.query_engine.goal_statistics()
        stop_conditions = _evaluate_stop_conditions(self.memory, statistics, graph)

        result = GoalGenerationResult(
            goals=self.query_engine.all_goals(), groups=self.query_engine.all_groups(),
            dependencies=sorted(self.memory.dependencies.values(), key=lambda d: d.dependency_id),
            conflicts=sorted(self.memory.conflicts.values(), key=lambda c: c.conflict_id),
            statistics=statistics, stop_conditions=stop_conditions, graph_version=graph.memory.graph_version,
            generation_count=self.memory.generation_count,
        )
        trace_record(
            "goal_generation.generation", generation_count=self.memory.generation_count, version_bumped=version_bumped,
            total_goals=statistics.total_goals, high_priority_count=statistics.high_priority_count,
            stop_conditions=stop_conditions,
        )
        return result

    # -- thin pass-throughs so callers rarely need `query_engine` directly --------

    def highest_priority_goals(self, limit: int = 20) -> list[InvestigationGoal]:
        return self.query_engine.highest_priority_goals(limit)

    def statistics(self):
        return self.query_engine.goal_statistics()


def _build_goal(candidate, *, graph, max_degree: int) -> InvestigationGoal:
    signals = goal_priority.derive_priority_signals(candidate, graph=graph, max_degree=max_degree)
    priority = goal_priority.compute_priority(candidate.subject_key, **signals, already_verified=candidate.already_verified)
    return InvestigationGoal(
        goal_id=candidate.subject_key, goal_type=candidate.goal_type, title=candidate.title, description=candidate.description,
        priority_score=priority.final_score, business_value=priority.business_value, risk_score=priority.risk,
        coverage_value=priority.coverage_improvement, confidence=candidate.source_confidence,
        required_entities=candidate.required_entities, required_actors=candidate.required_actors,
        required_workflows=candidate.required_workflows, required_outputs=candidate.required_outputs,
        required_permissions=candidate.required_permissions, required_states=candidate.required_states,
        required_context=candidate.required_context, blocking_gaps=sorted(set(candidate.blocking_gaps)),
        supporting_evidence=candidate.supporting_evidence, supporting_graph_nodes=candidate.supporting_graph_nodes,
        supporting_graph_edges=candidate.supporting_graph_edges, contradictions=candidate.contradictions,
        estimated_actor_count=candidate.estimated_actor_count, estimated_workflow_depth=candidate.estimated_workflow_depth,
        estimated_browser_actions=candidate.estimated_browser_actions, estimated_complexity=_complexity_for(candidate),
        graph_version=graph.memory.graph_version, priority=priority, goal_status="pending",
        source_gap_ids=sorted(set(candidate.source_gap_ids)), source_contradiction_ids=sorted(set(candidate.source_contradiction_ids)),
        source_consistency_issue_ids=sorted(set(candidate.source_consistency_issue_ids)),
        source_inference_rule_ids=sorted(set(candidate.source_inference_rule_ids)), source_reference_ids=sorted(set(candidate.source_reference_ids)),
    )


def _complexity_for(candidate) -> str:
    score = candidate.estimated_workflow_depth + candidate.estimated_actor_count + candidate.estimated_browser_actions
    if score >= 8:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def _build_primary_node_map(candidates) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for candidate in candidates:
        for node_id in candidate.supporting_graph_nodes:
            if candidate.subject_key == f"{candidate.goal_type}:{node_id}":
                mapping[node_id] = candidate.subject_key
                break
    return mapping


def _evaluate_stop_conditions(memory: GoalMemory, statistics, graph) -> list[str]:
    conditions: list[str] = []
    active_goals = [g for g in memory.goals.values() if g.goal_status in {"pending", "blocked"}]

    if not any(g.priority_score > NO_USEFUL_GOAL_PRIORITY_FLOOR for g in active_goals):
        conditions.append("no_useful_goals_remain")
    if active_goals and all(g.business_value < LOW_VALUE_BUSINESS_THRESHOLD for g in active_goals):
        conditions.append("only_low_value_goals_remain")

    graph_stats = graph.statistics()
    if graph_stats.total_nodes > 0:
        nodes_with_open_gap = {n for gap in graph.memory.gaps.values() if gap.status == "open" for n in gap.node_ids}
        coverage = 1.0 - (len(nodes_with_open_gap) / graph_stats.total_nodes)
        if coverage >= COVERAGE_TARGET:
            conditions.append("coverage_target_reached")

    if memory.goals:
        avg_confidence = sum(g.confidence for g in memory.goals.values()) / len(memory.goals)
        if avg_confidence >= CONFIDENCE_TARGET:
            conditions.append("confidence_target_reached")

    if graph_stats.gap_count == 0 and graph_stats.consistency_issue_count == 0 and not graph.memory.contradictions:
        conditions.append("application_sufficiently_understood")

    return conditions

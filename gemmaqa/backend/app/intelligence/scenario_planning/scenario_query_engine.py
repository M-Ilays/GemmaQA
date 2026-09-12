"""Scenario query API -- the only way callers read scenario memory
directly. Mirrors `goal_query_engine.py`'s role: simple, bounded,
deterministic lookups, never a place for new reasoning.
"""

from __future__ import annotations

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.scenario_planning.schemas import ScenarioStatistics

_RISK_ORDER = {"read_only": 0, "low": 1, "moderate": 2, "high": 3, "prohibited": 4, "unknown": 5}


class ScenarioQueryEngine:
    def __init__(self, memory) -> None:
        self.memory = memory

    def scenario_by_id(self, scenario_id: str):
        return self.memory.scenarios.get(scenario_id)

    def all_scenarios(self) -> list:
        return sorted(self.memory.scenarios.values(), key=lambda s: s.scenario_id)

    def scenarios_by_status(self, status: str) -> list:
        return sorted((s for s in self.memory.scenarios.values() if s.status == status), key=lambda s: s.scenario_id)

    def scenarios_by_type(self, scenario_type: str) -> list:
        return sorted((s for s in self.memory.scenarios.values() if s.scenario_type == scenario_type), key=lambda s: s.scenario_id)

    def scenarios_by_goal(self, goal_id: str) -> list:
        return sorted((s for s in self.memory.scenarios.values() if s.goal_id == goal_id), key=lambda s: s.scenario_id)

    scenarios_for_goal = scenarios_by_goal

    def scenarios_by_feasibility(self, feasibility_status: str) -> list:
        return sorted((s for s in self.memory.scenarios.values() if s.feasibility_status == feasibility_status), key=lambda s: s.scenario_id)

    def scenarios_by_risk(self, risk_class: str) -> list:
        return sorted(
            (s for s in self.memory.scenarios.values() if s.risk_assessment is not None and s.risk_assessment.risk_class == risk_class),
            key=lambda s: s.scenario_id,
        )

    def scenarios_for_actor(self, actor_term: str) -> list:
        target = normalize_term(actor_term)
        return sorted(
            (s for s in self.memory.scenarios.values() if any(normalize_term(r.canonical_name) == target for r in s.actor_requirements)),
            key=lambda s: s.scenario_id,
        )

    def scenarios_for_entity(self, entity_term: str) -> list:
        target = normalize_term(entity_term)
        return sorted(
            (s for s in self.memory.scenarios.values() if any(normalize_term(r.canonical_name) == target for r in s.entity_requirements)),
            key=lambda s: s.scenario_id,
        )

    def scenarios_for_workflow(self, workflow_term: str) -> list:
        target = normalize_term(workflow_term)
        return sorted(
            (s for s in self.memory.scenarios.values() if any(normalize_term(r.canonical_name) == target for r in s.workflow_requirements)),
            key=lambda s: s.scenario_id,
        )

    def scenarios_for_output(self, output_term: str) -> list:
        return sorted(
            (s for s in self.memory.scenarios.values() if any(r.output_id == output_term or r.requirement_id == f"output:{output_term}" for r in s.output_requirements)),
            key=lambda s: s.scenario_id,
        )

    def scenarios_for_permission(self, permission_term: str) -> list:
        return sorted(
            (s for s in self.memory.scenarios.values() if any(r.requirement_id == f"permission:{permission_term}" for r in s.permission_requirements)),
            key=lambda s: s.scenario_id,
        )

    def scenarios_requiring_state(self) -> list:
        return sorted((s for s in self.memory.scenarios.values() if s.state_requirements), key=lambda s: s.scenario_id)

    def scenarios_requiring_actor_switch(self) -> list:
        return sorted((s for s in self.memory.scenarios.values() if any(step.step_type == "switch_actor" for step in s.steps)), key=lambda s: s.scenario_id)

    def scenarios_requiring_mutation(self) -> list:
        return sorted((s for s in self.memory.scenarios.values() if any(step.mutation_type != "none" for step in s.steps)), key=lambda s: s.scenario_id)

    def read_only_scenarios(self) -> list:
        return sorted((s for s in self.memory.scenarios.values() if all(step.mutation_type == "none" for step in s.steps)), key=lambda s: s.scenario_id)

    def blocked_scenarios(self) -> list:
        return self.scenarios_by_feasibility("blocked")

    def incomplete_scenarios(self) -> list:
        return self.scenarios_by_feasibility("incomplete")

    def feasible_scenarios(self) -> list:
        return self.scenarios_by_feasibility("feasible")

    def conditionally_feasible_scenarios(self) -> list:
        return self.scenarios_by_feasibility("conditionally_feasible")

    def high_risk_scenarios(self) -> list:
        return sorted(
            (s for s in self.memory.scenarios.values() if s.risk_assessment is not None and s.risk_assessment.risk_class in {"high", "prohibited"}),
            key=lambda s: s.scenario_id,
        )

    def scenarios_with_gaps(self) -> list:
        scenario_ids_with_gaps = {g.scenario_id for g in self.memory.gaps.values() if g.status == "open"}
        return sorted((s for s in self.memory.scenarios.values() if s.scenario_id in scenario_ids_with_gaps), key=lambda s: s.scenario_id)

    def scenarios_with_conflicts(self) -> list:
        scenario_ids_with_conflicts = {sid for c in self.memory.conflicts.values() if c.status == "open" for sid in c.scenario_ids}
        return sorted((s for s in self.memory.scenarios.values() if s.scenario_id in scenario_ids_with_conflicts), key=lambda s: s.scenario_id)

    def alternatives_for_scenario(self, scenario_id: str) -> list:
        scenario = self.memory.scenarios.get(scenario_id)
        if scenario is None:
            return []
        return sorted((self.memory.scenarios[sid] for sid in scenario.alternatives if sid in self.memory.scenarios), key=lambda s: s.scenario_id)

    def dependencies_for_scenario(self, scenario_id: str) -> list:
        return sorted((d for d in self.memory.dependencies.values() if d.scenario_id == scenario_id), key=lambda d: d.dependency_id)

    def prerequisites_for_scenario(self, scenario_id: str) -> list:
        """The scenarios THIS scenario depends on (its prerequisites)."""
        required_ids = {d.required_scenario_id for d in self.memory.dependencies.values() if d.scenario_id == scenario_id}
        return sorted((self.memory.scenarios[sid] for sid in required_ids if sid in self.memory.scenarios), key=lambda s: s.scenario_id)

    def highest_information_gain_scenarios(self, limit: int = 20) -> list:
        return sorted(self.memory.scenarios.values(), key=lambda s: (-s.information_gain_score, s.scenario_id))[:limit]

    def lowest_risk_scenarios(self, limit: int = 20) -> list:
        return sorted(
            self.memory.scenarios.values(),
            key=lambda s: (_RISK_ORDER.get(s.risk_assessment.risk_class if s.risk_assessment else "unknown", 5), s.scenario_id),
        )[:limit]

    def highest_confidence_gain_scenarios(self, limit: int = 20) -> list:
        return sorted(self.memory.scenarios.values(), key=lambda s: (-s.confidence_gain_estimate, s.scenario_id))[:limit]

    def scenario_gaps(self) -> list:
        return sorted((g for g in self.memory.gaps.values() if g.status == "open"), key=lambda g: g.gap_id)

    def scenario_conflicts(self) -> list:
        return sorted((c for c in self.memory.conflicts.values() if c.status == "open"), key=lambda c: c.conflict_id)

    def scenario_dependencies(self) -> list:
        return sorted(self.memory.dependencies.values(), key=lambda d: d.dependency_id)

    def scenario_statistics(self) -> ScenarioStatistics:
        scenarios = list(self.memory.scenarios.values())
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_feasibility: dict[str, int] = {}
        by_risk: dict[str, int] = {}
        read_only = mutating = cross_actor = high_risk = 0
        complexity_sum = confidence_gain_sum = 0.0

        for s in scenarios:
            by_type[s.scenario_type] = by_type.get(s.scenario_type, 0) + 1
            by_status[s.status] = by_status.get(s.status, 0) + 1
            by_feasibility[s.feasibility_status] = by_feasibility.get(s.feasibility_status, 0) + 1
            risk_class = s.risk_assessment.risk_class if s.risk_assessment else "unknown"
            by_risk[risk_class] = by_risk.get(risk_class, 0) + 1
            if all(step.mutation_type == "none" for step in s.steps):
                read_only += 1
            else:
                mutating += 1
            if len(s.actor_requirements) > 1:
                cross_actor += 1
            if risk_class in {"high", "prohibited"}:
                high_risk += 1
            complexity_sum += s.complexity_score
            confidence_gain_sum += s.confidence_gain_estimate

        count = len(scenarios) or 1
        return ScenarioStatistics(
            total_scenarios=len(scenarios), scenarios_by_type=by_type, scenarios_by_status=by_status,
            scenarios_by_feasibility=by_feasibility, scenarios_by_risk=by_risk,
            read_only_count=read_only, mutating_count=mutating, cross_actor_count=cross_actor, high_risk_count=high_risk,
            average_complexity_score=complexity_sum / count, average_confidence_gain=confidence_gain_sum / count,
            dependency_count=len(self.memory.dependencies), conflict_count=len(self.memory.conflicts),
            gap_count=len(self.memory.gaps), alternative_count=len(self.memory.alternatives),
            graph_version=self.memory.last_graph_version, scenario_plan_version=self.memory.scenario_plan_version,
        )

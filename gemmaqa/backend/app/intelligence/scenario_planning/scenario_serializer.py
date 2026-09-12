"""Safe scenario serialisation. Nothing in this package's schemas ever
carries a password/token/cookie/secret header/personal test data -- by
construction, not by redaction -- so serialisation only needs to stay
BOUNDED, never redact anything after the fact.
"""

from __future__ import annotations

from typing import Any


class ScenarioSerializer:
    def __init__(self, memory) -> None:
        self.memory = memory

    def compact_snapshot(self) -> dict[str, Any]:
        return {
            "scenario_plan_version": self.memory.scenario_plan_version,
            "scenario_count": len(self.memory.scenarios),
            "scenario_ids": sorted(self.memory.scenarios.keys()),
            "gap_count": len([g for g in self.memory.gaps.values() if g.status == "open"]),
            "conflict_count": len([c for c in self.memory.conflicts.values() if c.status == "open"]),
        }

    def full_snapshot(self, statistics: dict[str, Any]) -> dict[str, Any]:
        return {
            "scenario_plan_version": self.memory.scenario_plan_version,
            "statistics": statistics,
            "scenarios": [s.model_dump(mode="json") for s in self.memory.scenarios.values()],
            "gaps": [g.model_dump(mode="json") for g in self.memory.gaps.values() if g.status == "open"],
            "conflicts": [c.model_dump(mode="json") for c in self.memory.conflicts.values() if c.status == "open"],
        }

    def bounded_scenario_export(self, scenario_ids: list[str]) -> dict[str, Any]:
        return {"scenarios": [self.memory.scenarios[s].model_dump(mode="json") for s in scenario_ids if s in self.memory.scenarios]}

    def human_readable_summary(self, statistics: dict[str, Any]) -> str:
        lines = [
            f"Scenario Plan -- version {self.memory.scenario_plan_version}",
            f"  Scenarios: {statistics.get('total_scenarios', 0)} (read_only={statistics.get('read_only_count', 0)}, mutating={statistics.get('mutating_count', 0)})",
            f"  By feasibility: {statistics.get('scenarios_by_feasibility', {})}",
            f"  By risk: {statistics.get('scenarios_by_risk', {})}",
            f"  Gaps: {statistics.get('gap_count', 0)} | Conflicts: {statistics.get('conflict_count', 0)} | Dependencies: {statistics.get('dependency_count', 0)}",
        ]
        return "\n".join(lines)

    def executor_handoff_stub(self, scenario_id: str) -> dict[str, Any]:
        """A future-facing shape a later Autonomous Investigation engine
        could translate into runtime planner constraints -- never executed
        or invoked from here."""
        scenario = self.memory.scenarios.get(scenario_id)
        if scenario is None:
            return {}
        return {
            "scenario_id": scenario.scenario_id, "goal_id": scenario.goal_id, "status": scenario.status,
            "steps": [
                {"step_id": s.step_id, "step_type": s.step_type, "semantic_action": s.semantic_action, "safety_class": s.safety_class}
                for s in scenario.steps
            ],
            "note": "This is a planning artifact, not an execution instruction. No browser action has been taken.",
        }

"""Scenario memory -- the raw store for `InvestigationScenario`/
`ScenarioPlan`/`ScenarioDependency`/`ScenarioConflict`/`ScenarioGap`/
`ScenarioAlternative` records and their idempotent merge logic. Mirrors
`goal_generation/goal_memory.py`'s role: this is NOT a second application-
memory system, it only holds the OUTPUT of scenario planning.

Idempotency matters here exactly as it did for Goal Generation and the
Knowledge Graph: re-running `generate()` against an UNCHANGED goal set
must leave every scenario's `created_at`/`observation_count` untouched --
only genuinely changed content bumps them, and only a genuine content
change bumps `scenario_plan_version`.
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import ScenarioAlternative, ScenarioConflict, ScenarioDependency, ScenarioGap, ScenarioPlan, ScenarioVersion, InvestigationScenario

_CONTENT_EXCLUDE = {"created_at", "updated_at", "observation_count"}


class ScenarioMemory:
    def __init__(self) -> None:
        self.scenarios: dict[str, InvestigationScenario] = {}
        self.plans: dict[str, ScenarioPlan] = {}
        self.dependencies: dict[str, ScenarioDependency] = {}
        self.conflicts: dict[str, ScenarioConflict] = {}
        self.gaps: dict[str, ScenarioGap] = {}
        self.alternatives: dict[str, ScenarioAlternative] = {}
        self.scenario_plan_version: int = 0
        self.last_graph_version: int = 0
        self.last_goal_generation_count: int = 0
        self.version_history: list[ScenarioVersion] = []

        self.dirty_this_pass: bool = False
        self.added_scenario_ids: list[str] = []
        self.updated_scenario_ids: list[str] = []
        self.stale_scenario_ids: list[str] = []
        self.added_gap_ids: list[str] = []
        self.resolved_gap_ids: list[str] = []
        self.added_conflict_ids: list[str] = []
        self.resolved_conflict_ids: list[str] = []

    def begin_pass(self) -> None:
        self.dirty_this_pass = False
        self.added_scenario_ids = []
        self.updated_scenario_ids = []
        self.stale_scenario_ids = []
        self.added_gap_ids = []
        self.resolved_gap_ids = []
        self.added_conflict_ids = []
        self.resolved_conflict_ids = []

    def end_pass(self) -> bool:
        if self.dirty_this_pass:
            self.scenario_plan_version += 1
        return self.dirty_this_pass

    # -- scenarios ------------------------------------------------------------

    def upsert_scenario(self, scenario: InvestigationScenario) -> InvestigationScenario:
        existing = self.scenarios.get(scenario.scenario_id)
        if existing is None:
            self.scenarios[scenario.scenario_id] = scenario
            self.added_scenario_ids.append(scenario.scenario_id)
            self.dirty_this_pass = True
            return scenario

        if _content_differs(existing, scenario):
            scenario.created_at = existing.created_at
            scenario.observation_count = existing.observation_count + 1
            self.scenarios[scenario.scenario_id] = scenario
            self.updated_scenario_ids.append(scenario.scenario_id)
            self.dirty_this_pass = True
            return scenario
        return existing

    def remove_scenarios_not_in(self, still_present_ids: set[str]) -> None:
        for scenario_id in list(self.scenarios.keys()):
            if scenario_id not in still_present_ids:
                del self.scenarios[scenario_id]
                self.stale_scenario_ids.append(scenario_id)
                self.dirty_this_pass = True

    # -- plans / dependencies / conflicts / gaps / alternatives (recomputed
    #    each pass; deterministic ids make wholesale replacement itself
    #    idempotent -- only genuinely new/removed keys mark the pass dirty) --

    def replace_plans(self, plans: list[ScenarioPlan]) -> None:
        new_plans = {p.plan_id: p for p in plans}
        if set(new_plans) != set(self.plans):
            self.dirty_this_pass = True
        self.plans = new_plans

    def replace_dependencies(self, dependencies: list[ScenarioDependency]) -> None:
        new_deps = {d.dependency_id: d for d in dependencies}
        if set(new_deps) != set(self.dependencies):
            self.dirty_this_pass = True
        self.dependencies = new_deps

    def replace_conflicts(self, conflicts: list[ScenarioConflict]) -> None:
        new_conflicts = {c.conflict_id: c for c in conflicts}
        added = set(new_conflicts) - set(self.conflicts)
        resolved = set(self.conflicts) - set(new_conflicts)
        if added or resolved:
            self.dirty_this_pass = True
        self.added_conflict_ids.extend(sorted(added))
        self.resolved_conflict_ids.extend(sorted(resolved))
        self.conflicts = new_conflicts

    def replace_gaps(self, gaps: list[ScenarioGap]) -> None:
        new_gaps = {g.gap_id: g for g in gaps}
        added = set(new_gaps) - set(self.gaps)
        resolved = set(self.gaps) - set(new_gaps)
        if added or resolved:
            self.dirty_this_pass = True
        self.added_gap_ids.extend(sorted(added))
        self.resolved_gap_ids.extend(sorted(resolved))
        self.gaps = new_gaps

    def replace_alternatives(self, alternatives: list[ScenarioAlternative]) -> None:
        new_alts = {a.alternative_id: a for a in alternatives}
        if set(new_alts) != set(self.alternatives):
            self.dirty_this_pass = True
        self.alternatives = new_alts

    def dependencies_for_scenario(self, scenario_id: str) -> list[ScenarioDependency]:
        return [d for d in self.dependencies.values() if d.scenario_id == scenario_id]

    def gaps_for_scenario(self, scenario_id: str) -> list[ScenarioGap]:
        return [g for g in self.gaps.values() if g.scenario_id == scenario_id]


def _content_differs(existing: InvestigationScenario, incoming: InvestigationScenario) -> bool:
    a = existing.model_dump(exclude=_CONTENT_EXCLUDE)
    b = incoming.model_dump(exclude=_CONTENT_EXCLUDE)
    return a != b

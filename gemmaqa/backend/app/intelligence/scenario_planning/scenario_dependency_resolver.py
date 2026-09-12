"""Projects GOAL-level dependencies (from Goal Generation's own
`depends_on_goal_ids`) onto SCENARIO-level dependencies. This is the only
dependency type with a genuine, non-fabricated signal behind it: "verify
KPI depends on verify workflow" exists because the underlying goals
already carry that relationship -- this resolver never invents a
dependency the goal graph doesn't support. Operates across ALL scenarios
in a planning pass (needs cross-scenario visibility), never executes
anything.
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import ScenarioDependency


def _primary_scenario_ids(scenarios: list) -> dict[str, str]:
    by_goal: dict[str, list[str]] = {}
    for scenario in scenarios:
        by_goal.setdefault(scenario.goal_id, []).append(scenario.scenario_id)
    result: dict[str, str] = {}
    for goal_id, ids in by_goal.items():
        ids = sorted(ids)
        result[goal_id] = next((i for i in ids if i.endswith(":primary")), ids[0])
    return result


def resolve_dependencies(scenarios: list, goals_by_id: dict) -> list[ScenarioDependency]:
    primary_for_goal = _primary_scenario_ids(scenarios)
    dependencies: list[ScenarioDependency] = []
    seen: set[tuple[str, str]] = set()

    for scenario in scenarios:
        goal = goals_by_id.get(scenario.goal_id)
        if goal is None:
            continue
        for dep_goal_id in goal.depends_on_goal_ids:
            required_scenario_id = primary_for_goal.get(dep_goal_id)
            if required_scenario_id is None or required_scenario_id == scenario.scenario_id:
                continue
            key = (scenario.scenario_id, required_scenario_id)
            if key in seen:
                continue
            seen.add(key)
            dependencies.append(
                ScenarioDependency(
                    dependency_id=f"dep:{scenario.scenario_id}->{required_scenario_id}",
                    scenario_id=scenario.scenario_id, required_scenario_id=required_scenario_id,
                    dependency_type="goal_dependency_projection",
                    reason=f"Goal '{scenario.goal_id}' depends on goal '{dep_goal_id}'.",
                    blocking=True, confidence=0.6, source_goal_dependency=dep_goal_id,
                )
            )

    return sorted(dependencies, key=lambda d: (d.scenario_id, d.required_scenario_id))

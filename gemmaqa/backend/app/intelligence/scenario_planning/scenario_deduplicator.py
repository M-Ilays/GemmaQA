"""Deduplicates scenarios by SEMANTIC signature (goal, scenario type,
actor sequence, source graph nodes, mutation strategy) -- never by title
alone. Two scenarios that differ in actor, scope, permission polarity,
mutation-vs-observation, branch, output, or transition are NEVER merged
(task: "Do not merge scenarios that differ materially in..."). This is
the ACTING counterpart to `scenario_conflict_detector.detect_duplicate_
alternatives`, which only DETECTS and retains the finding.
"""

from __future__ import annotations


def _signature(scenario) -> tuple:
    return (
        scenario.goal_id, scenario.scenario_type,
        tuple(sorted(r.actor_id for r in scenario.actor_requirements)),
        tuple(sorted(scenario.source_graph_nodes)),
        tuple(s.mutation_type for s in scenario.steps),
    )


def _sort_key(scenario) -> tuple:
    # Prefer keeping the ":primary" variant when a signature collision
    # occurs -- predictable, rather than whichever variant happens to sort
    # first alphabetically (e.g. "...:dup" < "...:primary").
    return (0 if scenario.scenario_id.endswith(":primary") else 1, scenario.scenario_id)


def deduplicate_scenarios(scenarios: list) -> tuple[list, list[str]]:
    seen: dict[tuple, object] = {}
    removed: list[str] = []
    for scenario in sorted(scenarios, key=_sort_key):
        signature = _signature(scenario)
        if signature in seen:
            removed.append(scenario.scenario_id)
            continue
        seen[signature] = scenario
    return list(seen.values()), sorted(removed)

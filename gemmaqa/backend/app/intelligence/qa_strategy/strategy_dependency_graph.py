"""Projects Scenario Planning's own `ScenarioDependency`/`ScenarioConflict`
records onto the strategy layer, substituting `candidate_id` for
`scenario_id`. Deliberately does NOT fabricate new cross-candidate
dependency or conflict types: Scenario Planning already worked out which
scenarios block which, and duplicating that reasoning here would risk
drifting out of sync with it -- see `strategy_candidate_builder.py`'s
module docstring for the same "never rediscover" principle.
"""

from __future__ import annotations

from app.intelligence.qa_strategy.schemas import ExecutionConflict, ExecutionDependency

_DEPENDENCY_TYPE_MAP = {
    "scenario_prerequisite": "scenario_prerequisite",
    "goal_dependency_projection": "goal_prerequisite",
    "actor_session_dependency": "actor_ordering",
    "data_preparation_dependency": "state_ordering",
    "unknown": "scenario_prerequisite",
}

_CONFLICT_TYPE_MAP = {
    "permission_contradiction": "actor_scope_conflict",
    "state_conflict": "resource_contention",
    "incompatible_scope": "batch_scope_conflict",
    "cleanup_permission_unavailable": "resource_contention",
    "mixed_mutation_readonly": "batch_scope_conflict",
    "branch_contradicts_workflow": "ordering_contradiction",
    "scope_mismatch": "batch_scope_conflict",
    "stale_reference": "unknown",
    "actor_result_incompatibility": "actor_scope_conflict",
    "delay_assumption_conflict": "ordering_contradiction",
    "transition_contradicted": "ordering_contradiction",
    "duplicate_alternative": "duplicate_candidate",
    "unknown": "unknown",
}


def build_dependencies(scenario_dependencies: list, known_candidate_ids: set[str]) -> list[ExecutionDependency]:
    projected: list[ExecutionDependency] = []
    for dep in scenario_dependencies:
        candidate_id = f"candidate:{dep.scenario_id}"
        required_candidate_id = f"candidate:{dep.required_scenario_id}"
        if candidate_id not in known_candidate_ids or required_candidate_id not in known_candidate_ids:
            continue
        projected.append(
            ExecutionDependency(
                dependency_id=f"exec-dependency:{dep.dependency_id}",
                candidate_id=candidate_id,
                required_candidate_id=required_candidate_id,
                dependency_type=_DEPENDENCY_TYPE_MAP.get(dep.dependency_type, "scenario_prerequisite"),
                reason=dep.reason,
                blocking=dep.blocking,
                confidence=dep.confidence,
            )
        )
    projected.sort(key=lambda d: d.dependency_id)
    return projected


def build_conflicts(scenario_conflicts: list, known_candidate_ids: set[str]) -> list[ExecutionConflict]:
    projected: list[ExecutionConflict] = []
    for conflict in scenario_conflicts:
        candidate_ids = sorted(
            f"candidate:{sid}" for sid in conflict.scenario_ids if f"candidate:{sid}" in known_candidate_ids
        )
        if len(candidate_ids) < 2:
            continue
        projected.append(
            ExecutionConflict(
                conflict_id=f"exec-conflict:{conflict.conflict_id}",
                candidate_ids=candidate_ids,
                conflict_type=_CONFLICT_TYPE_MAP.get(conflict.conflict_type, "unknown"),
                description=conflict.description,
                severity=conflict.severity,
                status=conflict.status,
            )
        )
    projected.sort(key=lambda c: c.conflict_id)
    return projected

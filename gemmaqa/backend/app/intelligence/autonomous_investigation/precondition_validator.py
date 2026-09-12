"""Revalidates a scenario's already-resolved requirements against the
CURRENT run state, right before attempting to execute it.

Scenario Planning already resolved every requirement's `.status` once, at
planning time, by looking the referenced actor/entity/workflow up in the
Knowledge Graph (`scenario_data_requirement_builder._find_node`, via
`graph.query_engine.find_nodes_by_canonical_name`) -- never the raw
Entity/Actor/Workflow registries directly. This module re-checks against
the SAME source for the SAME reason: the graph is the synchronized
projection every reasoning engine already trusts, and checking a
different, stricter source here (e.g. a registry's own `known_*()`
filter, which applies its own confidence/status thresholds) would
silently reject scenarios Scenario Planning itself already accepted.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import PreconditionCheckResult

_BLOCKING_REQUIREMENT_STATUSES = frozenset({"contradicted", "blocked"})
_DEFERRABLE_REQUIREMENT_STATUSES = frozenset({"unresolved", "unavailable", "stale", "unknown"})


def _node_known(graph, canonical_name: str, node_type: str) -> bool:
    if not canonical_name or graph is None:
        return False
    return any(n.node_type == node_type for n in graph.query_engine.find_nodes_by_canonical_name(canonical_name))


def check_preconditions(scenario, memory) -> PreconditionCheckResult:
    blocking: list[str] = []
    deferred: list[str] = []
    checked_types: list[str] = []

    graph = getattr(memory, "knowledge_graph", None)
    authenticated = bool(getattr(memory, "auth_strategy", None) and memory.auth_strategy.authenticated)

    for req in scenario.actor_requirements:
        checked_types.append("actor")
        if req.status in _BLOCKING_REQUIREMENT_STATUSES:
            blocking.append(f"actor requirement '{req.canonical_name}' is {req.status}")
            continue
        if req.canonical_name and not _node_known(graph, req.canonical_name, "actor"):
            deferred.append(f"actor '{req.canonical_name}' not yet known this run")
            continue
        if req.session_required and not authenticated:
            deferred.append(f"actor '{req.canonical_name}' requires an authenticated session")

    for req in scenario.entity_requirements:
        checked_types.append("entity")
        if req.status in _BLOCKING_REQUIREMENT_STATUSES:
            blocking.append(f"entity requirement '{req.canonical_name}' is {req.status}")
            continue
        if req.canonical_name and not _node_known(graph, req.canonical_name, "entity"):
            deferred.append(f"entity '{req.canonical_name}' not yet known this run")

    for req in scenario.workflow_requirements:
        checked_types.append("workflow")
        if req.status in _BLOCKING_REQUIREMENT_STATUSES:
            blocking.append(f"workflow requirement '{req.canonical_name}' is {req.status}")
            continue
        if req.canonical_name and not _node_known(graph, req.canonical_name, "workflow"):
            deferred.append(f"workflow '{req.canonical_name}' not yet known this run")

    for req in scenario.permission_requirements:
        checked_types.append("permission")
        if req.status in _BLOCKING_REQUIREMENT_STATUSES:
            blocking.append(f"permission requirement '{req.permission_id}' is {req.status}")
        elif req.status in _DEFERRABLE_REQUIREMENT_STATUSES:
            deferred.append(f"permission '{req.permission_id}' is {req.status}")

    for req in scenario.data_requirements:
        checked_types.append("data")
        if req.generation_policy == "unknown":
            deferred.append("data requirement has no resolvable generation policy yet")

    for req in scenario.state_requirements:
        checked_types.append("state")
        if req.status in _BLOCKING_REQUIREMENT_STATUSES:
            blocking.append(f"state requirement '{req.state_label}' is {req.status}")

    if scenario.feasibility_assessment and scenario.feasibility_assessment.feasibility_status == "blocked":
        blocking.extend(scenario.feasibility_assessment.blocking_reasons)

    satisfied = not blocking and not deferred
    return PreconditionCheckResult(
        satisfied=satisfied, deferred=bool(deferred) and not blocking,
        blocking_reasons=blocking, deferred_reasons=deferred, checked_requirement_types=sorted(set(checked_types)),
    )

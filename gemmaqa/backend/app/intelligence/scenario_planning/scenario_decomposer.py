"""Goal validation + graph context retrieval -- the FIRST two pipeline
stages (task section "SCENARIO GENERATION PIPELINE"). Nothing downstream
should re-derive whether a goal is plannable; that judgment is made once,
here, and carried forward as a `GoalValidation`.

This module never mutates the goal or the graph -- it only reads them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GoalValidation:
    goal: object
    graph: object
    exists: bool = True
    can_plan: bool = True
    stale_graph_context: bool = False
    missing_node_ids: list[str] = field(default_factory=list)
    contradiction_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    unresolved_dependency_goal_ids: list[str] = field(default_factory=list)
    already_verified: bool = False
    has_context: bool = True
    warnings: list[str] = field(default_factory=list)


def validate_and_load(goal, graph) -> GoalValidation:
    """`goal`: an `InvestigationGoal`. `graph`: an `ApplicationKnowledgeGraph`
    (may be `None` if no Knowledge Graph is attached to this run -- callers
    must handle that by producing an incomplete scenario, never crashing)."""
    if goal is None:
        return GoalValidation(goal=goal, graph=graph, exists=False, can_plan=False, has_context=False, warnings=["goal does not exist"])

    validation = GoalValidation(goal=goal, graph=graph)

    if graph is None:
        validation.has_context = False
        validation.warnings.append("no knowledge graph attached to this run")
        return validation

    memory = graph.memory

    # -- graph version compatibility / staleness ---------------------------
    # Deliberately NOT `if goal.graph_version and ...`: graph_version=0 is a
    # legitimate value (the very first synchronise() pass), not a sentinel
    # for "unset" -- a truthy-guard here would silently skip staleness
    # detection for any goal generated during that first pass.
    if goal.graph_version < memory.graph_version:
        validation.stale_graph_context = True
        validation.warnings.append(f"goal was generated against graph_version={goal.graph_version}, current is {memory.graph_version}")

    # -- required graph nodes still present --------------------------------
    for node_id in goal.supporting_graph_nodes:
        node = memory.nodes.get(node_id)
        if node is None or node.stale:
            validation.missing_node_ids.append(node_id)

    if goal.supporting_graph_nodes and len(validation.missing_node_ids) == len(goal.supporting_graph_nodes):
        # every single supporting node is gone -- nothing left to plan against
        validation.can_plan = False
        validation.warnings.append("all supporting graph nodes are missing or stale")
    elif not goal.supporting_graph_nodes and not goal.supporting_evidence:
        validation.has_context = False
        validation.warnings.append("goal carries no supporting graph nodes or evidence")

    # -- contradictions -----------------------------------------------------
    validation.contradiction_ids = list(goal.contradictions)

    # -- blocked / unresolved dependencies ------------------------------------
    validation.blocked = goal.goal_status == "blocked"
    validation.unresolved_dependency_goal_ids = list(goal.depends_on_goal_ids)

    # -- already verified (forward-compatible: nothing sets node status to
    #    "verified" today, but if a future engine does, planning should
    #    know rather than blindly re-plan already-confirmed knowledge) -----
    if goal.supporting_graph_nodes:
        statuses = [memory.nodes[n].status for n in goal.supporting_graph_nodes if n in memory.nodes]
        validation.already_verified = bool(statuses) and all(s == "verified" for s in statuses) and not validation.contradiction_ids

    return validation


def context_for_goal(goal, graph, *, depth: int = 1, max_neighbours: int = 20) -> dict:
    """A bounded context bundle around the goal's primary subject, reusing
    the Knowledge Graph's own context projection -- never re-deriving it."""
    if graph is None or not goal.supporting_graph_nodes:
        return {}
    focus_node_id = goal.supporting_graph_nodes[0]
    try:
        return graph.context_for_node(focus_node_id, depth=depth, max_neighbours=max_neighbours) or {}
    except Exception:
        return {}

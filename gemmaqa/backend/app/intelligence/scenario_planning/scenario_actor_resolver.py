"""Resolves `ScenarioActorRequirement`/`ScenarioPermissionRequirement`
records from the goal + graph context. Never assumes credentials or a
live session exist -- the ONE actor this codebase's exploration run is
already authenticated as ("current session", the dominant actor concept
produced by Actor Discovery) is the only actor ever treated as currently
available; any additional actor (cross-role/hand-off scenarios) is
explicitly marked unknown-availability, which downstream feasibility
analysis treats as a conditional-feasibility reason, never a fabricated
"yes"."""

from __future__ import annotations

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.scenario_planning.schemas import ScenarioActorRequirement, ScenarioEvidenceReference, ScenarioPermissionRequirement

_CURRENT_SESSION_MARKERS = {"current session", "current user", "session actor", "authenticated actor"}


def _is_current_session(canonical_name: str) -> bool:
    return normalize_term(canonical_name) in {normalize_term(m) for m in _CURRENT_SESSION_MARKERS}


def _find_node(graph, canonical_name: str, node_type: str):
    if graph is None:
        return None
    matches = [n for n in graph.query_engine.find_nodes_by_canonical_name(canonical_name) if n.node_type == node_type]
    return matches[0] if matches else None


def _derive_actor_terms_from_graph(goal, graph) -> list[str]:
    """Fallback for goals whose `required_actors` was never populated --
    e.g. a `validate_actor_hand_off` goal is built from a standalone
    `unresolved_actor_hand_off` GAP (subject = a workflow_step node, not a
    node type Goal Generation's Step-1 pass populates `required_actors`
    for). Rather than silently planning around only one actor, walk from
    the goal's supporting graph nodes to their owning workflow and collect
    every actor that actually performs a step in it -- real graph
    structure, never fabricated."""
    if graph is None:
        return []
    actor_names: set[str] = set()
    for node_id in goal.supporting_graph_nodes:
        node = graph.memory.nodes.get(node_id)
        if node is None:
            continue
        workflow_node_id = None
        if node.node_type == "workflow":
            workflow_node_id = node.node_id
        elif node.node_type == "workflow_step":
            for e_id in graph.memory.incoming_edge_ids(node.node_id):
                edge = graph.memory.edges.get(e_id)
                if edge is not None and not edge.stale and edge.edge_type == "has_step":
                    workflow_node_id = edge.source_node_id
                    break
        if workflow_node_id is None:
            continue
        try:
            steps = graph.query_engine.workflow_steps(workflow_node_id)
        except Exception:
            continue
        for step in steps:
            resolved_here = False
            for e_id in graph.memory.outgoing_edge_ids(step.node_id):
                edge = graph.memory.edges.get(e_id)
                if edge is not None and not edge.stale and edge.edge_type == "performed_by":
                    actor_node = graph.memory.nodes.get(edge.target_node_id)
                    if actor_node is not None:
                        actor_names.add(actor_node.canonical_name)
                        resolved_here = True
            if resolved_here:
                continue
            # The actor's Discovery record never confirmed this step's
            # performer, so no `performed_by` edge exists yet -- only a
            # pending reference. Surface the actor NAME the workflow step
            # already carries (real evidence, just not yet promoted to a
            # graph node) rather than silently dropping it.
            for ref in graph.memory.unresolved_references():
                if ref.source_node_id == step.node_id and ref.edge_type == "performed_by":
                    actor_names.add(ref.target_hint)
    return sorted(actor_names)


def resolve_actor_requirements(goal, graph, *, scenario_type: str) -> list[ScenarioActorRequirement]:
    requirements: list[ScenarioActorRequirement] = []
    actor_terms = list(goal.required_actors) or _derive_actor_terms_from_graph(goal, graph) or ["current session"]
    for actor_term in sorted(actor_terms):
        node = _find_node(graph, actor_term, "actor")
        req_id = f"actor:{actor_term}"
        is_current = _is_current_session(actor_term)

        positive_evidence: list[ScenarioEvidenceReference] = []
        negative_evidence: list[ScenarioEvidenceReference] = []
        required_permissions: list[str] = []
        unresolved_issues: list[str] = []

        if node is None:
            requirements.append(
                ScenarioActorRequirement(
                    requirement_id=req_id, actor_id="", canonical_name=actor_term, status="unresolved",
                    confidence=0.0, mandatory=True, resolved=False, missing_reason="actor not found in knowledge graph",
                    current_availability="unknown", unresolved_issues=["actor unknown to graph"],
                )
            )
            continue

        if graph is not None:
            for e_id in graph.memory.outgoing_edge_ids(node.node_id):
                edge = graph.memory.edges.get(e_id)
                if edge is None or edge.stale:
                    continue
                if edge.edge_type == "has_permission":
                    perm_node = graph.memory.nodes.get(edge.target_node_id)
                    if perm_node is not None:
                        required_permissions.append(perm_node.canonical_name)
                        positive_evidence.append(ScenarioEvidenceReference(reference_type="graph_edge", source_id=edge.edge_id, description=f"has_permission: {perm_node.canonical_name}", confidence=edge.confidence))
                elif edge.edge_type == "lacks_permission":
                    perm_node = graph.memory.nodes.get(edge.target_node_id)
                    if perm_node is not None:
                        negative_evidence.append(ScenarioEvidenceReference(reference_type="graph_edge", source_id=edge.edge_id, description=f"lacks_permission: {perm_node.canonical_name}", confidence=edge.confidence))

        current_availability = "available" if is_current else "unknown"
        if not is_current:
            unresolved_issues.append("session availability for this actor is not known")
        unresolved_issues.append("credentials are not known to this engine")

        requirements.append(
            ScenarioActorRequirement(
                requirement_id=req_id, actor_id=node.node_id, canonical_name=actor_term,
                status="satisfiable", confidence=node.confidence, mandatory=True, resolved=True,
                resolution_source="knowledge_graph", session_required=True,
                role_switch_required=not is_current, current_availability=current_availability,
                required_permissions=sorted(set(required_permissions)),
                positive_permission_evidence=positive_evidence, negative_permission_evidence=negative_evidence,
                unresolved_issues=unresolved_issues,
                supporting_graph_nodes=[node.node_id],
            )
        )
    return requirements


def resolve_permission_requirements(goal, graph, *, scenario_type: str) -> list[ScenarioPermissionRequirement]:
    requirements: list[ScenarioPermissionRequirement] = []
    denial_expected = scenario_type == "permission_negative_verification"
    permission_terms = list(goal.required_permissions)
    for permission_term in sorted(permission_terms):
        node = _find_node(graph, permission_term, "permission")
        req_id = f"permission:{permission_term}"
        if node is None:
            requirements.append(
                ScenarioPermissionRequirement(
                    requirement_id=req_id, permission_id="", status="unresolved", confidence=0.0,
                    mandatory=True, resolved=False, missing_reason="permission not found in knowledge graph",
                    denial_expected=denial_expected,
                )
            )
            continue

        positive, negative = [], []
        operation_id = ""
        if graph is not None:
            for e_id in graph.memory.incoming_edge_ids(node.node_id):
                edge = graph.memory.edges.get(e_id)
                if edge is None or edge.stale:
                    continue
                if edge.edge_type == "has_permission":
                    positive.append(ScenarioEvidenceReference(reference_type="graph_edge", source_id=edge.edge_id, description="has_permission", confidence=edge.confidence))
                elif edge.edge_type == "lacks_permission":
                    negative.append(ScenarioEvidenceReference(reference_type="graph_edge", source_id=edge.edge_id, description="lacks_permission", confidence=edge.confidence))
            for e_id in graph.memory.outgoing_edge_ids(node.node_id):
                edge = graph.memory.edges.get(e_id)
                if edge is not None and not edge.stale and edge.edge_type == "enables":
                    operation_id = edge.target_node_id
                    break

        status = "contradicted" if positive and negative else ("satisfied" if positive else ("blocked" if negative else "satisfiable"))
        requirements.append(
            ScenarioPermissionRequirement(
                requirement_id=req_id, permission_id=node.node_id, operation_id=operation_id,
                status=status, confidence=node.confidence, mandatory=True, resolved=True,
                resolution_source="knowledge_graph", denial_expected=denial_expected,
                positive_evidence=positive, negative_evidence=negative,
                supporting_graph_nodes=[node.node_id],
            )
        )
    return requirements

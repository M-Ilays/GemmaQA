"""Resolves entity/state/workflow/output/data requirements from the goal
+ graph context. Data requirements are represented SEMANTICALLY (task
section "DATA REQUIREMENTS") -- never a fabricated concrete value, and
never personal information or secrets. When the graph cannot confirm a
constraint, the requirement's status stays honestly "unknown"/
"satisfiable" rather than being asserted as satisfied.
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import (
    ScenarioDataRequirement,
    ScenarioEntityRequirement,
    ScenarioOutputRequirement,
    ScenarioStateRequirement,
    ScenarioWorkflowRequirement,
)

_OUTPUT_NODE_TYPES = {"derived_output", "counter", "badge", "chart", "report", "queue", "notification", "alert"}


def _find_node(graph, canonical_name: str, node_type):
    if graph is None:
        return None
    allowed = {node_type} if isinstance(node_type, str) else set(node_type)
    matches = [n for n in graph.query_engine.find_nodes_by_canonical_name(canonical_name) if n.node_type in allowed]
    return matches[0] if matches else None


def resolve_entity_requirements(goal, graph) -> list[ScenarioEntityRequirement]:
    requirements = []
    for term in sorted(goal.required_entities):
        node = _find_node(graph, term, "entity")
        if node is None:
            requirements.append(ScenarioEntityRequirement(requirement_id=f"entity:{term}", canonical_name=term, status="unresolved", missing_reason="entity not found in knowledge graph"))
            continue
        relationships = []
        if graph is not None:
            for e_id in graph.memory.outgoing_edge_ids(node.node_id) | graph.memory.incoming_edge_ids(node.node_id):
                edge = graph.memory.edges.get(e_id)
                if edge is not None and not edge.stale and edge.edge_type in {"relates_to", "parent_of", "child_of", "references", "owned_by"}:
                    relationships.append(edge.edge_type)
        requirements.append(
            ScenarioEntityRequirement(
                requirement_id=f"entity:{term}", entity_id=node.node_id, canonical_name=term,
                status="satisfied", confidence=node.confidence, resolved=True, resolution_source="knowledge_graph",
                required_relationships=sorted(set(relationships)), supporting_graph_nodes=[node.node_id],
            )
        )
    return requirements


def resolve_state_requirements(goal, graph) -> list[ScenarioStateRequirement]:
    requirements = []
    for term in sorted(goal.required_states):
        node = _find_node(graph, term, "entity_state")
        if node is None:
            requirements.append(ScenarioStateRequirement(requirement_id=f"state:{term}", state_label=term, status="unresolved", missing_reason="state not found in knowledge graph"))
            continue
        entity_id = ""
        if graph is not None:
            parts = node.node_id.split(":")
            if len(parts) >= 2:
                entity_id = f"entity:{parts[1]}"
        has_transition = False
        if graph is not None:
            has_transition = any(
                graph.memory.edges[e].edge_type in {"from_state", "to_state"}
                for e in graph.memory.incoming_edge_ids(node.node_id) if e in graph.memory.edges and not graph.memory.edges[e].stale
            )
        requirements.append(
            ScenarioStateRequirement(
                requirement_id=f"state:{term}", entity_id=entity_id, state_label=term, role="target",
                status="satisfied" if has_transition else "satisfiable", confidence=node.confidence,
                resolved=True, resolution_source="knowledge_graph", supporting_graph_nodes=[node.node_id],
            )
        )
    return requirements


def resolve_workflow_requirements(goal, graph) -> list[ScenarioWorkflowRequirement]:
    requirements = []
    for term in sorted(goal.required_workflows):
        node = _find_node(graph, term, "workflow")
        if node is None:
            requirements.append(ScenarioWorkflowRequirement(requirement_id=f"workflow:{term}", canonical_name=term, status="unresolved", missing_reason="workflow not found in knowledge graph"))
            continue
        required_steps: list[str] = []
        required_outcome = ""
        if graph is not None:
            try:
                required_steps = [s.canonical_name for s in graph.query_engine.workflow_steps(node.node_id)]
            except Exception:
                required_steps = []
            for e_id in graph.memory.outgoing_edge_ids(node.node_id):
                edge = graph.memory.edges.get(e_id)
                if edge is not None and not edge.stale and edge.edge_type == "produces":
                    outcome_node = graph.memory.nodes.get(edge.target_node_id)
                    if outcome_node is not None:
                        required_outcome = outcome_node.canonical_name
                        break
        status = "satisfied" if required_steps else "satisfiable"
        requirements.append(
            ScenarioWorkflowRequirement(
                requirement_id=f"workflow:{term}", workflow_id=node.node_id, canonical_name=term,
                status=status, confidence=node.confidence, resolved=True, resolution_source="knowledge_graph",
                required_steps=required_steps, required_outcome=required_outcome, supporting_graph_nodes=[node.node_id],
            )
        )
    return requirements


def resolve_output_requirements(goal, graph) -> list[ScenarioOutputRequirement]:
    requirements = []
    for term in sorted(goal.required_outputs):
        node = _find_node(graph, term, _OUTPUT_NODE_TYPES)
        if node is None:
            requirements.append(ScenarioOutputRequirement(requirement_id=f"output:{term}", status="unresolved", missing_reason="output not found in knowledge graph"))
            continue
        scope = ""
        if graph is not None:
            for e_id in graph.memory.incoming_edge_ids(node.node_id):
                edge = graph.memory.edges.get(e_id)
                if edge is not None and not edge.stale and any([edge.temporal_scope, edge.actor_scope, edge.tenant_scope, edge.filter_scope]):
                    scope = ", ".join(filter(None, [edge.temporal_scope, edge.actor_scope, edge.tenant_scope, edge.filter_scope]))
                    break
        requirements.append(
            ScenarioOutputRequirement(
                requirement_id=f"output:{term}", output_id=node.node_id, output_type=node.node_type, scope=scope,
                status="satisfied", confidence=node.confidence, resolved=True, resolution_source="knowledge_graph",
                supporting_graph_nodes=[node.node_id],
            )
        )
    return requirements


def resolve_data_requirements(goal, *, mutating: bool) -> list[ScenarioDataRequirement]:
    """Only mutating scenarios need prepared data -- an observational
    scenario locates EXISTING evidence and never needs to prepare any."""
    if not mutating or not goal.required_entities:
        return []
    requirements = []
    states = sorted(goal.required_states) or [""]
    for entity_term in sorted(goal.required_entities):
        state_label = states[0]
        req_id = f"data:{entity_term}:{state_label or 'any'}"
        requirements.append(
            ScenarioDataRequirement(
                requirement_id=req_id, entity_type=entity_term, field_purpose="workflow subject",
                required_state=state_label, required_scope="", ownership_requirement="",
                actor_relationship="", source_options=["reuse_existing_record"], mutation_requirement=True,
                cleanup_requirement=True, sensitivity="test_only", generation_policy="reuse_existing",
                status="satisfiable", confidence=0.4, mandatory=True, resolved=False,
                missing_reason="live data availability cannot be confirmed without browser access",
            )
        )
    return requirements

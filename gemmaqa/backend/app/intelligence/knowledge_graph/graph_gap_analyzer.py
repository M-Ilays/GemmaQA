"""Graph gap analysis — converts disconnected or incomplete knowledge into
explicit, queryable `GraphGap` records (task section 18). Every gap is
recorded, never turned into an executed investigation — that judgment
belongs to a future Goal Generation Engine this milestone explicitly does
not implement.
"""

from __future__ import annotations

from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.schemas import GraphGap

_OUTPUT_NODE_TYPES = {"derived_output", "counter", "badge", "chart", "report", "queue", "notification", "alert", "metric"}
_LOW_CONFIDENCE_THRESHOLD = 0.25
_DEPENDENCY_EDGE_TYPES = {
    "counts", "sums", "averages", "groups_by", "filters_by", "includes_state", "excludes_state",
    "increments", "decrements", "recalculates", "populates", "removes_from", "affects",
    "depends_on", "contributes_to", "controls_visibility", "creates_alert", "creates_notification",
    "unknown_effect",
}


class GraphGapAnalyzer:
    def __init__(self, memory: KnowledgeGraphMemory) -> None:
        self.memory = memory

    def analyze(self) -> list[GraphGap]:
        gaps: list[GraphGap] = []
        gaps.extend(self._per_node_gaps())
        gaps.extend(self._pending_reference_gaps())
        gaps.extend(self._contradiction_gaps())
        gaps.extend(self._structural_gaps())
        # `add_gap` deduplicates by (gap_type, node_ids) and returns the
        # ALREADY-STORED record (with its ORIGINAL gap_id) when one exists
        # -- callers (e.g. `close_gaps_not_in`) must compare against THESE
        # stored ids, never the fresh ones just constructed above, or every
        # re-synchronisation would look like "no gap survived" and resolve
        # everything that hadn't actually changed.
        return [self.memory.add_gap(gap) for gap in gaps]

    # -- node-type-specific completeness gaps ------------------------------------

    def _per_node_gaps(self) -> list[GraphGap]:
        gaps: list[GraphGap] = []
        for node in self.memory.nodes.values():
            if node.stale:
                continue
            out_types = {self.memory.edges[e].edge_type for e in self.memory.outgoing_edge_ids(node.node_id) if not self.memory.edges[e].stale}
            in_types = {self.memory.edges[e].edge_type for e in self.memory.incoming_edge_ids(node.node_id) if not self.memory.edges[e].stale}
            total_degree = len(self.memory.outgoing_edge_ids(node.node_id)) + len(self.memory.incoming_edge_ids(node.node_id))

            if total_degree == 0:
                gaps.append(_gap("isolated_node", f"'{node.node_id}' has no graph relationships at all.", node_ids=[node.node_id]))
                continue

            if node.node_type == "entity":
                if "manages" not in in_types and "owns" not in in_types and "assigned_to" not in in_types:
                    gaps.append(_gap("entity_without_actor", f"Entity '{node.canonical_name}' has no known associated actor.", node_ids=[node.node_id]))
                acts_on_sources = [
                    self.memory.nodes[self.memory.edges[e].source_node_id]
                    for e in self.memory.incoming_edge_ids(node.node_id)
                    if self.memory.edges[e].edge_type == "acts_on" and self.memory.edges[e].source_node_id in self.memory.nodes
                ]
                if not any(src.node_type in {"workflow", "workflow_step"} for src in acts_on_sources):
                    gaps.append(_gap("entity_without_workflow", f"Entity '{node.canonical_name}' has no known workflow acting on it.", node_ids=[node.node_id]))
            elif node.node_type == "entity_state":
                if "from_state" not in in_types and "to_state" not in in_types:
                    gaps.append(_gap("entity_state_without_transition", f"State '{node.canonical_name}' has no known transition.", node_ids=[node.node_id]))
            elif node.node_type == "operation":
                if "can_perform" not in in_types:
                    gaps.append(_gap("operation_without_actor", f"Operation '{node.canonical_name}' has no known actor able to perform it.", node_ids=[node.node_id]))
            elif node.node_type == "permission":
                if "enables" not in out_types:
                    gaps.append(_gap("permission_without_operation", f"Permission '{node.canonical_name}' has no known operation it enables.", node_ids=[node.node_id]))
            elif node.node_type == "actor":
                if "has_permission" not in out_types:
                    gaps.append(_gap("actor_without_confirmed_permission", f"Actor '{node.canonical_name}' has no confirmed permission.", node_ids=[node.node_id]))
                if "participates_in" not in out_types:
                    gaps.append(_gap("actor_without_workflow", f"Actor '{node.canonical_name}' has no known workflow participation.", node_ids=[node.node_id]))
            elif node.node_type == "workflow":
                if "participates_in" not in in_types:
                    gaps.append(_gap("workflow_without_actor", f"Workflow '{node.canonical_name}' has no known participating actor.", node_ids=[node.node_id]))
                if "acts_on" not in out_types:
                    gaps.append(_gap("workflow_without_entity", f"Workflow '{node.canonical_name}' has no known entity it acts on.", node_ids=[node.node_id]))
                if "triggered_by" not in out_types:
                    gaps.append(_gap("workflow_without_trigger", f"Workflow '{node.canonical_name}' has no known trigger.", node_ids=[node.node_id]))
                if "produces" not in out_types:
                    gaps.append(_gap("workflow_without_outcome", f"Workflow '{node.canonical_name}' has no known outcome.", node_ids=[node.node_id]))
                unresolved_steps = [
                    self.memory.edges[e].target_node_id for e in self.memory.outgoing_edge_ids(node.node_id)
                    if self.memory.edges[e].edge_type == "has_step" and self.memory.edges[e].status in {"unverified", "inferred", "blocked", "candidate"}
                ]
                if unresolved_steps:
                    gaps.append(_gap("workflow_with_unresolved_step", f"Workflow '{node.canonical_name}' has {len(unresolved_steps)} unresolved step(s).", node_ids=[node.node_id, *unresolved_steps]))
            elif node.node_type == "workflow_step":
                if "performed_by" not in out_types:
                    gaps.append(_gap("unresolved_actor_hand_off", f"Step '{node.canonical_name}' has no known performing actor.", node_ids=[node.node_id]))
            elif node.node_type == "prerequisite":
                if node.attributes.get("satisfied") is False and not _has_incoming_edge_type(self.memory, node.node_id, "requires"):
                    gaps.append(_gap("prerequisite_without_satisfaction_path", f"Prerequisite '{node.canonical_name}' is unsatisfied with no known path to satisfaction.", node_ids=[node.node_id]))
            elif node.node_type in _OUTPUT_NODE_TYPES:
                source_edges = [e for e in self.memory.incoming_edge_ids(node.node_id) if self.memory.edges[e].edge_type in _DEPENDENCY_EDGE_TYPES]
                if not source_edges:
                    gaps.append(_gap("output_without_source", f"Output '{node.canonical_name}' has no known producing source.", node_ids=[node.node_id]))
                elif not any(self.memory.edges[e].status in {"observed", "verified"} for e in source_edges):
                    gaps.append(_gap("unexplained_derived_output", f"Output '{node.canonical_name}' has only candidate/inferred sources, none confirmed.", node_ids=[node.node_id], exploration_value=0.4))
                if "visible_to" not in out_types:
                    gaps.append(_gap("output_without_consumer", f"Output '{node.canonical_name}' has no known consuming actor.", node_ids=[node.node_id]))
                if source_edges and not any(self.memory.nodes.get(self.memory.edges[e].source_node_id, node).node_type == "workflow" for e in source_edges):
                    gaps.append(_gap("dependency_without_workflow", f"Output '{node.canonical_name}' has a source but no producing workflow identified.", node_ids=[node.node_id], exploration_value=0.4))
                if source_edges and not any(self.memory.edges[e].status == "verified" for e in source_edges):
                    gaps.append(_gap("dependency_without_verification_path", f"Output '{node.canonical_name}' has no before/after-verified dependency yet.", node_ids=[node.node_id], exploration_value=0.4))
        return gaps

    # -- unresolved references ------------------------------------------------------

    def _pending_reference_gaps(self) -> list[GraphGap]:
        gaps: list[GraphGap] = []
        for ref in self.memory.unresolved_references():
            gap_type = "unresolved_identity" if "equivalent" in ref.reason.lower() or "alias" in ref.reason.lower() else "unresolved_reference"
            gaps.append(_gap(gap_type, f"Reference to '{ref.target_hint}' ({ref.edge_type}) from '{ref.source_node_id}' is unresolved: {ref.reason}", node_ids=[ref.source_node_id], related_registry_records=[ref.reference_id]))
        return gaps

    # -- contradictions/consistency issues become gaps too -----------------------

    def _contradiction_gaps(self) -> list[GraphGap]:
        gaps: list[GraphGap] = []
        for c in self.memory.contradictions.values():
            gaps.append(_gap("contradictory_relationship", c.description, node_ids=list(c.node_ids), edge_ids=list(c.edge_ids), risk="high", exploration_value=0.5))
        return gaps

    # -- structural: stale subgraphs, disconnected modules, low-confidence bridges --

    def _structural_gaps(self) -> list[GraphGap]:
        gaps: list[GraphGap] = []
        components = self.connected_components()
        if len(components) > 1:
            largest = max(components, key=len)
            for component in components:
                if component is largest:
                    continue
                if all(self.memory.nodes[n].stale for n in component if n in self.memory.nodes):
                    gaps.append(_gap("stale_subgraph", f"A subgraph of {len(component)} node(s) is entirely stale.", node_ids=list(component)))
                else:
                    gaps.append(_gap("disconnected_module", f"A subgraph of {len(component)} node(s) is disconnected from the main graph.", node_ids=list(component)))

        for e in self.memory.edges.values():
            if e.stale or e.confidence >= _LOW_CONFIDENCE_THRESHOLD:
                continue
            if not self._has_alternate_path(e.source_node_id, e.target_node_id, exclude_edge=e.edge_id, max_depth=2):
                gaps.append(_gap("low_confidence_bridge", f"Edge '{e.edge_id}' (confidence {round(e.confidence, 2)}) is the ONLY connection between '{e.source_node_id}' and '{e.target_node_id}'.", node_ids=[e.source_node_id, e.target_node_id], edge_ids=[e.edge_id]))
        return gaps

    def connected_components(self) -> list[set[str]]:
        adjacency: dict[str, set[str]] = {}
        for e in self.memory.edges.values():
            if e.stale:
                continue
            adjacency.setdefault(e.source_node_id, set()).add(e.target_node_id)
            adjacency.setdefault(e.target_node_id, set()).add(e.source_node_id)
        seen: set[str] = set()
        components: list[set[str]] = []
        for node_id in self.memory.nodes:
            if node_id in seen or node_id not in adjacency:
                continue
            stack, component = [node_id], set()
            while stack:
                current = stack.pop()
                if current in component:
                    continue
                component.add(current)
                stack.extend(adjacency.get(current, set()) - component)
            seen |= component
            components.append(component)
        return components

    def _has_alternate_path(self, start: str, goal: str, *, exclude_edge: str, max_depth: int) -> bool:
        frontier = {start}
        visited = {start}
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for node_id in frontier:
                for e_id in self.memory.outgoing_edge_ids(node_id) | self.memory.incoming_edge_ids(node_id):
                    if e_id == exclude_edge:
                        continue
                    edge = self.memory.edges.get(e_id)
                    if edge is None or edge.stale:
                        continue
                    other = edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
                    if other == goal:
                        return True
                    if other not in visited:
                        visited.add(other)
                        next_frontier.add(other)
            frontier = next_frontier
            if not frontier:
                break
        return False


def _gap(gap_type: str, description: str, *, node_ids=None, edge_ids=None, related_registry_records=None, risk: str = "low", exploration_value: float = 0.3) -> GraphGap:
    return GraphGap(
        gap_type=gap_type, description=description, node_ids=node_ids or [], edge_ids=edge_ids or [],
        related_registry_records=related_registry_records or [], risk=risk, exploration_value=exploration_value,
        confidence=0.3, recommended_future_goal_type=f"investigate_{gap_type}",
    )


def _has_incoming_edge_type(memory: KnowledgeGraphMemory, node_id: str, edge_type: str) -> bool:
    return any(memory.edges[e].edge_type == edge_type for e in memory.incoming_edge_ids(node_id))

"""Bounded graph query and traversal engine — the ONLY way callers touch
graph internals directly. Every traversal method accepts a
`GraphTraversalConstraint` (max depth, allowed types, min confidence,
allowed statuses, scope compatibility, stale/inferred inclusion,
result-size limit) and NEVER walks further than that — this is the
concrete mechanism preventing unrestricted transitive closure (task
section 27).
"""

from __future__ import annotations

from collections import deque

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.schemas import (
    GraphGap,
    GraphTraversalConstraint,
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgePath,
    KnowledgeSubgraph,
    GraphNeighbourhood,
)

_DEFAULT_CONSTRAINT = GraphTraversalConstraint()


class GraphQueryEngine:
    def __init__(self, memory: KnowledgeGraphMemory) -> None:
        self.memory = memory

    # -- node retrieval -----------------------------------------------------------

    def node_by_id(self, node_id: str) -> KnowledgeNode | None:
        return self.memory.nodes.get(node_id)

    def nodes_by_type(self, node_type: str, *, include_stale: bool = False) -> list[KnowledgeNode]:
        return [n for n in self.memory.nodes.values() if n.node_type == node_type and (include_stale or not n.stale)]

    def nodes_by_status(self, status: str) -> list[KnowledgeNode]:
        return [n for n in self.memory.nodes.values() if n.status == status]

    def nodes_by_confidence(self, min_confidence: float = 0.0, max_confidence: float = 1.0) -> list[KnowledgeNode]:
        return [n for n in self.memory.nodes.values() if min_confidence <= n.confidence <= max_confidence]

    def nodes_by_source_registry(self, source_registry: str) -> list[KnowledgeNode]:
        return [n for n in self.memory.nodes.values() if n.source_registry == source_registry]

    def find_nodes_by_canonical_name(self, name: str) -> list[KnowledgeNode]:
        target = normalize_term(name)
        return [n for n in self.memory.nodes.values() if normalize_term(n.canonical_name) == target]

    def find_nodes_by_alias(self, alias: str) -> list[KnowledgeNode]:
        target = normalize_term(alias)
        return [n for n in self.memory.nodes.values() if any(normalize_term(a) == target for a in n.aliases)]

    # -- edge retrieval -----------------------------------------------------------

    def edge_by_id(self, edge_id: str) -> KnowledgeEdge | None:
        return self.memory.edges.get(edge_id)

    def edges_by_type(self, edge_type: str) -> list[KnowledgeEdge]:
        return [e for e in self.memory.edges.values() if e.edge_type == edge_type]

    def incoming_edges(self, node_id: str) -> list[KnowledgeEdge]:
        return [self.memory.edges[e] for e in self.memory.incoming_edge_ids(node_id)]

    def outgoing_edges(self, node_id: str) -> list[KnowledgeEdge]:
        return [self.memory.edges[e] for e in self.memory.outgoing_edge_ids(node_id)]

    def edges_between(self, node_a: str, node_b: str) -> list[KnowledgeEdge]:
        ids = (self.memory.outgoing_edge_ids(node_a) & self.memory.incoming_edge_ids(node_b)) | (
            self.memory.outgoing_edge_ids(node_b) & self.memory.incoming_edge_ids(node_a)
        )
        return [self.memory.edges[e] for e in ids]

    def edges_by_status(self, status: str) -> list[KnowledgeEdge]:
        return [e for e in self.memory.edges.values() if e.status == status]

    def edges_by_confidence(self, min_confidence: float = 0.0, max_confidence: float = 1.0) -> list[KnowledgeEdge]:
        return [e for e in self.memory.edges.values() if min_confidence <= e.confidence <= max_confidence]

    # -- domain-neutral semantic queries -------------------------------------------

    def actors_for_entity(self, entity_id: str) -> list[KnowledgeNode]:
        actor_ids = {
            e.source_node_id for e in self.incoming_edges(entity_id)
            if e.edge_type in {"manages", "owns", "assigned_to"} and self._node_type(e.source_node_id) == "actor"
        }
        return [self.memory.nodes[n] for n in actor_ids if n in self.memory.nodes]

    def entities_for_actor(self, actor_id: str) -> list[KnowledgeNode]:
        entity_ids = {e.target_node_id for e in self.outgoing_edges(actor_id) if e.edge_type in {"manages", "owns"} and self._node_type(e.target_node_id) == "entity"}
        return [self.memory.nodes[n] for n in entity_ids if n in self.memory.nodes]

    def workflows_for_actor(self, actor_id: str) -> list[KnowledgeNode]:
        wf_ids = {e.target_node_id for e in self.outgoing_edges(actor_id) if e.edge_type == "participates_in"}
        return [self.memory.nodes[n] for n in wf_ids if n in self.memory.nodes]

    def workflows_for_entity(self, entity_id: str) -> list[KnowledgeNode]:
        wf_ids = {e.source_node_id for e in self.incoming_edges(entity_id) if e.edge_type == "acts_on" and self._node_type(e.source_node_id) == "workflow"}
        return [self.memory.nodes[n] for n in wf_ids if n in self.memory.nodes]

    def actors_for_workflow(self, workflow_id: str) -> list[KnowledgeNode]:
        actor_ids = {e.source_node_id for e in self.incoming_edges(workflow_id) if e.edge_type == "participates_in"}
        return [self.memory.nodes[n] for n in actor_ids if n in self.memory.nodes]

    def entities_for_workflow(self, workflow_id: str) -> list[KnowledgeNode]:
        entity_ids = {e.target_node_id for e in self.outgoing_edges(workflow_id) if e.edge_type == "acts_on" and self._node_type(e.target_node_id) == "entity"}
        return [self.memory.nodes[n] for n in entity_ids if n in self.memory.nodes]

    def workflow_steps(self, workflow_id: str) -> list[KnowledgeNode]:
        step_ids = [e.target_node_id for e in self.outgoing_edges(workflow_id) if e.edge_type == "has_step"]
        steps = [self.memory.nodes[s] for s in step_ids if s in self.memory.nodes]
        return sorted(steps, key=lambda n: n.attributes.get("sequence_hint", 0))

    def prerequisites_for_workflow(self, workflow_id: str) -> list[KnowledgeNode]:
        ids = {e.target_node_id for e in self.outgoing_edges(workflow_id) if e.edge_type == "requires"}
        return [self.memory.nodes[n] for n in ids if n in self.memory.nodes]

    def transitions_for_entity(self, entity_id: str) -> list[KnowledgeNode]:
        # Transitions don't reference the entity directly -- they connect
        # to its STATE nodes (from_state/to_state); collect transitions
        # touching any state this entity has_state.
        state_ids = {e.target_node_id for e in self.outgoing_edges(entity_id) if e.edge_type == "has_state"}
        transition_ids: set[str] = set()
        for state_id in state_ids:
            for e in self.incoming_edges(state_id):
                if e.edge_type in {"from_state", "to_state"}:
                    transition_ids.add(e.source_node_id)
        return [self.memory.nodes[t] for t in transition_ids if t in self.memory.nodes]

    def outputs_for_entity(self, entity_id: str) -> list[KnowledgeNode]:
        ids = {e.target_node_id for e in self.outgoing_edges(entity_id) if self._is_output(e.target_node_id)}
        return [self.memory.nodes[n] for n in ids if n in self.memory.nodes]

    def outputs_for_workflow(self, workflow_id: str) -> list[KnowledgeNode]:
        ids = {e.target_node_id for e in self.outgoing_edges(workflow_id) if self._is_output(e.target_node_id)}
        return [self.memory.nodes[n] for n in ids if n in self.memory.nodes]

    def producers_for_output(self, output_id: str) -> list[KnowledgeNode]:
        ids = {e.target_node_id for e in self.outgoing_edges(output_id) if e.edge_type == "generated_by"}
        return [self.memory.nodes[n] for n in ids if n in self.memory.nodes]

    def consumers_for_output(self, output_id: str) -> list[KnowledgeNode]:
        ids = {e.target_node_id for e in self.outgoing_edges(output_id) if e.edge_type == "visible_to"}
        return [self.memory.nodes[n] for n in ids if n in self.memory.nodes]

    def permissions_for_actor(self, actor_id: str) -> list[KnowledgeNode]:
        ids = {e.target_node_id for e in self.outgoing_edges(actor_id) if e.edge_type in {"has_permission", "lacks_permission"}}
        return [self.memory.nodes[n] for n in ids if n in self.memory.nodes]

    def operations_for_actor(self, actor_id: str) -> list[KnowledgeNode]:
        ids = {e.target_node_id for e in self.outgoing_edges(actor_id) if e.edge_type == "can_perform"}
        return [self.memory.nodes[n] for n in ids if n in self.memory.nodes]

    def actors_for_operation(self, operation_id: str) -> list[KnowledgeNode]:
        ids = {e.source_node_id for e in self.incoming_edges(operation_id) if e.edge_type == "can_perform"}
        return [self.memory.nodes[n] for n in ids if n in self.memory.nodes]

    def dependencies_requiring_actor_switch(self) -> list[KnowledgeEdge]:
        return [e for e in self.memory.edges.values() if e.edge_type == "hands_off_to" and not e.stale]

    def unresolved_cross_role_paths(self) -> list:
        return [r for r in self.memory.unresolved_references() if r.edge_type in {"performed_by", "participates_in", "hands_off_to"}]

    def unexplained_outputs(self) -> list[GraphGap]:
        return [g for g in self.memory.gaps.values() if g.gap_type in {"unexplained_derived_output", "output_without_source"} and g.status == "open"]

    def contradictory_subgraphs(self) -> list:
        return list(self.memory.contradictions.values())

    def incomplete_workflows(self) -> list[KnowledgeNode]:
        gap_node_ids = {n for g in self.memory.gaps.values() if g.gap_type.startswith("workflow_") for n in g.node_ids}
        return [self.memory.nodes[n] for n in gap_node_ids if n in self.memory.nodes and self.memory.nodes[n].node_type == "workflow"]

    def isolated_nodes(self) -> list[KnowledgeNode]:
        return [n for n in self.memory.nodes.values() if not n.stale and not self.memory.outgoing_edge_ids(n.node_id) and not self.memory.incoming_edge_ids(n.node_id)]

    def high_risk_gaps(self) -> list[GraphGap]:
        return sorted((g for g in self.memory.gaps.values() if g.risk in {"high", "medium"} and g.status == "open"), key=lambda g: g.exploration_value, reverse=True)

    def low_confidence_bridges(self) -> list[GraphGap]:
        return [g for g in self.memory.gaps.values() if g.gap_type == "low_confidence_bridge" and g.status == "open"]

    # -- bounded traversal ----------------------------------------------------------

    def shortest_path(self, start: str, goal: str, *, constraint: GraphTraversalConstraint = _DEFAULT_CONSTRAINT) -> KnowledgePath | None:
        if start not in self.memory.nodes or goal not in self.memory.nodes:
            return None
        if start == goal:
            return KnowledgePath(node_ids=[start], edge_ids=[], length=0)
        parent: dict[str, tuple[str, str]] = {}
        visited = {start}
        queue = deque([(start, 0)])
        while queue:
            node_id, depth = queue.popleft()
            if depth >= constraint.max_depth:
                continue
            for e_id in self._traversable_edges(node_id, constraint):
                edge = self.memory.edges[e_id]
                other = edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
                if other in visited:
                    continue
                other_node = self.memory.nodes.get(other)
                if other_node is None or not self._node_allowed(other_node, constraint):
                    continue
                visited.add(other)
                parent[other] = (node_id, e_id)
                if other == goal:
                    return self._reconstruct_path(goal, start, parent)
                queue.append((other, depth + 1))
        return None

    def all_paths(self, start: str, goal: str, *, constraint: GraphTraversalConstraint = _DEFAULT_CONSTRAINT) -> list[KnowledgePath]:
        if start not in self.memory.nodes or goal not in self.memory.nodes:
            return []
        results: list[KnowledgePath] = []

        def dfs(node_id: str, path_nodes: list[str], path_edges: list[str]) -> None:
            if len(results) >= constraint.max_results:
                return
            if node_id == goal and path_nodes:
                results.append(KnowledgePath(node_ids=list(path_nodes), edge_ids=list(path_edges), length=len(path_edges)))
                return
            if len(path_edges) >= constraint.max_depth:
                return
            for e_id in self._traversable_edges(node_id, constraint):
                edge = self.memory.edges[e_id]
                other = edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
                if other in path_nodes:
                    continue
                other_node = self.memory.nodes.get(other)
                if other_node is None or not self._node_allowed(other_node, constraint):
                    continue
                dfs(other, path_nodes + [other], path_edges + [e_id])
                if len(results) >= constraint.max_results:
                    return

        dfs(start, [start], [])
        return results

    def neighbourhood(self, node_id: str, *, constraint: GraphTraversalConstraint = _DEFAULT_CONSTRAINT) -> GraphNeighbourhood:
        nodes, edges, truncated = self._bfs(node_id, constraint=constraint)
        return GraphNeighbourhood(center_node_id=node_id, depth=constraint.max_depth, node_ids=sorted(nodes), edge_ids=sorted(edges), truncated=truncated)

    def ancestors(self, node_id: str, *, constraint: GraphTraversalConstraint = _DEFAULT_CONSTRAINT) -> set[str]:
        nodes, _, _ = self._bfs(node_id, constraint=constraint, direction="in")
        return nodes - {node_id}

    def descendants(self, node_id: str, *, constraint: GraphTraversalConstraint = _DEFAULT_CONSTRAINT) -> set[str]:
        nodes, _, _ = self._bfs(node_id, constraint=constraint, direction="out")
        return nodes - {node_id}

    def reachable_nodes(self, node_id: str, *, constraint: GraphTraversalConstraint = _DEFAULT_CONSTRAINT) -> set[str]:
        nodes, _, _ = self._bfs(node_id, constraint=constraint, direction="both")
        return nodes

    def subgraph_for_nodes(self, node_ids: list[str]) -> KnowledgeSubgraph:
        node_set = set(node_ids)
        edge_ids = {
            e.edge_id for e in self.memory.edges.values()
            if e.source_node_id in node_set and e.target_node_id in node_set
        }
        return KnowledgeSubgraph(node_ids=sorted(node_set), edge_ids=sorted(edge_ids))

    def subgraph_for_entity(self, entity_id: str, *, constraint: GraphTraversalConstraint = _DEFAULT_CONSTRAINT) -> KnowledgeSubgraph:
        nodes, edges, _ = self._bfs(entity_id, constraint=constraint)
        return KnowledgeSubgraph(focus_node_id=entity_id, node_ids=sorted(nodes), edge_ids=sorted(edges))

    subgraph_for_actor = subgraph_for_entity
    subgraph_for_workflow = subgraph_for_entity
    subgraph_for_output = subgraph_for_entity

    # -- internals --------------------------------------------------------------

    def _node_type(self, node_id: str) -> str:
        node = self.memory.nodes.get(node_id)
        return node.node_type if node is not None else ""

    def _is_output(self, node_id: str) -> bool:
        return self._node_type(node_id) in {"derived_output", "counter", "badge", "chart", "report", "queue", "notification", "alert", "metric"}

    @staticmethod
    def _edge_allowed(edge: KnowledgeEdge, constraint: GraphTraversalConstraint) -> bool:
        if edge.stale and not constraint.include_stale:
            return False
        if edge.status == "inferred" and not constraint.include_inferred:
            return False
        if constraint.allowed_edge_types is not None and edge.edge_type not in constraint.allowed_edge_types:
            return False
        if edge.confidence < constraint.min_confidence:
            return False
        if constraint.allowed_statuses is not None and edge.status not in constraint.allowed_statuses:
            return False
        if constraint.required_scope is not None and not edge.scope().compatible_with(constraint.required_scope):
            return False
        return True

    @staticmethod
    def _node_allowed(node: KnowledgeNode, constraint: GraphTraversalConstraint) -> bool:
        if node.stale and not constraint.include_stale:
            return False
        if constraint.allowed_node_types is not None and node.node_type not in constraint.allowed_node_types:
            return False
        return True

    def _traversable_edges(self, node_id: str, constraint: GraphTraversalConstraint) -> list[str]:
        candidate_ids = self.memory.outgoing_edge_ids(node_id) | self.memory.incoming_edge_ids(node_id)
        return [e_id for e_id in candidate_ids if e_id in self.memory.edges and self._edge_allowed(self.memory.edges[e_id], constraint)]

    def _bfs(self, start: str, *, constraint: GraphTraversalConstraint, direction: str = "both") -> tuple[set[str], set[str], bool]:
        if start not in self.memory.nodes:
            return set(), set(), False
        visited_nodes = {start}
        visited_edges: set[str] = set()
        frontier = [start]
        truncated = False
        for _ in range(constraint.max_depth):
            next_frontier: list[str] = []
            for node_id in frontier:
                candidate_ids: set[str] = set()
                if direction in ("out", "both"):
                    candidate_ids |= self.memory.outgoing_edge_ids(node_id)
                if direction in ("in", "both"):
                    candidate_ids |= self.memory.incoming_edge_ids(node_id)
                for e_id in candidate_ids:
                    edge = self.memory.edges.get(e_id)
                    if edge is None or not self._edge_allowed(edge, constraint):
                        continue
                    other = edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
                    other_node = self.memory.nodes.get(other)
                    if other_node is None or not self._node_allowed(other_node, constraint):
                        continue
                    if len(visited_nodes) >= constraint.max_results and other not in visited_nodes:
                        truncated = True
                        continue
                    visited_edges.add(e_id)
                    if other not in visited_nodes:
                        visited_nodes.add(other)
                        next_frontier.append(other)
            frontier = next_frontier
            if not frontier:
                break
        return visited_nodes, visited_edges, truncated

    @staticmethod
    def _reconstruct_path(goal: str, start: str, parent: dict[str, tuple[str, str]]) -> KnowledgePath:
        nodes = [goal]
        edges: list[str] = []
        current = goal
        while current != start:
            prev, e_id = parent[current]
            edges.append(e_id)
            nodes.append(prev)
            current = prev
        nodes.reverse()
        edges.reverse()
        return KnowledgePath(node_ids=nodes, edge_ids=edges, length=len(edges))

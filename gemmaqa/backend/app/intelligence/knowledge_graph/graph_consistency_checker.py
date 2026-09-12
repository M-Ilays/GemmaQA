"""Graph consistency checking — detects contradictions and structural
inconsistencies WITHOUT deleting either side of the conflicting evidence.
A `GraphConsistencyIssue` (or `GraphContradiction`) is a first-class,
queryable record, never a silent overwrite.
"""

from __future__ import annotations

from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.schemas import GraphConsistencyIssue, GraphContradiction


def _operation_entity_key(operation_node_id: str) -> str:
    """`"operation:job:create"` -> `"job"` -- the entity-name segment of an
    operation node id (see `graph_node_factory.operation_node_id`)."""
    parts = operation_node_id.split(":")
    return parts[1] if len(parts) >= 3 else ""


def _operation_verb_key(operation_node_id: str) -> str:
    """`"operation:job:create"` -> `"create"` -- the verb segment. Matching
    on entity alone (without this) means a denial of ONE operation
    ("export") on an entity would falsely flag the actor for performing
    ANY OTHER unrelated step ("create") on that same entity."""
    parts = operation_node_id.split(":")
    return parts[-1] if len(parts) >= 3 else ""


class GraphConsistencyChecker:
    def __init__(self, memory: KnowledgeGraphMemory) -> None:
        self.memory = memory

    def check_all(self) -> dict[str, int]:
        return {
            "permission_conflicts": self._check_permission_conflicts(),
            "opposite_effect_conflicts": self._check_opposite_effects(),
            "dangling_references": self._check_dangling_references(),
            "verified_but_contradicted_source": self._check_verified_but_contradicted_source(),
            "performed_despite_denial": self._check_performed_despite_denial(),
            "equivalence_conflicts": self._check_equivalence_conflicts(),
            "stale_endpoints": self._check_stale_endpoints(),
            "cyclic_workflow_ordering": self._check_cyclic_ordering(),
        }

    # -- 1. actor can_perform + cannot_perform same operation, compatible scope --

    def _check_permission_conflicts(self) -> int:
        found = 0
        by_actor_op: dict[tuple[str, str], list] = {}
        for e in self.memory.edges.values():
            if e.stale or e.edge_type not in {"can_perform", "cannot_perform"}:
                continue
            by_actor_op.setdefault((e.source_node_id, e.target_node_id), []).append(e)
        for (actor_id, op_id), edges in by_actor_op.items():
            positives = [e for e in edges if e.edge_type == "can_perform"]
            negatives = [e for e in edges if e.edge_type == "cannot_perform"]
            for pos in positives:
                for neg in negatives:
                    if not pos.scope().compatible_with(neg.scope()):
                        continue
                    issue = GraphConsistencyIssue(
                        issue_type="permission_conflict", severity="high", node_ids=[actor_id, op_id],
                        edge_ids=[pos.edge_id, neg.edge_id], compatible_scope=True,
                        explanation=f"Actor '{actor_id}' has both can_perform and cannot_perform for '{op_id}' under compatible scope.",
                    )
                    self.memory.add_consistency_issue(issue)
                    found += 1
        return found

    # -- 2. opposite effect_direction on the same source->target under compatible scope --

    def _check_opposite_effects(self) -> int:
        found = 0
        by_pair: dict[tuple[str, str], list] = {}
        for e in self.memory.edges.values():
            if e.stale or "effect_direction" not in e.attributes:
                continue
            by_pair.setdefault((e.source_node_id, e.target_node_id), []).append(e)
        opposite_pairs = {frozenset({"increase", "decrease"})}
        for (src, tgt), edges in by_pair.items():
            for i, a in enumerate(edges):
                for b in edges[i + 1 :]:
                    dir_a, dir_b = a.attributes.get("effect_direction"), b.attributes.get("effect_direction")
                    if dir_a == dir_b or frozenset({dir_a, dir_b}) not in opposite_pairs:
                        continue
                    if not a.scope().compatible_with(b.scope()):
                        continue
                    contradiction = GraphContradiction(
                        description=f"'{src}' -> '{tgt}' has conflicting effect directions ('{dir_a}' vs '{dir_b}') under compatible scope.",
                        node_ids=[src, tgt], edge_ids=[a.edge_id, b.edge_id], confidence=0.4,
                    )
                    self.memory.add_contradiction(contradiction)
                    for e_id in (a.edge_id, b.edge_id):
                        edge = self.memory.edges[e_id]
                        if contradiction.contradiction_id not in edge.contradiction_ids:
                            edge.contradiction_ids.append(contradiction.contradiction_id)
                    found += 1
        return found

    # -- 3. edge endpoints referencing a node id that doesn't exist --------------

    def _check_dangling_references(self) -> int:
        found = 0
        for e in self.memory.edges.values():
            if e.stale:
                continue
            missing = [nid for nid in (e.source_node_id, e.target_node_id) if nid not in self.memory.nodes]
            if not missing:
                continue
            issue = GraphConsistencyIssue(
                issue_type="dangling_reference", severity="medium", node_ids=missing, edge_ids=[e.edge_id],
                explanation=f"Edge '{e.edge_id}' references missing node(s): {missing}.",
            )
            self.memory.add_consistency_issue(issue)
            found += 1
        return found

    # -- 5. an edge is verified but its source node is contradicted -------------

    def _check_verified_but_contradicted_source(self) -> int:
        found = 0
        for e in self.memory.edges.values():
            if e.stale or e.status != "verified":
                continue
            source = self.memory.nodes.get(e.source_node_id)
            if source is not None and source.status == "contradicted":
                issue = GraphConsistencyIssue(
                    issue_type="verified_dependency_contradicted_source", severity="high", node_ids=[source.node_id],
                    edge_ids=[e.edge_id], explanation=f"Edge '{e.edge_id}' is verified but its source node '{source.node_id}' is contradicted.",
                )
                self.memory.add_consistency_issue(issue)
                found += 1
        return found

    # -- 6. actor performs a step despite verified negative permission -----------

    def _check_performed_despite_denial(self) -> int:
        found = 0
        for pb in self.memory.edges.values():
            if pb.stale or pb.edge_type != "performed_by":
                continue
            step_id, actor_id = pb.source_node_id, pb.target_node_id
            step_node = self.memory.nodes.get(step_id)
            if step_node is None:
                continue
            step_verb = step_node.canonical_name
            acts_on_edges = [self.memory.edges[e] for e in self.memory.outgoing_edge_ids(step_id) if self.memory.edges[e].edge_type == "acts_on"]
            denial_edges = [self.memory.edges[e] for e in self.memory.outgoing_edge_ids(actor_id) if self.memory.edges[e].edge_type == "lacks_permission"]
            for acts_on in acts_on_edges:
                entity_id = acts_on.target_node_id
                entity_key = entity_id.split(":", 1)[-1] if ":" in entity_id else entity_id
                for denial in denial_edges:
                    can_perform = [self.memory.edges[e] for e in self.memory.outgoing_edge_ids(denial.target_node_id) if self.memory.edges[e].edge_type == "enables"]
                    # Both the ENTITY and the VERB must match -- a denial of
                    # "export" on an entity must never flag an unrelated
                    # "create" step on that same entity as a violation.
                    if any(
                        _operation_entity_key(cp.target_node_id) == entity_key and _operation_verb_key(cp.target_node_id) == step_verb
                        for cp in can_perform
                    ):
                        issue = GraphConsistencyIssue(
                            issue_type="performed_despite_denial", severity="high", node_ids=[actor_id, entity_id],
                            edge_ids=[pb.edge_id, denial.edge_id], explanation=f"Actor '{actor_id}' performed step '{step_id}' ('{step_verb}') on '{entity_id}' despite a verified permission denial.",
                        )
                        self.memory.add_consistency_issue(issue)
                        found += 1
        return found

    # -- 8. equivalent_to edge between nodes with conflicting authoritative ids --

    def _check_equivalence_conflicts(self) -> int:
        found = 0
        for e in self.memory.edges.values():
            if e.stale or e.edge_type != "equivalent_to":
                continue
            a, b = self.memory.nodes.get(e.source_node_id), self.memory.nodes.get(e.target_node_id)
            if a is None or b is None:
                continue
            if a.source_registry == b.source_registry and a.source_record_id and b.source_record_id and a.source_record_id != b.source_record_id:
                issue = GraphConsistencyIssue(
                    issue_type="equivalence_conflict", severity="medium", node_ids=[a.node_id, b.node_id], edge_ids=[e.edge_id],
                    explanation=f"'{a.node_id}' and '{b.node_id}' are marked equivalent but have conflicting authoritative source ids from the same registry.",
                )
                self.memory.add_consistency_issue(issue)
                found += 1
        return found

    # -- 9. an active edge whose endpoint is stale --------------------------------

    def _check_stale_endpoints(self) -> int:
        found = 0
        for e in self.memory.edges.values():
            if e.stale:
                continue
            source, target = self.memory.nodes.get(e.source_node_id), self.memory.nodes.get(e.target_node_id)
            if (source is not None and source.stale) or (target is not None and target.stale):
                issue = GraphConsistencyIssue(
                    issue_type="stale_edge_endpoint", severity="low", node_ids=[e.source_node_id, e.target_node_id], edge_ids=[e.edge_id],
                    explanation=f"Edge '{e.edge_id}' is active but one of its endpoints is stale.",
                )
                self.memory.add_consistency_issue(issue)
                found += 1
        return found

    # -- 10. cyclic step ordering in a workflow expected to be linear ------------

    def _check_cyclic_ordering(self) -> int:
        found = 0
        precedes_by_source: dict[str, list[str]] = {}
        for e in self.memory.edges.values():
            if e.stale or e.edge_type != "precedes":
                continue
            precedes_by_source.setdefault(e.source_node_id, []).append(e.target_node_id)

        visiting: set[str] = set()
        visited: set[str] = set()

        def has_cycle(node: str, path: list[str]) -> list[str] | None:
            if node in visiting:
                return (path[path.index(node) :] if node in path else []) + [node]
            if node in visited:
                return None
            visiting.add(node)
            result = None
            for nxt in precedes_by_source.get(node, []):
                cycle = has_cycle(nxt, path + [node])
                if cycle is not None:
                    result = cycle
                    break
            # ALWAYS unwind, even when a cycle was found and is being
            # propagated up -- otherwise a node stays stuck "visiting"
            # forever, and the next top-level start that revisits it
            # crashes on `path.index(node)` with an empty path.
            visiting.discard(node)
            visited.add(node)
            return result

        for start in list(precedes_by_source.keys()):
            if start in visited:
                continue
            cycle = has_cycle(start, [])
            if cycle:
                issue = GraphConsistencyIssue(
                    issue_type="cyclic_workflow_ordering", severity="medium", node_ids=cycle,
                    explanation=f"Cyclic step ordering detected: {' -> '.join(cycle)}.",
                )
                self.memory.add_consistency_issue(issue)
                found += 1
        return found

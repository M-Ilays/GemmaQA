"""Relationship resolution — converts a reference (a node_id, a canonical
name, an alias, or a bare hint string) into an actual graph node, or defers
it as a `PendingReference` when the target doesn't exist yet.

This is the mechanism behind "a workflow step references an actor name
before the Actor Registry confirms it" (task section 14): the edge isn't
silently dropped or guessed at — it's held as a pending reference and
resolved (without duplication) the next time synchronisation runs and the
target has appeared.
"""

from __future__ import annotations

from datetime import datetime

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.schemas import GraphEvidenceReference, PendingReference


class RelationshipResolver:
    def __init__(self, memory: KnowledgeGraphMemory) -> None:
        self.memory = memory

    def resolve(self, hint: str, node_type_hint: str = "") -> str | None:
        if not hint:
            return None
        if hint in self.memory.nodes:
            node = self.memory.nodes[hint]
            if not node_type_hint or node.node_type == node_type_hint:
                return hint
        normalized = normalize_term(hint)
        for node in self.memory.nodes.values():
            if node_type_hint and node.node_type != node_type_hint:
                continue
            if normalize_term(node.canonical_name) == normalized:
                return node.node_id
            if any(normalize_term(a) == normalized for a in node.aliases):
                return node.node_id
        return None

    def resolve_or_defer(
        self,
        *,
        source_node_id: str,
        edge_type: str,
        hint: str,
        node_type_hint: str = "",
        reason: str = "",
        evidence: list[GraphEvidenceReference] | None = None,
    ) -> str | None:
        target = self.resolve(hint, node_type_hint)
        if target is not None:
            return target
        self.memory.add_pending_reference(
            PendingReference(
                source_node_id=source_node_id, edge_type=edge_type, target_hint=hint,
                target_node_type=node_type_hint, reason=reason, evidence_references=evidence or [],
            )
        )
        return None

    def try_resolve_pending(self) -> list[tuple[PendingReference, str]]:
        """Attempts to resolve every still-unresolved reference against the
        graph's CURRENT node set. Returns (reference, resolved_node_id)
        pairs — the caller is responsible for actually creating the edge
        (so the resolution trace and the edge creation stay in the same
        place, the synchronizer)."""
        resolved: list[tuple[PendingReference, str]] = []
        for ref in self.memory.unresolved_references():
            target = self.resolve(ref.target_hint, ref.target_node_type)
            if target is not None:
                ref.resolved = True
                ref.resolved_at = datetime.utcnow()
                resolved.append((ref, target))
        return resolved

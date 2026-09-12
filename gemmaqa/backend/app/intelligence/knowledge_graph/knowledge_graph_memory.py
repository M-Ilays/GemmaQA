"""Knowledge graph memory — the raw node/edge/gap/contradiction store and
its idempotent merge logic. Mirrors `dependency_memory.py`'s role: owns the
mutable dict-backed store; `ApplicationKnowledgeGraph` (in
`knowledge_graph.py`) is the orchestrator that calls into this.

Idempotency is the load-bearing property here (task section 8): re-
synchronising from UNCHANGED registries must leave every node/edge byte-
for-byte identical (same `last_seen`, same `observation_count`, same
`confidence`, same `graph_version`) — never silently drift on every sync
tick. This is achieved by comparing the INCOMING desired state against what
is already stored and only touching a record when something actually
differs.
"""

from __future__ import annotations

from datetime import datetime

from app.intelligence.knowledge_graph.schemas import (
    GraphContradiction,
    GraphConsistencyIssue,
    GraphEvidenceReference,
    GraphGap,
    GraphInference,
    GraphProvenance,
    KnowledgeEdge,
    KnowledgeNode,
    PendingReference,
)

MAX_EVIDENCE_PER_RECORD = 40
MAX_ALIASES_PER_NODE = 20
MAX_PROVENANCE_PER_RECORD = 20


def _evidence_key(ev: GraphEvidenceReference) -> tuple[str, str, str]:
    return (ev.source_kind, ev.observed_text, ev.page_url)


def _dedupe_evidence(existing: list[GraphEvidenceReference], new: list[GraphEvidenceReference]) -> tuple[list[GraphEvidenceReference], bool]:
    seen = {_evidence_key(e) for e in existing}
    merged = list(existing)
    changed = False
    for ev in new:
        key = _evidence_key(ev)
        if key in seen:
            continue
        seen.add(key)
        merged.append(ev)
        changed = True
    return merged[-MAX_EVIDENCE_PER_RECORD:], changed


class KnowledgeGraphMemory:
    def __init__(self) -> None:
        self.nodes: dict[str, KnowledgeNode] = {}
        self.edges: dict[str, KnowledgeEdge] = {}
        self.pending_references: dict[str, PendingReference] = {}
        self.contradictions: dict[str, GraphContradiction] = {}
        self.consistency_issues: dict[str, GraphConsistencyIssue] = {}
        self.gaps: dict[str, GraphGap] = {}
        self.inferences: dict[str, GraphInference] = {}
        self.graph_version: int = 0
        self._outgoing: dict[str, set[str]] = {}
        self._incoming: dict[str, set[str]] = {}
        # Set/cleared once per synchronize() call by the orchestrator --
        # tracks whether THIS pass actually changed anything, so
        # graph_version only increments when content truly changed.
        self.dirty_this_pass: bool = False
        self.added_node_ids: list[str] = []
        self.updated_node_ids: list[str] = []
        self.stale_node_ids: list[str] = []
        self.added_edge_ids: list[str] = []
        self.updated_edge_ids: list[str] = []
        self.stale_edge_ids: list[str] = []
        self.resolved_reference_ids: list[str] = []
        self.new_contradiction_ids: list[str] = []
        self.new_gap_ids: list[str] = []
        self.resolved_gap_ids_this_pass: list[str] = []
        self.version_history: list = []  # list[GraphVersion], appended by end_pass()

    def begin_pass(self) -> None:
        """Opens ONE version-tracking window. The caller (the top-level
        `ApplicationKnowledgeGraph.synchronize()`) is responsible for
        calling this exactly once before synchronisation + inference +
        consistency + gap analysis all run, and `end_pass()` exactly once
        after — so a single graph_version bump covers the WHOLE pipeline,
        not just the registry-sync portion of it."""
        self.dirty_this_pass = False
        self.added_node_ids = []
        self.updated_node_ids = []
        self.stale_node_ids = []
        self.added_edge_ids = []
        self.updated_edge_ids = []
        self.stale_edge_ids = []
        self.resolved_reference_ids = []
        self.new_contradiction_ids = []
        self.new_gap_ids = []
        self.resolved_gap_ids_this_pass = []

    def end_pass(self) -> bool:
        """Returns True if graph_version was bumped this pass."""
        if self.dirty_this_pass:
            self.graph_version += 1
            return True
        return False

    # -- nodes ------------------------------------------------------------------

    def upsert_node(
        self,
        node_id: str,
        *,
        node_type: str,
        canonical_name: str,
        aliases: list[str] | None = None,
        status: str = "observed",
        confidence: float = 0.0,
        source_registry: str,
        source_record_id: str,
        source_record_version: int = 0,
        evidence_references: list[GraphEvidenceReference] | None = None,
        attributes: dict | None = None,
        tags: list[str] | None = None,
        iteration: int = 0,
        now: datetime | None = None,
    ) -> KnowledgeNode:
        now = now or datetime.utcnow()
        aliases = aliases or []
        evidence_references = evidence_references or []
        attributes = attributes or {}
        tags = tags or []

        existing = self.nodes.get(node_id)
        if existing is None:
            node = KnowledgeNode(
                node_id=node_id, node_type=node_type, canonical_name=canonical_name, aliases=list(dict.fromkeys(aliases))[:MAX_ALIASES_PER_NODE],
                status=status, confidence=confidence, source_registry=source_registry, source_record_id=source_record_id,
                source_record_version=source_record_version, evidence_references=evidence_references[:MAX_EVIDENCE_PER_RECORD],
                provenance=[GraphProvenance(source_registry=source_registry, source_record_id=source_record_id, source_record_version=source_record_version, synchronized_at_iteration=iteration, synchronized_at=now)],
                attributes=attributes, tags=tags, first_seen=now, last_seen=now, observation_count=1, graph_version=self.graph_version,
                active=True, stale=False,
            )
            self.nodes[node_id] = node
            self.added_node_ids.append(node_id)
            self.dirty_this_pass = True
            return node

        changed = False
        new_aliases = list(dict.fromkeys([*existing.aliases, *aliases]))[:MAX_ALIASES_PER_NODE]
        if new_aliases != existing.aliases:
            existing.aliases = new_aliases
            changed = True
        merged_evidence, ev_changed = _dedupe_evidence(existing.evidence_references, evidence_references)
        if ev_changed:
            existing.evidence_references = merged_evidence
            changed = True
        if existing.status != status:
            existing.status = status
            changed = True
        if abs(existing.confidence - confidence) > 1e-9:
            existing.confidence = confidence
            changed = True
        for key, value in attributes.items():
            if existing.attributes.get(key) != value:
                existing.attributes[key] = value
                changed = True
        new_tags = list(dict.fromkeys([*existing.tags, *tags]))
        if new_tags != existing.tags:
            existing.tags = new_tags
            changed = True
        if existing.source_record_version != source_record_version:
            existing.provenance.append(
                GraphProvenance(source_registry=source_registry, source_record_id=source_record_id, source_record_version=source_record_version, synchronized_at_iteration=iteration, synchronized_at=now)
            )
            existing.provenance = existing.provenance[-MAX_PROVENANCE_PER_RECORD:]
            existing.source_record_version = source_record_version
            changed = True
        if existing.stale:
            existing.stale = False
            existing.active = True
            changed = True

        if changed:
            existing.observation_count += 1
            existing.last_seen = now
            existing.graph_version = self.graph_version
            self.updated_node_ids.append(node_id)
            self.dirty_this_pass = True
        return existing

    def mark_node_stale(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None or node.stale:
            return
        node.stale = True
        node.active = False
        node.last_seen = datetime.utcnow()
        self.stale_node_ids.append(node_id)
        self.dirty_this_pass = True

    # -- edges ------------------------------------------------------------------

    def upsert_edge(
        self,
        edge_id: str,
        *,
        edge_type: str,
        source_node_id: str,
        target_node_id: str,
        status: str = "observed",
        confidence: float = 0.0,
        source_registry: str,
        source_record_ids: list[str] | None = None,
        evidence_references: list[GraphEvidenceReference] | None = None,
        attributes: dict | None = None,
        qualifiers: dict[str, str] | None = None,
        temporal_scope: str | None = None,
        actor_scope: str | None = None,
        tenant_scope: str | None = None,
        filter_scope: str | None = None,
        iteration: int = 0,
        now: datetime | None = None,
    ) -> KnowledgeEdge:
        now = now or datetime.utcnow()
        source_record_ids = source_record_ids or []
        evidence_references = evidence_references or []
        attributes = attributes or {}
        qualifiers = qualifiers or {}

        existing = self.edges.get(edge_id)
        if existing is None:
            edge = KnowledgeEdge(
                edge_id=edge_id, edge_type=edge_type, source_node_id=source_node_id, target_node_id=target_node_id,
                status=status, confidence=confidence, source_registry=source_registry, source_record_ids=list(dict.fromkeys(source_record_ids)),
                evidence_references=evidence_references[:MAX_EVIDENCE_PER_RECORD],
                provenance=[GraphProvenance(source_registry=source_registry, source_record_id=(source_record_ids[0] if source_record_ids else ""), synchronized_at_iteration=iteration, synchronized_at=now)],
                attributes=attributes, qualifiers=qualifiers, temporal_scope=temporal_scope, actor_scope=actor_scope,
                tenant_scope=tenant_scope, filter_scope=filter_scope, first_seen=now, last_seen=now, observation_count=1,
                graph_version=self.graph_version, active=True, stale=False,
            )
            self.edges[edge_id] = edge
            self._outgoing.setdefault(source_node_id, set()).add(edge_id)
            self._incoming.setdefault(target_node_id, set()).add(edge_id)
            self.added_edge_ids.append(edge_id)
            self.dirty_this_pass = True
            return edge

        changed = False
        new_source_ids = list(dict.fromkeys([*existing.source_record_ids, *source_record_ids]))
        if new_source_ids != existing.source_record_ids:
            existing.source_record_ids = new_source_ids
            changed = True
        merged_evidence, ev_changed = _dedupe_evidence(existing.evidence_references, evidence_references)
        if ev_changed:
            existing.evidence_references = merged_evidence
            changed = True
        if existing.status != status:
            existing.status = status
            changed = True
        if abs(existing.confidence - confidence) > 1e-9:
            existing.confidence = confidence
            changed = True
        for key, value in attributes.items():
            if existing.attributes.get(key) != value:
                existing.attributes[key] = value
                changed = True
        for key, value in qualifiers.items():
            if existing.qualifiers.get(key) != value:
                existing.qualifiers[key] = value
                changed = True
        for field_name, value in (("temporal_scope", temporal_scope), ("actor_scope", actor_scope), ("tenant_scope", tenant_scope), ("filter_scope", filter_scope)):
            if value is not None and getattr(existing, field_name) != value:
                setattr(existing, field_name, value)
                changed = True
        if existing.stale:
            existing.stale = False
            existing.active = True
            changed = True

        if changed:
            existing.observation_count += 1
            existing.last_seen = now
            existing.graph_version = self.graph_version
            self.updated_edge_ids.append(edge_id)
            self.dirty_this_pass = True
        return existing

    def mark_edge_stale(self, edge_id: str) -> None:
        edge = self.edges.get(edge_id)
        if edge is None or edge.stale:
            return
        edge.stale = True
        edge.active = False
        edge.last_seen = datetime.utcnow()
        self.stale_edge_ids.append(edge_id)
        self.dirty_this_pass = True

    def outgoing_edge_ids(self, node_id: str) -> set[str]:
        return set(self._outgoing.get(node_id, set()))

    def incoming_edge_ids(self, node_id: str) -> set[str]:
        return set(self._incoming.get(node_id, set()))

    # -- gaps / contradictions / consistency / inference ------------------------

    def add_contradiction(self, contradiction: GraphContradiction) -> GraphContradiction:
        key_existing = next(
            (c for c in self.contradictions.values() if c.description == contradiction.description and set(c.node_ids) == set(contradiction.node_ids) and set(c.edge_ids) == set(contradiction.edge_ids)),
            None,
        )
        if key_existing is not None:
            key_existing.last_seen = datetime.utcnow()
            return key_existing
        self.contradictions[contradiction.contradiction_id] = contradiction
        self.new_contradiction_ids.append(contradiction.contradiction_id)
        self.dirty_this_pass = True
        return contradiction

    def add_consistency_issue(self, issue: GraphConsistencyIssue) -> GraphConsistencyIssue:
        existing = next(
            (i for i in self.consistency_issues.values() if i.issue_type == issue.issue_type and set(i.node_ids) == set(issue.node_ids) and set(i.edge_ids) == set(issue.edge_ids)),
            None,
        )
        if existing is not None:
            existing.last_seen = datetime.utcnow()
            return existing
        self.consistency_issues[issue.issue_id] = issue
        self.dirty_this_pass = True
        return issue

    def add_gap(self, gap: GraphGap) -> GraphGap:
        existing = next(
            (g for g in self.gaps.values() if g.gap_type == gap.gap_type and set(g.node_ids) == set(gap.node_ids)),
            None,
        )
        if existing is not None:
            existing.last_seen = datetime.utcnow()
            if existing.status == "resolved":
                # The condition that produced this gap disappeared for at
                # least one pass and has now recurred -- reopen the SAME
                # record rather than leaving it silently resolved.
                existing.status = "open"
                self.dirty_this_pass = True
            return existing
        self.gaps[gap.gap_id] = gap
        self.new_gap_ids.append(gap.gap_id)
        self.dirty_this_pass = True
        return gap

    def resolve_gap(self, gap_id: str, *, resolution: str = "") -> None:
        gap = self.gaps.get(gap_id)
        if gap is not None and gap.status == "open":
            gap.status = "resolved"
            self.resolved_gap_ids_this_pass.append(gap_id)
            self.dirty_this_pass = True

    def close_gaps_not_in(self, still_open_gap_ids: set[str]) -> None:
        """A gap resolves when the condition that produced it no longer
        holds — called once per pass with the FULL set of gap ids the
        analyzer just re-produced; any previously-open gap NOT in that set
        is considered resolved by newer registry evidence."""
        for gap_id, gap in self.gaps.items():
            if gap.status == "open" and gap_id not in still_open_gap_ids:
                self.resolve_gap(gap_id)

    def add_inference(self, inference: GraphInference) -> GraphInference:
        self.inferences[inference.edge_id] = inference
        return inference

    def invalidate_inference(self, edge_id: str, *, reason: str) -> None:
        inference = self.inferences.get(edge_id)
        if inference is not None and not inference.invalidated:
            inference.invalidated = True
            inference.invalidated_reason = reason
            self.mark_edge_stale(edge_id)

    # -- pending references -------------------------------------------------------

    def add_pending_reference(self, ref: PendingReference) -> PendingReference:
        existing = next(
            (r for r in self.pending_references.values() if not r.resolved and r.source_node_id == ref.source_node_id and r.edge_type == ref.edge_type and r.target_hint == ref.target_hint),
            None,
        )
        if existing is not None:
            return existing
        self.pending_references[ref.reference_id] = ref
        self.dirty_this_pass = True
        return ref

    def unresolved_references(self) -> list[PendingReference]:
        return [r for r in self.pending_references.values() if not r.resolved]

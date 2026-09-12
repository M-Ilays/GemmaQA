"""Entity memory — persistence and merging of discovered entities across
observations.

Owns the term -> EntityRecord store. Every page observation's findings are
MERGED here (never appended as duplicates): same normalized term -> same
record; a weakly-evidenced multiword term whose head noun matches an
existing entity is folded in as an alias ("customer profile" -> customer)
rather than becoming a spurious second entity. Confidence is recomputed
from the merged evidence set every time, so it evolves as corroboration
accumulates.
"""

from __future__ import annotations

from datetime import datetime

from app.intelligence.entity_discovery.entity_classifier import PageDiscovery, TermFinding
from app.intelligence.entity_discovery.entity_confidence import (
    MIN_DISTINCT_SOURCES_CONFIRMED,
    score_confidence,
    status_for,
)
from app.intelligence.entity_discovery.entity_relationship_builder import RelationshipFinding
from app.intelligence.entity_discovery.schemas import (
    EntityEvidence,
    EntityOperation,
    EntityRecord,
    EntityRelationship,
)

MAX_EVIDENCE_PER_ENTITY = 60
MAX_ALIASES_PER_ENTITY = 12


class EntityMemory:
    """The mutable store behind EntityRegistry."""

    def __init__(self) -> None:
        self.records: dict[str, EntityRecord] = {}  # keyed by canonical term

    # -- lookup --------------------------------------------------------------

    def resolve_term(self, term: str) -> str | None:
        """Which canonical term (if any) this term belongs to — exact match
        first, then head-noun fold-in for weak multiword terms."""
        if term in self.records:
            return term
        head = term.split()[-1] if term else ""
        if head and head != term and head in self.records:
            return head
        return None

    # -- merging ------------------------------------------------------------

    def merge_discovery(self, discovery: PageDiscovery, *, iteration: int = 0) -> dict[str, list[str]]:
        """Merge one page's findings. Returns a change log for tracing:
        {"created": [...], "updated": [...], "alias_merged": [...]}"""
        changes: dict[str, list[str]] = {"created": [], "updated": [], "alias_merged": []}
        now = datetime.utcnow()

        for term, finding in sorted(discovery.findings.items()):
            target = self._merge_target(term, finding)
            if target != term:
                changes["alias_merged"].append(f"{term} -> {target}")
            record = self.records.get(target)
            if record is None:
                record = EntityRecord(canonical_name=target, first_seen=now, first_seen_iteration=iteration)
                self.records[target] = record
                changes["created"].append(target)
            else:
                changes["updated"].append(target)

            self._merge_finding(record, finding, page_url=discovery.page_url, now=now, iteration=iteration)
        return changes

    def _merge_target(self, term: str, finding: TermFinding) -> str:
        """Exact term match wins. A multiword term with WEAK evidence (fewer
        distinct source kinds than the confirmation threshold) whose head
        noun already exists as an entity folds into that entity as an alias
        — a strongly-evidenced multiword term stays its own entity ("purchase
        order" can be genuinely distinct from "order")."""
        if term in self.records:
            return term
        head = term.split()[-1] if term else term
        if head != term and head in self.records:
            distinct_kinds = {ev.source_kind for ev in finding.evidence}
            if len(distinct_kinds) < MIN_DISTINCT_SOURCES_CONFIRMED:
                return head
        return term

    def _merge_finding(
        self,
        record: EntityRecord,
        finding: TermFinding,
        *,
        page_url: str,
        now: datetime,
        iteration: int,
    ) -> None:
        # Evidence: dedupe by (source_kind, observed_text, page_url) so
        # re-observing the same page doesn't inflate confidence.
        seen = {(ev.source_kind, ev.observed_text, ev.page_url) for ev in record.evidence}
        for ev in finding.evidence:
            key = (ev.source_kind, ev.observed_text, ev.page_url)
            if key in seen or len(record.evidence) >= MAX_EVIDENCE_PER_ENTITY:
                continue
            seen.add(key)
            record.evidence.append(ev)

        for alias in sorted(finding.aliases):
            if alias not in record.aliases and alias.lower() != record.canonical_name:
                if len(record.aliases) < MAX_ALIASES_PER_ENTITY:
                    record.aliases.append(alias)

        by_op = {op.operation: op for op in record.operations}
        for op_name, op_evidence in sorted(finding.operations.items()):
            op = by_op.get(op_name)
            if op is None:
                op = EntityOperation(operation=op_name, evidence=[], confidence=0.5)
                record.operations.append(op)
                by_op[op_name] = op
            existing = {(ev.source_kind, ev.observed_text, ev.page_url) for ev in op.evidence}
            for ev in op_evidence:
                key = (ev.source_kind, ev.observed_text, ev.page_url)
                if key not in existing:
                    existing.add(key)
                    op.evidence.append(ev)
            op.confidence = min(1.0, 0.4 + 0.15 * len({(e.source_kind, e.page_url) for e in op.evidence}))

        if page_url and page_url not in record.related_pages:
            record.related_pages.append(page_url)
        for form_id in sorted(finding.related_form_ids):
            if form_id not in record.related_forms:
                record.related_forms.append(form_id)
        for table_id in sorted(finding.related_table_ids):
            if table_id not in record.related_tables:
                record.related_tables.append(table_id)
        for state in sorted(finding.known_states):
            if state not in record.known_states:
                record.known_states.append(state)

        record.discovered_in = sorted({ev.source_kind for ev in record.evidence})
        record.confidence = score_confidence(record.evidence)
        record.status = status_for(record)
        record.last_seen = now
        record.last_seen_iteration = iteration

    def merge_relationships(self, findings: list[RelationshipFinding]) -> list[str]:
        """Attach relationship observations to their SUBJECT entity records.
        Both endpoints must resolve to known entities. Returns trace lines."""
        traced: list[str] = []
        for finding in findings:
            subject_term = self.resolve_term(finding.subject_term)
            object_term = self.resolve_term(finding.object_term)
            if subject_term is None or object_term is None or subject_term == object_term:
                continue
            subject = self.records[subject_term]
            object_record = self.records[object_term]
            existing = next(
                (
                    r
                    for r in subject.relationships
                    if r.kind == finding.kind and r.object_entity_id == object_record.entity_id
                ),
                None,
            )
            if existing is None:
                subject.relationships.append(
                    EntityRelationship(
                        subject_entity_id=subject.entity_id,
                        kind=finding.kind,
                        object_entity_id=object_record.entity_id,
                        evidence=[finding.evidence],
                        confidence=0.4,
                    )
                )
                traced.append(f"{subject_term} {finding.kind} {object_term}")
            else:
                keys = {(e.source_kind, e.observed_text, e.page_url) for e in existing.evidence}
                key = (finding.evidence.source_kind, finding.evidence.observed_text, finding.evidence.page_url)
                if key not in keys:
                    existing.evidence.append(finding.evidence)
                    existing.confidence = min(1.0, 0.4 + 0.15 * len(existing.evidence))
        return traced

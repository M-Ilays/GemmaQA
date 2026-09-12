"""Entity relationship discovery — records OBSERVED structural relationships
between discovered entities. Deliberately simple (per the phase scope): no
workflow reconstruction, only three generic structural patterns any web
application exhibits:

1. URL nesting: `/customers/{id}/orders` — the outer resource *owns* the
   inner one (and the inner *belongs_to* the outer).
2. Cross-entity table column: a column named after ANOTHER known entity on
   this entity's page ("Customer" column in an orders table) — *references*;
   if the column label carries the generic verb "assigned", *assigned_to*.
3. Cross-entity form field: a field named after another known entity in
   this entity's form ("customer" select in a job form) — *references*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from app.intelligence.entity_discovery.entity_candidate_builder import (
    normalize_term,
    url_path_terms,
)
from app.intelligence.entity_discovery.entity_classifier import PageDiscovery
from app.intelligence.entity_discovery.schemas import EntityEvidence

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel


@dataclass
class RelationshipFinding:
    """One observed relationship between two TERMS (resolved to entity ids
    later, at registry-merge time, once both terms exist as entities)."""

    subject_term: str
    kind: str  # owns | belongs_to | references | assigned_to
    object_term: str
    evidence: EntityEvidence


class EntityRelationshipBuilder:
    def build(
        self,
        model: "CanonicalPageModel",
        discovery: PageDiscovery,
        *,
        known_terms: Iterable[str],
        iteration: int = 0,
    ) -> list[RelationshipFinding]:
        """`known_terms` is every term the registry already tracks (including
        this page's findings, merged first) — a relationship is only recorded
        between two terms that BOTH exist as discovered entities."""
        known = set(known_terms) | set(discovery.findings.keys())
        out: list[RelationshipFinding] = []
        out.extend(self._from_url_nesting(model, known, iteration=iteration))
        out.extend(self._from_table_columns(model, discovery, known, iteration=iteration))
        out.extend(self._from_form_fields(model, discovery, known, iteration=iteration))
        return out

    # -- URL nesting -------------------------------------------------------

    @staticmethod
    def _from_url_nesting(
        model: "CanonicalPageModel", known: set[str], *, iteration: int
    ) -> list[RelationshipFinding]:
        terms = [normalize_term(t) for t in url_path_terms(model.url)]
        entity_terms = [t for t in terms if t in known]
        out: list[RelationshipFinding] = []
        for parent, child in zip(entity_terms, entity_terms[1:]):
            if parent == child:
                continue
            evidence = EntityEvidence(
                source_kind="url_segment",
                observed_text=model.url[:160],
                page_url=model.url,
                state_fingerprint=model.state_fingerprint or "",
                observed_at_iteration=iteration,
            )
            out.append(RelationshipFinding(subject_term=parent, kind="owns", object_term=child, evidence=evidence))
            out.append(
                RelationshipFinding(
                    subject_term=child, kind="belongs_to", object_term=parent, evidence=evidence.model_copy()
                )
            )
        return out

    # -- cross-entity table columns ------------------------------------------

    @staticmethod
    def _from_table_columns(
        model: "CanonicalPageModel", discovery: PageDiscovery, known: set[str], *, iteration: int
    ) -> list[RelationshipFinding]:
        subject = discovery.page_context_term
        if subject is None:
            return []
        out: list[RelationshipFinding] = []
        for table in model.tables or []:
            for header in table.headers or []:
                raw_lower = header.lower()
                normalized = normalize_term(header)
                # Match the column against another entity: either the whole
                # normalized header, or its head noun ("Customer Name" ->
                # "customer name" won't match, but head-scan finds "customer").
                matched = normalized if normalized in known else next(
                    (w for w in normalized.split() if w in known), None
                )
                if not matched or matched == subject:
                    continue
                kind = "assigned_to" if "assign" in raw_lower else "references"
                out.append(
                    RelationshipFinding(
                        subject_term=subject,
                        kind=kind,
                        object_term=matched,
                        evidence=EntityEvidence(
                            source_kind="table_column",
                            observed_text=header[:160],
                            page_url=model.url,
                            state_fingerprint=model.state_fingerprint or "",
                            element_id=table.table_id,
                            observed_at_iteration=iteration,
                        ),
                    )
                )
        return out

    # -- cross-entity form fields ----------------------------------------

    @staticmethod
    def _from_form_fields(
        model: "CanonicalPageModel", discovery: PageDiscovery, known: set[str], *, iteration: int
    ) -> list[RelationshipFinding]:
        subject = discovery.page_context_term
        if subject is None:
            return []
        out: list[RelationshipFinding] = []
        for form in model.forms or []:
            for f in form.fields or []:
                label = f.label or f.name or ""
                if not label:
                    continue
                raw_lower = label.lower()
                normalized = normalize_term(label)
                matched = normalized if normalized in known else next(
                    (w for w in normalized.split() if w in known), None
                )
                if not matched or matched == subject:
                    continue
                kind = "assigned_to" if "assign" in raw_lower else "references"
                out.append(
                    RelationshipFinding(
                        subject_term=subject,
                        kind=kind,
                        object_term=matched,
                        evidence=EntityEvidence(
                            source_kind="form_field",
                            observed_text=label[:160],
                            page_url=model.url,
                            state_fingerprint=model.state_fingerprint or "",
                            element_id=f.element_id,
                            observed_at_iteration=iteration,
                        ),
                    )
                )
        return out

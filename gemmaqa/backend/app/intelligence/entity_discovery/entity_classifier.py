"""Entity classification — turns raw per-page candidates into structured
per-term findings: grouped evidence, inferred operations, related structures,
and observed value-states.

Deterministic. Operations are inferred ONLY from what was actually observed:
a UI control whose label carries a generic action verb ("Add X", "Export"),
or an API call's HTTP method — never from an assumption that an entity
"should" support CRUD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.intelligence.entity_discovery.entity_candidate_builder import (
    HTTP_METHOD_OPERATIONS,
    NON_ENTITY_OPERATIONS,
    OPERATION_VERBS,
    EntityCandidateBuilder,
    normalize_term,
    split_verb_and_noun,
    url_path_terms,
)
from app.intelligence.entity_discovery.schemas import EntityCandidate, EntityEvidence

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel

# Source kinds strong enough to define the PAGE's own subject entity —
# an "Add" button with no noun ("Add", "Save") is attributed to this entity.
_PAGE_CONTEXT_KINDS = ("url_segment", "breadcrumb", "heading", "page_title", "navigation_item")

# Column/field head-words that carry entity VALUE-STATES rather than being
# entities themselves (generic structure vocabulary, part of UI_STOPWORDS).
_STATE_COLUMN_HEADS = frozenset({"status", "state"})

MAX_STATES_PER_ENTITY = 12


@dataclass
class TermFinding:
    """Everything one observation revealed about one candidate term."""

    term: str
    evidence: list[EntityEvidence] = field(default_factory=list)
    aliases: set[str] = field(default_factory=set)
    # canonical operation -> supporting evidence
    operations: dict[str, list[EntityEvidence]] = field(default_factory=dict)
    related_form_ids: set[str] = field(default_factory=set)
    related_table_ids: set[str] = field(default_factory=set)
    known_states: set[str] = field(default_factory=set)


@dataclass
class PageDiscovery:
    """The classified result of one page observation."""

    page_url: str
    state_fingerprint: str
    findings: dict[str, TermFinding] = field(default_factory=dict)
    page_context_term: str | None = None


class EntityClassifier:
    """Groups candidates by normalized term and enriches each with
    operations, related structures, and observed states."""

    def __init__(self) -> None:
        self.candidate_builder = EntityCandidateBuilder()

    def classify(self, model: "CanonicalPageModel", *, iteration: int = 0) -> PageDiscovery:
        candidates = self.candidate_builder.build(model, iteration=iteration)
        discovery = PageDiscovery(page_url=model.url, state_fingerprint=model.state_fingerprint or "")

        for cand in candidates:
            finding = discovery.findings.setdefault(cand.term, TermFinding(term=cand.term))
            finding.evidence.append(cand.evidence)
            finding.aliases.add(cand.raw_term)

        discovery.page_context_term = self._page_context_term(discovery)
        self._attach_structures(model, discovery)
        self._infer_ui_operations(model, discovery, iteration=iteration)
        self._infer_api_operations(model, discovery, iteration=iteration)
        self._collect_states(model, discovery)
        return discovery

    # -- page context -----------------------------------------------------

    @staticmethod
    def _page_context_term(discovery: PageDiscovery) -> str | None:
        """The page's own subject: the term with the most evidence from the
        strong, page-identity sources. Deterministic tie-break by term."""
        best: tuple[int, str] | None = None
        for term, finding in discovery.findings.items():
            strong = sum(1 for ev in finding.evidence if ev.source_kind in _PAGE_CONTEXT_KINDS)
            if strong == 0:
                continue
            key = (strong, term)
            if best is None or key[0] > best[0] or (key[0] == best[0] and term < best[1]):
                best = key
        return best[1] if best else None

    # -- structural linkage -------------------------------------------------

    @staticmethod
    def _attach_structures(model: "CanonicalPageModel", discovery: PageDiscovery) -> None:
        """Tables/forms on a page belong to the page's subject entity — the
        strongest structural association a single observation can make."""
        context = discovery.page_context_term
        if context is None or context not in discovery.findings:
            return
        finding = discovery.findings[context]
        for table in model.tables or []:
            if table.table_id:
                finding.related_table_ids.add(table.table_id)
                finding.evidence.append(
                    EntityEvidence(
                        source_kind="table_region",
                        observed_text=f"table {table.table_id} on this entity's page",
                        page_url=model.url,
                        state_fingerprint=model.state_fingerprint or "",
                        element_id=table.table_id,
                    )
                )
        for form in model.forms or []:
            if form.form_id:
                finding.related_form_ids.add(form.form_id)
                finding.evidence.append(
                    EntityEvidence(
                        source_kind="form_region",
                        observed_text=f"form {form.form_id} on this entity's page",
                        page_url=model.url,
                        state_fingerprint=model.state_fingerprint or "",
                        element_id=form.form_id,
                    )
                )

    # -- operation inference ---------------------------------------------

    def _infer_ui_operations(self, model: "CanonicalPageModel", discovery: PageDiscovery, *, iteration: int) -> None:
        for el in model.interactive_elements or []:
            label = el.accessible_name or el.visible_text or el.text or ""
            if not label:
                continue
            verb, noun = split_verb_and_noun(label)
            if verb is None or verb in NON_ENTITY_OPERATIONS:
                continue
            target_term = None
            if noun:
                normalized = normalize_term(noun)
                if normalized in discovery.findings:
                    target_term = normalized
            if target_term is None:
                # Bare verb ("Save", "Export") — attribute to the page's
                # own subject entity, if one exists.
                target_term = discovery.page_context_term
            if target_term is None or target_term not in discovery.findings:
                continue
            evidence = EntityEvidence(
                source_kind="button_label",
                observed_text=label[:160],
                page_url=model.url,
                state_fingerprint=model.state_fingerprint or "",
                element_id=el.element_id,
                observed_at_iteration=iteration,
            )
            discovery.findings[target_term].operations.setdefault(verb, []).append(evidence)

    def _infer_api_operations(self, model: "CanonicalPageModel", discovery: PageDiscovery, *, iteration: int) -> None:
        for entry in model.network_evidence or []:
            method = (entry.method or "").upper()
            operation = HTTP_METHOD_OPERATIONS.get(method)
            if operation is None:
                continue
            text = entry.text or ""
            parts = text.split(" ", 1)
            entry_url = parts[1] if len(parts) == 2 else text
            terms = [normalize_term(t) for t in url_path_terms(entry_url)]
            # The LAST meaningful segment names the resource the call is about.
            for term in reversed(terms):
                if term in discovery.findings:
                    evidence = EntityEvidence(
                        source_kind="api_endpoint",
                        observed_text=text[:160],
                        page_url=model.url,
                        state_fingerprint=model.state_fingerprint or "",
                        observed_at_iteration=iteration,
                    )
                    discovery.findings[term].operations.setdefault(operation, []).append(evidence)
                    break

    # -- observed value-states ---------------------------------------------

    @staticmethod
    def _collect_states(model: "CanonicalPageModel", discovery: PageDiscovery) -> None:
        """A status/state column's cell values are the entity's observed
        lifecycle states — recorded verbatim from the application."""
        context = discovery.page_context_term
        if context is None or context not in discovery.findings:
            return
        finding = discovery.findings[context]
        for table in model.tables or []:
            headers = [normalize_term(h) for h in (table.headers or [])]
            for idx, header in enumerate(headers):
                head_word = header.split()[-1] if header else ""
                if head_word not in _STATE_COLUMN_HEADS:
                    continue
                for row in table.sample_rows or []:
                    if idx < len(row) and row[idx]:
                        value = str(row[idx]).strip()[:40]
                        if value and len(finding.known_states) < MAX_STATES_PER_ENTITY:
                            finding.known_states.add(value)

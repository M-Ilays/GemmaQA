"""Derived output discovery — finds visible values that may summarize
application data (KPI cards, counters, badges, chart totals, report
summaries, queue counts, ...) from a `CanonicalPageModel`, BEFORE any
dependency reasoning happens.

Application-neutral: every heuristic here is generic layout/structural
vocabulary (a "card_group" region, a table's "Total" row, a chart-classified
image) — never one target application's business terms. Reuses the
Perception Engine's own region classification (`card_group`, per
`REGION_TYPES`) and Image Extractor's chart classification
(`visual_semantic_type == "chart"`) rather than re-deriving either.

Not every number is a KPI — see `_looks_like_non_business_number()` for the
exclusions the task explicitly calls for (navigation numbering, dates,
versions, phone numbers, identifiers, postal codes, decorative/stepper/
pagination numbers).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.intelligence.dependency_discovery.schemas import DependencyEvidence, DerivedOutputDescriptor

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel

_CURRENCY_SYMBOLS = "$€£¥"
_NUMBER_RE = re.compile(
    r"(?P<currency>[" + re.escape(_CURRENCY_SYMBOLS) + r"])?\s*"
    r"(?P<number>\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<percent>%)?"
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_MONTH_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b|\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"^v?\d+\.\d+(\.\d+)?$", re.IGNORECASE)
_PHONE_RE = re.compile(r"\(\d{3}\)\s*\d{3}[-.\s]?\d{4}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")
_ID_LIKE_RE = re.compile(r"^[A-Za-z]{1,6}-\d+$|^#[A-Za-z0-9]+$")
_YEAR_ONLY_RE = re.compile(r"^(19|20)\d{2}$")

_ID_COLUMN_HINTS = ("id", "identifier", "code", "zip", "postal", "phone", "sku", "reference", "ref", "#")
_TOTAL_ROW_HINTS = ("total", "sum", "grand total", "subtotal")
_KPI_CARD_REGION_TYPES = ("card_group",)
_QUEUE_HINTS = ("queue", "backlog", "pending", "unassigned", "unread", "inbox", "open", "in progress")
_ALERT_BADGE_HINTS = ("alert", "warning", "error", "notification", "unread")
# A genuine report/summary FIGURE is a short phrase ("42 jobs completed this
# month"), never several paragraphs of concatenated page chrome -- a
# text_block this long is almost certainly a mis-tagged whole-page/region
# text dump, not a business summary value.
_MAX_SUMMARY_TEXT_LENGTH = 200


def _evidence(source_kind: str, text: str, *, page_url: str, fingerprint: str, element_id: str | None = None) -> DependencyEvidence:
    return DependencyEvidence(
        source_kind=source_kind, observed_text=(text or "")[:160], page_url=page_url,
        state_fingerprint=fingerprint, element_id=element_id,
    )


def _looks_like_non_business_number(text: str) -> bool:
    stripped = text.strip()
    if _DATE_RE.search(stripped) or _MONTH_RE.search(stripped):
        return True
    if _VERSION_RE.match(stripped):
        return True
    if _PHONE_RE.search(stripped):
        return True
    if _ID_LIKE_RE.match(stripped):
        return True
    if _YEAR_ONLY_RE.match(stripped):
        return True
    return False


def parse_numeric_value(text: str) -> tuple[float | None, str | None, str | None]:
    """Returns (parsed_value, unit, format) or (None, None, None) if no safe
    number could be extracted. Never guesses across ambiguous text — the
    first well-formed number/currency/percentage token wins."""
    if not text or _looks_like_non_business_number(text):
        return None, None, None
    match = _NUMBER_RE.search(text)
    if not match:
        return None, None, None
    raw_number = match.group("number")
    try:
        value = float(raw_number.replace(",", "").replace(" ", ""))
    except ValueError:
        return None, None, None
    if match.group("percent"):
        return value, "percent", "percentage"
    if match.group("currency"):
        return value, "currency", "monetary"
    return value, None, "plain"


class OutputCandidateBuilder:
    def build(
        self,
        model: "CanonicalPageModel",
        *,
        current_actor_term: str | None = None,
        tenant_term: str | None = None,
    ) -> list[DerivedOutputDescriptor]:
        url, fp = model.url, model.state_fingerprint or ""
        out: list[DerivedOutputDescriptor] = []

        nav_element_ids = {
            item.element_id
            for region in (model.navigation_regions or [])
            for item in (region.items or [])
            if item.element_id
        }
        tab_element_ids = {
            tab.element_id
            for group in (model.tabs or [])
            for tab in (group.tabs or [])
            if tab.element_id
        }
        pagination_item_ids = {
            iid for pg in (model.pagination or []) for iid in (pg.item_ids or [])
        }
        excluded_ids = nav_element_ids | tab_element_ids | pagination_item_ids

        def add(
            output_type: str, source_kind: str, label: str, raw_value: str, *,
            element_id: str | None = None, region_id: str | None = None,
            nearby_context: list[str] | None = None, is_aggregate: bool = False,
            is_static: bool = False, is_derived_field: bool = False, confidence: float = 0.3,
        ) -> None:
            parsed_value, unit, fmt = parse_numeric_value(raw_value)
            out.append(
                DerivedOutputDescriptor(
                    canonical_label=label.strip()[:160],
                    output_type=output_type,
                    raw_value=raw_value.strip()[:160],
                    parsed_value=parsed_value,
                    unit=unit,
                    format=fmt,
                    region_id=region_id,
                    page_url=url,
                    state_fingerprint=fp,
                    nearby_context=nearby_context or [],
                    current_actor_term=current_actor_term,
                    tenant_term=tenant_term,
                    is_aggregate=is_aggregate,
                    is_static=is_static,
                    is_derived_field=is_derived_field,
                    confidence=confidence,
                    evidence=[_evidence(source_kind, f"{label}: {raw_value}", page_url=url, fingerprint=fp, element_id=element_id)],
                )
            )

        self._kpi_card_regions(model, add, excluded_ids)
        self._badges_and_queues(model, add, excluded_ids)
        self._charts(model, add)
        self._tables(model, add)
        self._reports_and_summaries(model, add)
        self._derived_form_fields(model, add)
        return out

    # -- KPI cards: a card_group region containing a heading + a number ------

    @staticmethod
    def _kpi_card_regions(model, add, excluded_ids: set[str]) -> None:
        card_region_ids = {r.stable_id for r in (model.regions or []) if r.region_type in _KPI_CARD_REGION_TYPES}
        if not card_region_ids:
            return
        headings_by_region: dict[str, list] = {}
        for h in model.headings or []:
            if h.parent_region_id in card_region_ids:
                headings_by_region.setdefault(h.parent_region_id, []).append(h)
        texts_by_region: dict[str, list] = {}
        for tb in model.text_blocks or []:
            if tb.parent_region_id in card_region_ids:
                texts_by_region.setdefault(tb.parent_region_id, []).append(tb)
        for el in model.interactive_elements or []:
            if el.element_id and el.element_id in excluded_ids:
                continue
            region_id = getattr(el, "parent_region_id", None)
            if region_id in card_region_ids:
                texts_by_region.setdefault(region_id, []).append(el)

        for region_id in card_region_ids:
            headings = headings_by_region.get(region_id, [])
            candidates = texts_by_region.get(region_id, [])
            label = (headings[0].text or headings[0].accessible_name or "") if headings else ""
            for cand in candidates:
                raw = getattr(cand, "text", None) or getattr(cand, "accessible_name", None) or ""
                if not raw or not any(ch.isdigit() for ch in raw):
                    continue
                if _looks_like_non_business_number(raw):
                    continue
                element_id = getattr(cand, "element_id", None)
                if element_id and element_id in excluded_ids:
                    continue
                add(
                    "kpi_card", "heading", label or raw, raw,
                    element_id=element_id, region_id=region_id,
                    nearby_context=[h.text or "" for h in headings],
                    is_aggregate=True, confidence=0.5 if label else 0.35,
                )

    # -- badges/queues: numeric text near a queue/alert/notification hint ---

    @staticmethod
    def _badges_and_queues(model, add, excluded_ids: set[str]) -> None:
        haystacks = [
            (h.text or h.accessible_name or "", getattr(h, "element_id", None), getattr(h, "parent_region_id", None))
            for h in (model.headings or [])
        ] + [
            (el.accessible_name or el.text or "", el.element_id, getattr(el, "parent_region_id", None))
            for el in (model.interactive_elements or [])
        ] + [
            (item.text or "", item.element_id, None)
            for region in (model.navigation_regions or []) for item in (region.items or [])
        ]
        for text, element_id, region_id in haystacks:
            if not text or not any(ch.isdigit() for ch in text):
                continue
            if element_id and element_id in excluded_ids:
                continue
            if _looks_like_non_business_number(text):
                continue
            lowered = text.lower()
            if any(h in lowered for h in _ALERT_BADGE_HINTS):
                add("notification_count", "interactive_element", text, text, element_id=element_id, region_id=region_id, confidence=0.4)
            elif any(h in lowered for h in _QUEUE_HINTS):
                add("queue_count", "interactive_element", text, text, element_id=element_id, region_id=region_id, is_aggregate=True, confidence=0.4)

        for alert in model.alerts or []:
            text = alert.text or ""
            if text and any(ch.isdigit() for ch in text) and not _looks_like_non_business_number(text):
                add("alert_count", "alert", text, text, element_id=getattr(alert, "element_id", None), confidence=0.35)

    # -- charts: image classified as a chart by the (deterministic) image ----
    # extractor, or a chart-shaped table with a legend-like header set -------

    @staticmethod
    def _charts(model, add) -> None:
        for img in model.images or []:
            if img.visual_semantic_type != "chart":
                continue
            label = img.alt_text or img.surrounding_text or "chart"
            add(
                "chart_total", "chart_image", label, "", element_id=getattr(img, "element_id", None),
                nearby_context=[img.surrounding_text or ""], is_aggregate=True, confidence=0.3,
            )

    # -- tables: a totals/summary row, or the table's own row_count ----------

    @staticmethod
    def _tables(model, add) -> None:
        for table in model.tables or []:
            headers = [h or "" for h in (table.headers or [])]
            id_like_headers = {h.lower() for h in headers if any(hint in h.lower() for hint in _ID_COLUMN_HINTS)}
            for row in table.sample_rows or []:
                first_cell = (row[0] if row else "").strip().lower()
                if first_cell in _TOTAL_ROW_HINTS:
                    for header, cell in zip(headers, row):
                        if header.lower() in id_like_headers or not cell or not any(ch.isdigit() for ch in cell):
                            continue
                        if _looks_like_non_business_number(cell):
                            continue
                        add(
                            "table_total", "table_total", f"{header} {first_cell}", cell,
                            element_id=table.table_id, is_aggregate=True, confidence=0.45,
                        )
            if table.row_count and not any(h.lower() in _TOTAL_ROW_HINTS for h in headers):
                add(
                    "row_count_label", "table_header", f"{table.table_id} row count", str(table.row_count),
                    element_id=table.table_id, is_aggregate=True, confidence=0.2,
                )

    # -- reports/summaries: heading classified as summary/description + a ---
    # nearby numeric text_block -----------------------------------------------

    @staticmethod
    def _reports_and_summaries(model, add) -> None:
        for tb in model.text_blocks or []:
            if tb.block_type not in {"summary", "description"}:
                continue
            text = tb.text or ""
            if not text or not any(ch.isdigit() for ch in text) or _looks_like_non_business_number(text):
                continue
            # A genuine summary is prose (a sentence, maybe wrapping once) --
            # several stacked newline-separated fragments ("Open Menu\nYour
            # Cart\nCheckout\n...") is nav/footer chrome concatenated into
            # one block, not a business summary figure, however short.
            if text.count("\n") > 1:
                continue
            if len(text) > _MAX_SUMMARY_TEXT_LENGTH:
                continue
            add(
                "report_summary", "text_block", text, text,
                element_id=getattr(tb, "element_id", None), region_id=getattr(tb, "parent_region_id", None),
                is_aggregate=True, confidence=0.3,
            )

    # -- derived/read-only form fields ---------------------------------------

    @staticmethod
    def _derived_form_fields(model, add) -> None:
        for form in model.forms or []:
            for field_ in form.field_descriptors or []:
                # A disabled (not enabled) field carrying a value is the only
                # generic, application-neutral signal available today for
                # "this is a read-only, system-calculated field" — there is
                # no explicit readonly/computed flag in FormFieldDescriptor.
                if getattr(field_, "is_enabled", True):
                    continue
                label = field_.label or field_.accessible_name or ""
                value = field_.current_value or ""
                if not value or not any(ch.isdigit() for ch in value) or _looks_like_non_business_number(value):
                    continue
                add(
                    "derived_form_field", "interactive_element", label, value,
                    element_id=getattr(field_, "element_id", None), is_derived_field=True, confidence=0.35,
                )

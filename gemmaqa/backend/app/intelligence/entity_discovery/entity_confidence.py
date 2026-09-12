"""Entity confidence scoring — deterministic, corroboration-based.

Confidence is a function of HOW MANY DISTINCT KINDS of structural evidence
corroborate an entity, not how often one kind repeats: a term seen once each
in the navigation, a table, and a URL is far stronger evidence of a real
business entity than the same word appearing twenty times in body text.
"""

from __future__ import annotations

from app.intelligence.entity_discovery.schemas import EntityEvidence, EntityRecord

# How strongly each evidence source kind suggests "this term is a business
# entity" — structural weights, not word meanings. Navigation/breadcrumbs/
# API endpoints are the strongest signals (applications name their nav and
# routes after their entities); loose visible text is the weakest.
SOURCE_KIND_WEIGHTS: dict[str, float] = {
    "navigation_item": 0.30,
    "breadcrumb": 0.25,
    "api_endpoint": 0.30,
    "url_segment": 0.25,
    "table_region": 0.25,
    "page_title": 0.20,
    "heading": 0.20,
    "tab": 0.20,
    "form_region": 0.20,
    "dialog": 0.15,
    "card": 0.15,
    "button_label": 0.15,
    "link_label": 0.10,
    "table_column": 0.10,
    "form_field": 0.10,
    "network_response": 0.15,
    "aria_label": 0.10,
    "visible_text": 0.05,
    "memory": 0.05,
}

# An entity is only CONFIRMED once corroborated by at least this many
# DISTINCT source kinds — a single source, however loud, stays a candidate.
MIN_DISTINCT_SOURCES_CONFIRMED = 2
# Just below nav(0.30)+table_column(0.10)+one repeat increment: a term named
# in the navigation AND appearing as another entity's column is real.
CONFIRMED_MIN_CONFIDENCE = 0.4


def score_confidence(evidence: list[EntityEvidence]) -> float:
    """Sum the weight of each DISTINCT source kind (repeats of the same kind
    add only a small increment), clamped to [0, 1]. Deterministic."""
    seen_kinds: set[str] = set()
    total = 0.0
    for ev in evidence:
        weight = SOURCE_KIND_WEIGHTS.get(ev.source_kind, 0.05)
        if ev.source_kind in seen_kinds:
            total += weight * 0.15  # diminishing return for repeats of one kind
        else:
            total += weight
            seen_kinds.add(ev.source_kind)
    return min(1.0, total)


def status_for(record: EntityRecord) -> str:
    """Lifecycle status from evidence breadth + completeness. `incomplete`
    means confirmed-but-thin: real, but with no operations or no related
    pages discovered yet — exactly the set the Planner should send more
    exploration toward."""
    distinct_kinds = {ev.source_kind for ev in record.evidence}
    if len(distinct_kinds) < MIN_DISTINCT_SOURCES_CONFIRMED or record.confidence < CONFIRMED_MIN_CONFIDENCE:
        return "candidate"
    if not record.operations or not record.related_pages:
        return "incomplete"
    return "confirmed"

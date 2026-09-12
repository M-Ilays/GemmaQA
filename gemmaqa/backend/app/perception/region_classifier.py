"""Region classification — assigns generic, application-neutral region types to
landmark containers and synthesizes regions for structures that aren't
markup-landmarks at all (forms, tables, dialogs, card groups, icon-only
utility clusters).

Every rule here is structural (tag, ARIA role, position, text length) — never
a business word. Low-confidence guesses (e.g. "legal_region") are marked with
a correspondingly lower `confidence` rather than asserted outright.
"""

from __future__ import annotations

from app.perception.dom_extractor import RawObservation
from app.perception.models import PageRegion, VisualRegion
from app.schemas import InteractiveElement

PRIMARY_NAV_MAX_Y = 150.0
SIDEBAR_NAV_MAX_X = 250.0
SIDEBAR_NAV_MAX_WIDTH = 320.0
LEGAL_LINK_AVG_LEN = 20.0
MIN_CARD_GROUP_SIZE = 2
MIN_UTILITY_CLUSTER_SIZE = 2


def _classify_landmark(region_raw: dict, elements_in_region: list[dict]) -> tuple[str, float]:
    tag = (region_raw.get("tag") or "").lower()
    role = (region_raw.get("role") or "").lower()
    box = region_raw.get("bounding_box") or {}

    if tag == "header" or role == "banner":
        return "header", 0.9
    if tag == "footer" or role == "contentinfo":
        link_texts = [e.get("text") or "" for e in elements_in_region if e.get("category") == "link"]
        if link_texts and (sum(len(t) for t in link_texts) / len(link_texts)) < LEGAL_LINK_AVG_LEN:
            return "legal_region", 0.5  # structural guess (short link rows), stated as lower confidence
        return "footer", 0.85
    if tag == "main" or role == "main":
        return "main_content", 0.9
    if tag == "aside" or role == "complementary":
        return "sidebar", 0.85
    if tag == "nav" or role == "navigation":
        y = float(box.get("y", 0) or 0)
        x = float(box.get("x", 0) or 0)
        width = float(box.get("width", 9999) or 9999)
        if y < PRIMARY_NAV_MAX_Y:
            return "primary_navigation", 0.75
        if x < SIDEBAR_NAV_MAX_X and width < SIDEBAR_NAV_MAX_WIDTH:
            return "secondary_navigation", 0.65
        return "content_navigation", 0.55
    return "unknown_region", 0.3


def classify_landmark_regions(raw: RawObservation) -> list[PageRegion]:
    elements_by_region: dict[str, list[dict]] = {}
    for el in raw.elements:
        region_id = el.get("landmark_region_id")
        if region_id:
            elements_by_region.setdefault(region_id, []).append(el)

    regions: list[PageRegion] = []
    for region_raw in raw.regions_raw:
        region_id = region_raw.get("dom_id")
        if not region_id:
            continue
        members = elements_by_region.get(region_id, [])
        region_type, confidence = _classify_landmark(region_raw, members)
        regions.append(
            PageRegion(
                stable_id=region_id,
                element_id=region_id,
                region_type=region_type,
                dom_tag=region_raw.get("tag"),
                aria_role=region_raw.get("role"),
                accessible_name=region_raw.get("aria_label"),
                text=region_raw.get("text_snippet"),
                bounding_box=region_raw.get("bounding_box"),
                landmark_role=region_raw.get("role") or region_raw.get("tag"),
                child_region_ids=[e.get("dom_id") for e in members if e.get("dom_id")],
                source="dom",
                confidence=confidence,  # type: ignore[arg-type]
            )
        )
    return regions


def synthesize_structural_regions(raw: RawObservation) -> list[PageRegion]:
    """Regions for structures that aren't markup landmarks: one per form, one
    per table, one per dialog — so `form_region`/`data_table`/`dialog` are
    always representable even on a page with no `<nav>`/`<aside>` at all."""
    regions: list[PageRegion] = []
    for form in raw.forms:
        dom_id = form.get("dom_id")
        if not dom_id:
            continue
        regions.append(
            PageRegion(
                stable_id=dom_id,
                element_id=dom_id,
                region_type="form_region",
                dom_tag="form",
                bounding_box=form.get("bounding_box"),
                child_region_ids=[f.get("dom_id") for f in (form.get("fields") or []) if f.get("dom_id")],
                source="dom",
                confidence=0.8,  # type: ignore[arg-type]
            )
        )
    for table in raw.tables:
        dom_id = table.get("dom_id")
        if not dom_id:
            continue
        regions.append(
            PageRegion(
                stable_id=dom_id,
                element_id=dom_id,
                region_type="data_table",
                dom_tag="table",
                bounding_box=table.get("bounding_box"),
                source="dom",
                confidence=0.8,  # type: ignore[arg-type]
            )
        )
    for dialog in raw.dialogs:
        dom_id = dialog.get("dom_id")
        if not dom_id:
            continue
        regions.append(
            PageRegion(
                stable_id=dom_id,
                element_id=dom_id,
                region_type="dialog",
                dom_tag=dialog.get("role") or "dialog",
                aria_role=dialog.get("role"),
                text=dialog.get("text"),
                child_region_ids=list(dialog.get("contained_element_ids") or []),
                source="dom",
                confidence=0.85,  # type: ignore[arg-type]
            )
        )
    return regions


def synthesize_card_group_region(interactive_elements: list[InteractiveElement]) -> list[PageRegion]:
    """A single `card_group` region wrapping every element the interactive
    detector flagged as a "navigable card" (no semantic markup, just a pointer
    cursor and a substantial footprint) — so a grid of non-semantic tiles is
    represented as one region instead of silently scattered, unrelated hits."""
    cards = [e for e in interactive_elements if "navigable_card" in (e.evidence or [])]
    if len(cards) < MIN_CARD_GROUP_SIZE:
        return []
    return [
        PageRegion(
            region_type="card_group",
            child_region_ids=[c.element_id for c in cards],
            source="heuristic",
            confidence=0.5,  # type: ignore[arg-type]
        )
    ]


def synthesize_utility_region(raw: RawObservation) -> list[PageRegion]:
    """A cluster of icon-only controls (search/settings/user-menu style
    affordances) with no other structural classification — represented once,
    generically, as `utility_region`."""
    icon_only = [
        el
        for el in raw.elements
        if el.get("looks_icon_only") and el.get("is_visible", True) and not el.get("landmark_region_id")
    ]
    if len(icon_only) < MIN_UTILITY_CLUSTER_SIZE:
        return []
    return [
        PageRegion(
            region_type="utility_region",
            child_region_ids=[e["dom_id"] for e in icon_only if e.get("dom_id")],
            source="heuristic",
            confidence=0.4,  # type: ignore[arg-type]
        )
    ]


def _layout_hint_for(box: dict, *, viewport_width: float, viewport_height: float) -> str:
    x = float(box.get("x", 0) or 0)
    y = float(box.get("y", 0) or 0)
    width = float(box.get("width", 0) or 0)
    height = float(box.get("height", 0) or 0)
    cx, cy = x + width / 2, y + height / 2
    if cy < viewport_height * 0.15:
        return "top"
    if cy > viewport_height * 0.85:
        return "bottom"
    if cx < viewport_width * 0.2:
        return "left"
    if cx > viewport_width * 0.8:
        return "right"
    return "center"


def classify_visual_regions(
    raw: RawObservation, *, viewport_width: float = 1280.0, viewport_height: float = 800.0
) -> list[VisualRegion]:
    """Pure bounding-box-geometry clustering (no markup/ARIA signal at all) —
    every visible element is bucketed into a coarse layout zone. Deliberately
    simple: this is a fallback signal for pages with no semantic landmarks,
    not a layout-detection algorithm."""
    buckets: dict[str, list[str]] = {"top": [], "bottom": [], "left": [], "right": [], "center": []}
    for el in raw.elements:
        dom_id = el.get("dom_id")
        box = el.get("bounding_box")
        if not dom_id or not box or not el.get("is_visible", True):
            continue
        hint = _layout_hint_for(box, viewport_width=viewport_width, viewport_height=viewport_height)
        buckets[hint].append(dom_id)

    return [
        VisualRegion(layout_hint=hint, member_element_ids=ids, source="heuristic", confidence=0.3)  # type: ignore[arg-type]
        for hint, ids in buckets.items()
        if ids
    ]

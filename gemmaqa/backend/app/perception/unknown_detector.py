"""Unknown-component detection — the audit's "no fallback bucket" fix.

Every other extractor records which `dom_id`s it successfully classified
(a link, a form field, a table action, an image, ...). Anything visible that
made it into `RawObservation.elements` at all (meaning the raw DOM walk found
SOME reason to notice it — a native tag, an ARIA attribute, a tabindex, or a
`cursor: pointer` style) but that no other extractor claimed is preserved here
as an `UnknownComponent` instead of silently vanishing.
"""

from __future__ import annotations

from app.perception.dom_extractor import RawObservation
from app.perception.models import UnknownComponent


def _reason_for(el: dict) -> str:
    reasons = []
    if el.get("tabindex") is not None:
        reasons.append(f"tabindex={el['tabindex']}")
    if el.get("aria_haspopup"):
        reasons.append("aria-haspopup present")
    if el.get("aria_expanded") is not None:
        reasons.append("aria-expanded present")
    if (el.get("cursor_style") or "").lower() == "pointer":
        reasons.append("cursor:pointer")
    if el.get("has_onclick_attr"):
        reasons.append("onclick attribute/property present")
    if not reasons:
        reasons.append("matched raw interactive-candidate selector")
    return "; ".join(reasons)


def detect_unknown_components(
    raw: RawObservation, claimed_dom_ids: set[str]
) -> list[UnknownComponent]:
    unknown: list[UnknownComponent] = []
    for el in raw.elements:
        dom_id = el.get("dom_id")
        if not dom_id or dom_id in claimed_dom_ids:
            continue
        if not el.get("is_visible", True):
            continue
        unknown.append(
            UnknownComponent(
                element_id=dom_id,
                stable_id=dom_id,
                text=el.get("text"),
                dom_tag=el.get("tag"),
                aria_role=el.get("role"),
                bounding_box=el.get("bounding_box"),
                parent_region_id=el.get("landmark_region_id"),
                detection_reason=_reason_for(el),
                source="heuristic",
                confidence=0.3,
                status="unknown",
            )
        )
    return unknown

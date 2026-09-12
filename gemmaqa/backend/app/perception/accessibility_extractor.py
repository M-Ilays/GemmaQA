"""Accessibility extraction — normalizes the raw per-element ARIA/accessibility
facts `dom_extractor` already collected into a clean, typed structure, applies
role-inference fallbacks, and builds the parent-child relationship map other
extractors use to set `parent_region_id` consistently.

Deterministic: every value here is either taken directly from a raw attribute
or derived through a fixed, stated fallback rule (e.g. implied ARIA role from
tag+type) — never guessed from page content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.browser.locators import _implied_role
from app.perception.dom_extractor import RawObservation


@dataclass(frozen=True)
class AccessibilityInfo:
    """Normalized accessibility facts for one raw element (keyed by `dom_id`)."""

    dom_id: str
    role: Optional[str]
    accessible_name: Optional[str]
    accessible_description: Optional[str]
    is_expanded: Optional[bool]
    is_selected: Optional[bool]
    is_checked: Optional[bool]
    is_disabled: bool
    is_required: bool
    heading_level: Optional[int]
    parent_dom_id: Optional[str]


def _is_current_truthy(value: Any) -> Optional[bool]:
    """`aria-current` is a token attribute (`page`/`step`/`true`/...), not a
    plain boolean — any non-empty, non-"false" value means "this is current"."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text != "false"


def build_accessibility_index(raw: RawObservation) -> dict[str, AccessibilityInfo]:
    """One `AccessibilityInfo` per raw element, keyed by `dom_id`. Heading level
    is joined in from `raw.headings` when a heading and an interactive/structural
    element happen to share a `dom_id` (e.g. a heading that is also a disclosure
    trigger); parent-child linkage uses `landmark_region_id` — the closest
    ancestor landmark region computed once, in the browser, by `dom_extractor`."""
    heading_levels = {h.get("dom_id"): h.get("level") for h in raw.headings if h.get("dom_id")}

    index: dict[str, AccessibilityInfo] = {}
    for el in raw.elements:
        dom_id = el.get("dom_id")
        if not dom_id:
            continue
        tag = el.get("tag") or ""
        input_type = el.get("type")
        role = el.get("role") or _implied_role(tag, input_type)
        selected = el.get("aria_selected")
        if selected is None:
            selected = _is_current_truthy(el.get("aria_current"))
        index[dom_id] = AccessibilityInfo(
            dom_id=dom_id,
            role=role,
            accessible_name=el.get("accessible_name") or el.get("aria_label") or el.get("label"),
            accessible_description=el.get("accessible_description"),
            is_expanded=el.get("aria_expanded"),
            is_selected=selected,
            is_checked=el.get("checked"),
            is_disabled=bool(el.get("disabled")) or not bool(el.get("is_enabled", True)),
            is_required=bool(el.get("required")),
            heading_level=heading_levels.get(dom_id),
            parent_dom_id=el.get("landmark_region_id"),
        )
    return index


def build_parent_child_map(raw: RawObservation) -> dict[str, str]:
    """dom_id -> nearest ancestor landmark region's dom_id, for every element
    that has one. Used by region_classifier/engine to set `parent_region_id`
    generically across every descriptor type."""
    return {
        el["dom_id"]: el["landmark_region_id"]
        for el in raw.elements
        if el.get("dom_id") and el.get("landmark_region_id")
    }

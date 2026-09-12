"""Interactive detection — classifies raw elements as interactive using more
than "is it a native `<button>`/`<a>`", per docs/PAGE_PERCEPTION_AUDIT.md's
"non-standard clickable elements are invisible" finding.

Signals combined (deterministic — same input always yields the same output):
native interactive tag/role, ARIA interactive roles, `tabindex`, an `onclick`
attribute/property, a `cursor: pointer` computed style (the strongest generic
signal for framework-attached click handlers `addEventListener` makes
otherwise invisible), `aria-expanded`/`aria-haspopup` presence, icon-only
controls, table-row action buttons, and "card" containers that look navigable.

Every element this module accepts becomes an `InteractiveElement` (reused,
unmodified, from `app.schemas`) with `source` recording which signal(s) fired
and `evidence` naming them — never a bare, unexplained classification.
"""

from __future__ import annotations

from app.perception.accessibility_extractor import AccessibilityInfo
from app.perception.dom_extractor import RawObservation
from app.perception.evidence_merger import merge_duplicate_elements
from app.schemas import InteractiveElement, LocatorStrategy

NATIVE_INTERACTIVE_TAGS = frozenset({"a", "button", "input", "select", "textarea", "summary"})
ARIA_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "tab",
        "menuitem",
        "checkbox",
        "radio",
        "switch",
        "combobox",
        "option",
        "textbox",
    }
)
MIN_CARD_WIDTH = 80.0
MIN_CARD_HEIGHT = 80.0


def _detect_signals(el: dict, *, row_action_ids: set[str]) -> list[str]:
    """Which detection rule(s) fired for this raw element, in a stable,
    deterministic order. Empty means "not detected as interactive"."""
    signals: list[str] = []
    tag = (el.get("tag") or "").lower()
    role = (el.get("role") or "").lower()

    if tag in NATIVE_INTERACTIVE_TAGS:
        signals.append("native_tag")
    if role in ARIA_INTERACTIVE_ROLES:
        signals.append("aria_role")
    tabindex = el.get("tabindex")
    if tabindex is not None and tabindex >= 0:
        signals.append("tabindex")
    if el.get("has_onclick_attr"):
        signals.append("click_handler_attribute")
    if (el.get("cursor_style") or "").lower() == "pointer" and tag not in NATIVE_INTERACTIVE_TAGS:
        signals.append("pointer_style")
    if el.get("aria_expanded") is not None:
        signals.append("expandable_control")
    if el.get("aria_haspopup"):
        signals.append("custom_dropdown_trigger")
    if el.get("dom_id") in row_action_ids:
        signals.append("table_row_action")
    if _looks_like_navigable_card(el, signals):
        signals.append("navigable_card")
    return signals


def _looks_like_navigable_card(el: dict, signals_so_far: list[str]) -> bool:
    """A container with no NATIVE/ARIA semantic interactivity but a pointer
    cursor and a substantial footprint — the generic "clickable card" pattern
    (a product tile, a list item that navigates on click). This is additive to
    `pointer_style` (both can fire for the same element) — it specifically
    flags the subset of pointer-style elements large enough to plausibly be a
    whole navigable card rather than a small icon/toggle."""
    if "native_tag" in signals_so_far or "aria_role" in signals_so_far:
        return False  # already semantically classified; not a bare "card"
    if (el.get("cursor_style") or "").lower() != "pointer":
        return False
    box = el.get("bounding_box") or {}
    return float(box.get("width", 0) or 0) >= MIN_CARD_WIDTH and float(box.get("height", 0) or 0) >= MIN_CARD_HEIGHT


def _is_icon_only(el: dict) -> bool:
    return bool(el.get("looks_icon_only"))


def _element_key(el: InteractiveElement) -> str:
    return el.element_id


def detect_interactive_elements(
    raw: RawObservation,
    accessibility_index: dict[str, AccessibilityInfo],
) -> list[InteractiveElement]:
    row_action_ids: set[str] = set()
    for table in raw.tables:
        for row in table.get("rows") or []:
            row_action_ids.update(row.get("row_action_ids") or [])

    detected: list[InteractiveElement] = []
    for el in raw.elements:
        dom_id = el.get("dom_id")
        if not dom_id or not el.get("is_visible", True):
            continue
        signals = _detect_signals(el, row_action_ids=row_action_ids)
        if not signals:
            continue

        info = accessibility_index.get(dom_id)
        attributes: dict[str, str] = {}
        haspopup = el.get("aria_haspopup")
        if haspopup:
            attributes["aria-haspopup"] = str(haspopup)
        source = "aria" if "aria_role" in signals and "native_tag" not in signals else (
            "heuristic" if signals and signals[0] in {"pointer_style", "navigable_card"} else "dom"
        )
        confidence = 1.0 if "native_tag" in signals or "aria_role" in signals else 0.6

        detected.append(
            InteractiveElement(
                element_id=dom_id,
                tag=el.get("tag") or "div",
                role=(info.role if info else el.get("role")),
                type=el.get("type"),
                input_type=el.get("type"),
                name=el.get("name"),
                id_attr=el.get("id_attr"),
                text=el.get("text"),
                visible_text=el.get("text"),
                aria_label=el.get("aria_label"),
                accessible_name=(info.accessible_name if info else el.get("accessible_name")),
                accessible_description=(info.accessible_description if info else None),
                label=el.get("label"),
                href=el.get("href"),
                placeholder=el.get("placeholder"),
                title=el.get("title_attr"),
                is_visible=bool(el.get("is_visible", True)),
                is_enabled=not (info.is_disabled if info else bool(el.get("disabled"))),
                required=(info.is_required if info else bool(el.get("required"))),
                disabled=(info.is_disabled if info else bool(el.get("disabled"))),
                checked=(info.is_checked if info else el.get("checked")),
                current_value=el.get("current_value"),
                available_options=list(el.get("available_options") or []),
                bounding_box=el.get("bounding_box"),
                selector_hint=el.get("selector_hint"),
                locator_strategy=LocatorStrategy(kind="css", selector=el.get("selector_hint") or ""),
                category=el.get("category"),
                stable_id=dom_id,
                parent_region_id=el.get("landmark_region_id"),
                source=source,
                attributes=attributes,
                is_expanded=(info.is_expanded if info else el.get("aria_expanded")),
                is_selected=(info.is_selected if info else el.get("aria_selected")),
                is_external_url=None,
                confidence=confidence,
                evidence=list(signals),
                status="observed",
                fingerprint_contribution=None,
            )
        )

    return merge_duplicate_elements(detected, _element_key)


def is_icon_only(el: dict) -> bool:
    """Exposed for `unknown_detector`/tests — whether a raw element looks like an
    icon-only control (no text, contains an svg/img or an icon-ish class)."""
    return _is_icon_only(el)

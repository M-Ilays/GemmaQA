"""Action semantics — classifies WHAT VERB an interactive control performs,
independent of whether it carries visible text.

Signal priority (never text alone — an icon-only "Delete" trash-can button
with `aria-label="Delete"` and zero visible text must still resolve to
`delete`):

1. Visible text (first word, then whole-text substring match)
2. `aria-label`
3. `title` attribute (tooltip)
4. Icon-only + `aria-haspopup` -> kebab/contextual menu (`more_actions`)
5. Icon-only + fixed/absolute position -> floating action button (verb
   still resolved from whatever text/aria-label IS present; the FAB flag is
   independent metadata, not a verb substitute)

Every classification is application-neutral: the keyword table below is
fixed UI vocabulary ("save", "delete", "next", ...), never a business term.
"""

from __future__ import annotations

import re
from typing import Any

from app.perception.models import ActionSemantics

# Ordered so a more specific multi-word phrase never loses to a shorter
# substring of itself (e.g. "add" must not swallow "address").
_VERB_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("delete", re.compile(r"\b(delete|trash|discard)\b", re.IGNORECASE)),
    ("remove", re.compile(r"\bremove\b", re.IGNORECASE)),
    ("add", re.compile(r"\badd\b", re.IGNORECASE)),
    ("create", re.compile(r"\bcreate\b", re.IGNORECASE)),
    ("new", re.compile(r"\bnew\b", re.IGNORECASE)),
    ("save", re.compile(r"\bsave\b", re.IGNORECASE)),
    ("submit", re.compile(r"\bsubmit\b", re.IGNORECASE)),
    ("update", re.compile(r"\bupdate\b", re.IGNORECASE)),
    ("edit", re.compile(r"\bedit\b", re.IGNORECASE)),
    ("confirm", re.compile(r"\b(confirm|proceed|yes)\b", re.IGNORECASE)),
    ("cancel", re.compile(r"\bcancel\b", re.IGNORECASE)),
    ("search", re.compile(r"\bsearch\b", re.IGNORECASE)),
    ("filter", re.compile(r"\bfilter\b", re.IGNORECASE)),
    ("reset", re.compile(r"\b(reset|clear)\b", re.IGNORECASE)),
    ("view", re.compile(r"\b(view|details|inspect)\b", re.IGNORECASE)),
    ("open", re.compile(r"\bopen\b", re.IGNORECASE)),
    ("next", re.compile(r"^(next|>|›)$", re.IGNORECASE)),
    ("previous", re.compile(r"^(previous|prev|back|<|‹)$", re.IGNORECASE)),
]

_MORE_ACTIONS_HINT = re.compile(r"\b(more|options|actions|menu)\b", re.IGNORECASE)


def _match_verb(text: str) -> str | None:
    if not text:
        return None
    for verb, pattern in _VERB_PATTERNS:
        if pattern.search(text):
            return verb
    return None


def _classify_one(el: dict[str, Any]) -> ActionSemantics:
    dom_id = el.get("dom_id") or ""
    text = (el.get("text") or "").strip()
    aria_label = (el.get("aria_label") or "").strip()
    title_attr = (el.get("title_attr") or "").strip()
    icon_only = bool(el.get("looks_icon_only"))
    has_svg = bool(el.get("has_inline_svg"))
    haspopup = el.get("aria_haspopup")
    position = (el.get("position_style") or "").lower()
    box = el.get("bounding_box") or {}

    signals: list[str] = []
    verb: str | None = None

    if text:
        verb = _match_verb(text)
        if verb:
            signals.append("visible_text")
    if verb is None and aria_label:
        verb = _match_verb(aria_label)
        if verb:
            signals.append("aria_label")
    if verb is None and title_attr:
        verb = _match_verb(title_attr)
        if verb:
            signals.append("title_attribute")

    is_kebab = False
    if verb is None and haspopup:
        # `aria-haspopup` + no recognized verb is a contextual-menu trigger
        # whether it's icon-only (a bare kebab glyph) or carries a generic
        # non-verb label ("Actions", "More", "..." ) — a real row-actions
        # dropdown routinely has visible text, not just an icon.
        is_kebab = True
        signals.append("kebab_menu")
        if _MORE_ACTIONS_HINT.search(" ".join(filter(None, [text, aria_label, title_attr]))):
            signals.append("contextual_menu")
        verb = "more_actions"

    is_fab = False
    width = float(box.get("width", 0) or 0)
    height = float(box.get("height", 0) or 0)
    if icon_only and position in {"fixed", "absolute"} and width and height and abs(width - height) < max(width, height) * 0.5:
        is_fab = True
        signals.append("floating_action_button")

    if icon_only and "visible_text" not in signals and "aria_label" not in signals and "title_attribute" not in signals:
        if has_svg:
            signals.append("svg_icon")
        signals.append("icon_class")

    confidence = 0.85 if "visible_text" in signals else (0.7 if signals else 0.2)
    if verb is None:
        verb = "unknown"

    return ActionSemantics(
        element_id=dom_id,
        semantic_action=verb,
        confidence=confidence,
        signals=signals,
        is_icon_only=icon_only,
        is_kebab_menu=is_kebab,
        is_floating_action_button=is_fab,
    )


def classify_actions(raw: Any) -> list[ActionSemantics]:
    """`raw`: `dom_extractor.RawObservation`. Classifies every element with a
    `category` of button/link (the action-triggering categories) plus any
    icon-only element regardless of category, since an icon-only row action
    is routinely a bare `<div>`/`<span>` with no semantic tag at all."""
    results: list[ActionSemantics] = []
    for el in raw.elements:
        if not el.get("dom_id"):
            continue
        category = el.get("category")
        if category not in {"button", "link"} and not el.get("looks_icon_only"):
            continue
        results.append(_classify_one(el))
    return results


def action_semantics_by_id(results: list[ActionSemantics]) -> dict[str, str]:
    return {r.element_id: r.semantic_action for r in results if r.semantic_action != "unknown"}

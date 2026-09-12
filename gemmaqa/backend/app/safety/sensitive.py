"""Sensitive control detection from normalized UI text."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from app.schemas import InteractiveElement, PageState

# Default blocked / warned phrases (configurable via SafetyPolicy / env)
DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    "delete",
    "remove",
    "payment",
    "purchase",
    "checkout",
    "refund",
    "invite",
    "change password",
    "reset password",
    "transfer",
    "withdraw",
    "deactivate",
    "terminate",
    "cancel subscription",
    "send email",
    "send message",
    "publish",
    "deploy",
    "destroy",
    "purge",
    "wipe",
    "pay now",
    "buy now",
    "unsubscribe",
)


@dataclass(frozen=True)
class SensitiveMatch:
    matched: bool
    pattern: str = ""
    source_text: str = ""
    severity: str = "block"  # block | warn


def normalize_ui_text(text: str | None) -> str:
    """Normalize for matching: lowercase, strip accents, collapse whitespace/punct."""
    if not text:
        return ""
    # NFKD + drop combining marks for light i18n / accent folding
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_ish = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_ish.lower()
    # Keep letters/digits/spaces; turn other punctuation into spaces
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def element_context_text(el: InteractiveElement, nearby: str = "") -> str:
    parts = [
        el.text,
        el.visible_text,
        el.label,
        el.accessible_name,
        el.aria_label,
        el.name,
        el.placeholder,
        el.href,
        getattr(el, "title", None),
        el.id_attr,
        nearby,
    ]
    return normalize_ui_text(" ".join(p for p in parts if p))


def match_sensitive(
    text: str,
    patterns: Iterable[str] | None = None,
    *,
    warn_only_patterns: Iterable[str] | None = None,
) -> SensitiveMatch:
    """Match normalized text against blocked-word patterns (substring on normalized forms)."""
    hay = normalize_ui_text(text)
    if not hay:
        return SensitiveMatch(False)

    for pattern in patterns or DEFAULT_SENSITIVE_PATTERNS:
        needle = normalize_ui_text(pattern)
        if needle and needle in hay:
            severity = "warn" if warn_only_patterns and pattern in set(warn_only_patterns) else "block"
            return SensitiveMatch(True, pattern=pattern, source_text=hay[:200], severity=severity)
    return SensitiveMatch(False)


def scan_element(
    el: InteractiveElement,
    patterns: Iterable[str] | None = None,
    nearby: str = "",
) -> SensitiveMatch:
    return match_sensitive(element_context_text(el, nearby), patterns)


def nearby_form_text(page_state: PageState | None, element_id: str | None) -> str:
    if not page_state or not element_id:
        return ""
    chunks: list[str] = []
    for form in page_state.forms:
        field_ids = {f.element_id for f in form.fields if f.element_id}
        if element_id in field_ids or form.submit_element_id == element_id or form.form_id == element_id:
            chunks.append(form.action or "")
            for f in form.fields:
                chunks.extend([f.name or "", f.label or "", f.placeholder or ""])
    return " ".join(chunks)


def find_element(page_state: PageState | None, element_id: str | None) -> InteractiveElement | None:
    if not page_state or not element_id:
        return None
    for el in page_state.interactive_elements:
        if el.element_id == element_id:
            return el
    return None

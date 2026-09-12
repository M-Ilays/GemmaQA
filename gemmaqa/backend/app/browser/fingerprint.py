"""Page state fingerprinting for change / no-progress detection."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

from app.schemas import InteractiveElement, PageState


_COUNTER_RE = re.compile(
    r"\b\d{1,2}:\d{2}(:\d{2})?\b|\b\d{4}-\d{2}-\d{2}\b|\b\d+ (seconds?|minutes?|hours?) ago\b",
    re.IGNORECASE,
)
_TRANSIENT_RE = re.compile(r"\b\d+\s*(items?|results?|notifications?|messages?)\b", re.I)
# A control whose ENTIRE visible text is just digits (e.g. a cart/notification badge
# showing "6") is almost always volatile UI chrome, not meaningful content — without
# this, every add-to-cart action changes that one element's signature, which changes
# the whole-page fingerprint, which resets the "have I tried this before" memory for
# every other element on the page too.
_BARE_NUMBER_RE = re.compile(r"^\d{1,4}$")


def normalize_url(url: str) -> str:
    """Strip fragments and trailing slashes for stable comparison."""
    try:
        parsed = urlparse(url or "")
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", parsed.query, ""))
    except Exception:
        return (url or "").split("#")[0].rstrip("/")


def _clean_text(value: str | None, max_len: int = 80) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if _BARE_NUMBER_RE.match(text):
        return "<n>"
    text = _COUNTER_RE.sub("<n>", text)
    text = _TRANSIENT_RE.sub("<n> items", text)
    return text[:max_len]


def element_signature(el: InteractiveElement | dict[str, Any]) -> str:
    """Stable signature for an interactive control (no generated IDs)."""
    if isinstance(el, InteractiveElement):
        data = el.model_dump()
    else:
        data = el
    parts = [
        str(data.get("tag") or ""),
        str(data.get("role") or data.get("category") or ""),
        str(data.get("input_type") or data.get("type") or ""),
        _clean_text(data.get("accessible_name") or data.get("label") or data.get("aria_label")),
        _clean_text(data.get("visible_text") or data.get("text")),
        _clean_text(data.get("placeholder")),
        "1" if data.get("disabled") or not data.get("is_enabled", True) else "0",
        "1" if data.get("required") else "0",
    ]
    return "|".join(parts)


def compute_fingerprint(
    *,
    url: str,
    title: str,
    headings: list[str],
    interactive_elements: list[InteractiveElement] | list[dict[str, Any]],
    modals: list[str] | None = None,
    dialogs: list[str] | None = None,
    visible_text_summary: str = "",
) -> str:
    """
    Build a SHA-256 fingerprint from stable page fields.

    Excludes timestamps, generated element IDs, and rapidly changing counters
    where patterns are recognizable.
    """
    control_sigs = sorted(
        element_signature(el)
        for el in interactive_elements
        if _is_visible(el)
    )[:80]

    payload = {
        "url": normalize_url(url),
        "title": _clean_text(title, 120),
        "headings": [_clean_text(h, 60) for h in headings[:12]],
        "controls": control_sigs,
        "modals": [_clean_text(m, 60) for m in (modals or [])[:5]],
        "dialogs": [_clean_text(d, 60) for d in (dialogs or [])[:5]],
        "text": _clean_text(visible_text_summary, 240),
    }
    raw = repr(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def fingerprint_page_state(state: PageState) -> str:
    """Fingerprint a PageState model."""
    return compute_fingerprint(
        url=state.url,
        title=state.title,
        headings=state.headings,
        interactive_elements=state.interactive_elements,
        modals=state.modals,
        dialogs=state.dialogs,
        visible_text_summary=state.visible_text_summary,
    )


def _is_visible(el: InteractiveElement | dict[str, Any]) -> bool:
    if isinstance(el, InteractiveElement):
        return el.is_visible
    return bool(el.get("is_visible", True))

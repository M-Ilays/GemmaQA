"""State building — a richer page-state fingerprint than
`app.browser.fingerprint.compute_fingerprint`'s url/title/headings/controls/
text hash, incorporating the state signals the audit named explicitly:
selected tab, open dialog, expanded menus, active filters, pagination state,
search state, important visible regions, and the logged-in role (if known —
this module never infers auth state itself; the caller injects it).

Deterministic: same inputs always produce the same fingerprint. `known_role`
is an explicit parameter, never guessed, so this stays free of any
authentication/authorization logic of its own.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from app.browser.fingerprint import normalize_url
from app.perception.models import (
    DialogDescriptor,
    PageRegion,
    PaginationDescriptor,
    PageStateDescriptor,
    TabGroup,
)
from app.schemas import InteractiveElement

SEARCH_INPUT_TYPES = frozenset({"search"})


def _active_filter_ids(interactive_elements: list[InteractiveElement]) -> list[str]:
    """Generic, structural definition of "an active filter": a checked
    checkbox/radio among visible controls. No filter-related vocabulary is
    matched — purely toggle state."""
    return sorted(
        el.element_id
        for el in interactive_elements
        if el.is_visible and el.category in {"checkbox", "radio"} and el.checked
    )


def _search_state(interactive_elements: list[InteractiveElement]) -> Optional[str]:
    """The current value of the first visible native `type="search"` field —
    a standard HTML semantic, not a heuristic guess about page content."""
    for el in interactive_elements:
        if el.is_visible and (el.input_type or "").lower() in SEARCH_INPUT_TYPES:
            return el.current_value
    return None


def build_page_state(
    *,
    url: str,
    tabs: list[TabGroup],
    dialogs: list[DialogDescriptor],
    interactive_elements: list[InteractiveElement],
    pagination: list[PaginationDescriptor],
    regions: list[PageRegion],
    known_role: str | None = None,
) -> PageStateDescriptor:
    selected_tabs = sorted(tg.selected_tab_id for tg in tabs if tg.selected_tab_id)
    open_dialog_ids = sorted((d.element_id or d.stable_id) for d in dialogs if d.is_open)
    expanded_ids = sorted(el.element_id for el in interactive_elements if el.is_expanded)
    active_filters = _active_filter_ids(interactive_elements)
    search_state = _search_state(interactive_elements)
    pagination_state = sorted(
        f"{p.current_page or ''}/{p.total_pages or ''}" for p in pagination if p.current_page or p.total_pages
    )
    visible_regions = sorted({r.region_type for r in regions})

    payload = {
        "url": normalize_url(url),
        "selected_tabs": selected_tabs,
        "open_dialogs": open_dialog_ids,
        "expanded_elements": expanded_ids,
        "active_filters": active_filters,
        "pagination_state": pagination_state,
        "search_state": search_state or "",
        "visible_regions": visible_regions,
        "role": known_role or "",
    }
    fingerprint = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
    return PageStateDescriptor(
        fingerprint=fingerprint,
        contributing_fields=list(payload.keys()),
    )

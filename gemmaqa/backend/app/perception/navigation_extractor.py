"""Navigation extraction — builds NavigationRegion/NavigationItem,
BreadcrumbDescriptor, and PaginationDescriptor from the raw navigation
containers, breadcrumb, and pagination facts `dom_extractor` collected.

Richer than the current `PageState.navigation_items`/`.breadcrumbs`/
`.pagination_controls` (flat label lists): every item here carries a real
`target_url` and `is_current` state where the DOM provides one.
"""

from __future__ import annotations

from urllib.parse import urljoin

from app.application.url_normalize import origin_of, same_origin
from app.perception.dom_extractor import RawObservation
from app.perception.models import (
    BreadcrumbDescriptor,
    NavigationItem,
    NavigationRegion,
    PaginationDescriptor,
    TabDescriptor,
    TabGroup,
)
from app.utils.ids import new_id


def _is_current(value: object) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text != "false"


def extract_navigation_regions(raw: RawObservation) -> list[NavigationRegion]:
    regions: list[NavigationRegion] = []
    for container in raw.navigation_containers:
        items = [
            NavigationItem(
                element_id=item.get("dom_id"),
                stable_id=item.get("dom_id") or new_id(),
                text=item.get("text"),
                url=item.get("href"),
                target_url=item.get("href"),
                is_current=_is_current(item.get("is_current")),
                source="dom",
            )
            for item in (container.get("items") or [])
            if item.get("text")
        ]
        if not items:
            continue
        regions.append(
            NavigationRegion(
                items=items,
                landmark_role=container.get("role") or "nav",
                accessible_name=container.get("aria_label"),
                source="dom",
            )
        )
    return regions


def extract_breadcrumbs(raw: RawObservation) -> list[BreadcrumbDescriptor]:
    return [
        BreadcrumbDescriptor(
            text=item.get("text"),
            ordinal=i,
            url=item.get("href"),
            target_url=item.get("href"),
            source="dom",
        )
        for i, item in enumerate(raw.breadcrumbs)
        if item.get("text")
    ]


def extract_pagination(raw: RawObservation) -> list[PaginationDescriptor]:
    pagination = raw.pagination
    if not pagination:
        return []
    items = pagination.get("items") or []
    return [
        PaginationDescriptor(
            text=", ".join(i.get("text", "") for i in items if i.get("text")),
            current_page=pagination.get("current_page"),
            total_pages=pagination.get("total_pages"),
            source="dom",
        )
    ]


def extract_tabs(raw: RawObservation) -> list[TabGroup]:
    """Tab elements are already captured generically (any `role="tab"` matches
    `dom_extractor`'s interactive selector) with real `aria_selected` state —
    this groups them into one `TabGroup` with a real `selected_tab_id`.

    Simplification: every tab-role element on the page is treated as one
    group. A page with multiple independent tablists collapses into one
    `TabGroup` today — stated here rather than silently assumed away; see
    docs/PERCEPTION_ENGINE.md.
    """
    tab_elements = [el for el in raw.elements if el.get("category") == "tab" and el.get("dom_id")]
    if not tab_elements:
        return []

    tabs = [
        TabDescriptor(
            element_id=el["dom_id"],
            stable_id=el["dom_id"],
            text=el.get("text") or el.get("accessible_name"),
            accessible_name=el.get("accessible_name"),
            aria_role="tab",
            is_selected=el.get("aria_selected"),
            is_visible=bool(el.get("is_visible", True)),
            bounding_box=el.get("bounding_box"),
            parent_region_id=el.get("landmark_region_id"),
            source="dom",
        )
        for el in tab_elements
    ]
    selected = next((t.element_id for t in tabs if t.is_selected), None)
    return [TabGroup(tabs=tabs, selected_tab_id=selected, source="dom")]


def classify_link_locality(url: str | None, page_url: str) -> bool | None:
    """True if `url` is external to `page_url`'s origin — a straightforward,
    already-used-elsewhere same-origin comparison, never a guess. Relative
    URLs are resolved against `page_url` first (same_origin/origin_of only
    understand absolute URLs)."""
    if not url:
        return None
    return not same_origin(urljoin(page_url, url), origin_of(page_url))

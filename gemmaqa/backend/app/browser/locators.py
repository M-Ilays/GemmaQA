"""Locator registry and strategy selection for stable element IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Locator, Page

from app.schemas import InteractiveElement, LocatorStrategy, PageState
from app.utils.logging import get_logger

logger = get_logger("browser.locators")

_CSS_ESCAPE = re.compile(r'([!"#$%&\'()*+,./:;<=>?@\[\\\]^`{|}~])')


def css_escape(value: str) -> str:
    return _CSS_ESCAPE.sub(r"\\\1", value)


def choose_locator_strategy(raw: dict[str, Any]) -> LocatorStrategy:
    """
    Pick the best Playwright locator strategy from extracted element metadata.

    Priority:
      1. data-testid
      2. unique id
      3. accessible role + name
      4. associated form label
      5. placeholder
      6. link text
      7. button text
      8. CSS selector
      9. nth fallback
    """
    tag = (raw.get("tag") or "div").lower()
    test_id = raw.get("test_id") or raw.get("data_testid")
    id_attr = raw.get("id_attr") or raw.get("id")
    role = raw.get("role") or _implied_role(tag, raw.get("input_type") or raw.get("type"))
    accessible = (
        raw.get("accessible_name")
        or raw.get("aria_label")
        or raw.get("label")
        or ""
    ).strip()
    placeholder = (raw.get("placeholder") or "").strip()
    text = (raw.get("visible_text") or raw.get("text") or "").strip()
    href = raw.get("href")
    name = raw.get("name")
    gemma_id = raw.get("element_id")
    nth = raw.get("nth")
    fallbacks: list[str] = []

    if gemma_id:
        fallbacks.append(f'[data-gemmaqa-id="{gemma_id}"]')

    if test_id:
        sel = f'[data-testid="{css_escape(str(test_id))}"]'
        return LocatorStrategy(kind="testid", selector=sel, fallback_selectors=fallbacks)

    if id_attr and _is_unique_id_candidate(str(id_attr)):
        sel = f"#{css_escape(str(id_attr))}"
        return LocatorStrategy(kind="id", selector=sel, fallback_selectors=fallbacks)

    if role and accessible:
        return LocatorStrategy(
            kind="role",
            role=role,
            name=accessible[:120],
            fallback_selectors=fallbacks,
        )

    if accessible and tag in {"input", "textarea", "select"}:
        # Label association often works via get_by_label
        return LocatorStrategy(
            kind="label",
            name=accessible[:120],
            fallback_selectors=fallbacks,
        )

    if placeholder:
        return LocatorStrategy(
            kind="placeholder",
            text=placeholder[:120],
            fallback_selectors=fallbacks,
        )

    if tag == "a" and text:
        return LocatorStrategy(
            kind="link_text",
            text=text[:120],
            fallback_selectors=fallbacks,
        )

    if tag in {"button", "summary"} or role == "button":
        if text or accessible:
            return LocatorStrategy(
                kind="button_text",
                text=(text or accessible)[:120],
                fallback_selectors=fallbacks,
            )

    # CSS selector from attributes
    css_parts = [tag]
    if name:
        css_parts.append(f'[name="{css_escape(str(name))}"]')
    elif href and tag == "a":
        css_parts.append(f'[href="{css_escape(str(href)[:180])}"]')
    elif raw.get("input_type") or raw.get("type"):
        css_parts.append(f'[type="{css_escape(str(raw.get("input_type") or raw.get("type")))}"]')
    css = "".join(css_parts)
    if css != tag or gemma_id:
        return LocatorStrategy(
            kind="css",
            selector=css if css != tag else (fallbacks[0] if fallbacks else tag),
            fallback_selectors=fallbacks,
            nth=nth,
        )

    return LocatorStrategy(
        kind="nth",
        selector=tag,
        nth=int(nth) if nth is not None else 0,
        fallback_selectors=fallbacks,
    )


def _implied_role(tag: str, input_type: str | None) -> str | None:
    if tag == "a":
        return "link"
    if tag == "button":
        return "button"
    if tag == "select":
        return "combobox"
    if tag == "textarea":
        return "textbox"
    if tag == "input":
        t = (input_type or "text").lower()
        if t in {"checkbox"}:
            return "checkbox"
        if t in {"radio"}:
            return "radio"
        if t in {"button", "submit", "reset"}:
            return "button"
        if t in {"file"}:
            return None
        return "textbox"
    return None


def _is_unique_id_candidate(value: str) -> bool:
    if not value or " " in value:
        return False
    # Skip obviously generated unstable IDs
    if re.fullmatch(r"(ember|react|mui|:/+)\d+", value, re.I):
        return False
    if re.fullmatch(r"[a-f0-9-]{32,}", value, re.I):
        return False
    return True


@dataclass
class LocatorRegistry:
    """Maps element_id → LocatorStrategy for the current page observation."""

    strategies: dict[str, LocatorStrategy] = field(default_factory=dict)
    elements: dict[str, InteractiveElement] = field(default_factory=dict)

    def clear(self) -> None:
        self.strategies.clear()
        self.elements.clear()

    def register(self, element: InteractiveElement) -> None:
        strategy = element.locator_strategy or choose_locator_strategy(element.model_dump())
        element.locator_strategy = strategy
        if not element.selector_hint and strategy.selector:
            element.selector_hint = strategy.selector
        elif not element.selector_hint:
            element.selector_hint = f'[data-gemmaqa-id="{element.element_id}"]'
        self.strategies[element.element_id] = strategy
        self.elements[element.element_id] = element

    def get(self, element_id: str | None) -> LocatorStrategy | None:
        if not element_id:
            return None
        return self.strategies.get(element_id)

    def get_element(self, element_id: str | None) -> InteractiveElement | None:
        if not element_id:
            return None
        return self.elements.get(element_id)

    def resolve_selector(self, element_id: str | None) -> str | None:
        """Return a CSS/selector string when available (for simple cases)."""
        if not element_id:
            return None
        strategy = self.strategies.get(element_id)
        if strategy and strategy.selector:
            return strategy.selector
        el = self.elements.get(element_id)
        if el and el.selector_hint:
            return el.selector_hint
        return f'[data-gemmaqa-id="{element_id}"]'

    async def resolve_locator(self, page: Page, element_id: str) -> Locator:
        """Build a Playwright Locator from the registered strategy."""
        strategy = self.strategies.get(element_id)
        if not strategy:
            return page.locator(f'[data-gemmaqa-id="{element_id}"]').first

        locator = await self._build_locator(page, strategy)
        return locator

    async def _build_locator(self, page: Page, strategy: LocatorStrategy) -> Locator:
        kind = strategy.kind
        try:
            if kind == "testid" and strategy.selector:
                return page.locator(strategy.selector).first
            if kind == "id" and strategy.selector:
                return page.locator(strategy.selector).first
            if kind == "role" and strategy.role:
                loc = page.get_by_role(strategy.role, name=strategy.name or None)  # type: ignore[arg-type]
                return loc.first
            if kind == "label" and strategy.name:
                return page.get_by_label(strategy.name).first
            if kind == "placeholder" and strategy.text:
                return page.get_by_placeholder(strategy.text).first
            if kind == "link_text" and strategy.text:
                return page.get_by_role("link", name=strategy.text).first
            if kind == "button_text" and strategy.text:
                return page.get_by_role("button", name=strategy.text).first
            if kind == "css" and strategy.selector:
                loc = page.locator(strategy.selector)
                if strategy.nth is not None:
                    return loc.nth(strategy.nth)
                return loc.first
            if kind == "nth" and strategy.selector:
                return page.locator(strategy.selector).nth(strategy.nth or 0)
        except Exception as exc:
            logger.debug("Primary locator failed (%s): %s", kind, exc)

        for fb in strategy.fallback_selectors:
            try:
                return page.locator(fb).first
            except Exception:
                continue
        if strategy.selector:
            return page.locator(strategy.selector).first
        raise ValueError(f"Unable to build locator for strategy {strategy}")


def resolve_selector(page_state: PageState, element_id: str | None) -> str | None:
    """Resolve a selector for a known page-state element. Returns None if unknown."""
    if not element_id:
        return None
    for el in page_state.interactive_elements:
        if el.element_id == element_id:
            if el.selector_hint:
                return el.selector_hint
            if el.locator_strategy and el.locator_strategy.selector:
                return el.locator_strategy.selector
            return f'[data-gemmaqa-id="{element_id}"]'
    for form in page_state.forms:
        if form.form_id == element_id:
            return f'[data-gemmaqa-id="{element_id}"]'
        for field in form.fields:
            if field.element_id == element_id:
                return f'[data-gemmaqa-id="{element_id}"]'
    for table in page_state.tables:
        if table.table_id == element_id:
            return f'[data-gemmaqa-id="{element_id}"]'
    return None

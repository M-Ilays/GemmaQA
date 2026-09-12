"""Unit tests for the Playwright browser automation layer (no live browser required)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.browser.fingerprint import (  # noqa: E402
    compute_fingerprint,
    element_signature,
    normalize_url,
)
from app.browser.locators import LocatorRegistry, choose_locator_strategy  # noqa: E402
from app.browser.network_monitor import sanitize_network_url  # noqa: E402
from app.browser.observer import PageObserver, normalize_element  # noqa: E402
from app.browser.executor import ActionExecutor  # noqa: E402
from app.schemas import ActionType, BrowserAction, InteractiveElement, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_normalize_url_strips_fragment_and_slash():
    assert normalize_url("https://Example.com/path/#section") == "https://example.com/path"
    assert normalize_url("https://example.com/path/") == "https://example.com/path"


def test_fingerprint_stable_across_generated_ids():
    els_a = [
        InteractiveElement(
            element_id="el_001",
            tag="a",
            role="link",
            visible_text="Customers",
            accessible_name="Customers",
            is_visible=True,
        )
    ]
    els_b = [
        InteractiveElement(
            element_id="el_999",
            tag="a",
            role="link",
            visible_text="Customers",
            accessible_name="Customers",
            is_visible=True,
        )
    ]
    fp1 = compute_fingerprint(
        url="https://app.example.com/home",
        title="Home",
        headings=["Welcome"],
        interactive_elements=els_a,
        visible_text_summary="Hello world",
    )
    fp2 = compute_fingerprint(
        url="https://app.example.com/home#top",
        title="Home",
        headings=["Welcome"],
        interactive_elements=els_b,
        visible_text_summary="Hello world",
    )
    assert fp1 == fp2


def test_fingerprint_changes_when_controls_change():
    base = dict(
        url="https://app.example.com/",
        title="Home",
        headings=["Welcome"],
        visible_text_summary="Hello",
    )
    fp1 = compute_fingerprint(
        **base,
        interactive_elements=[
            InteractiveElement(element_id="el_001", tag="button", visible_text="Save", is_visible=True)
        ],
    )
    fp2 = compute_fingerprint(
        **base,
        interactive_elements=[
            InteractiveElement(element_id="el_001", tag="button", visible_text="Delete", is_visible=True)
        ],
    )
    assert fp1 != fp2


def test_fingerprint_stable_across_cart_badge_count_changes():
    """Regression: SauceDemo's cart icon renders just a bare digit ("6") as its whole
    accessible name/text — the existing transient-counter regex only matched a number
    followed by a word like "items", so a bare badge count slipped through unnormalized.
    Every add-to-cart action then produced a brand-new page fingerprint purely because
    the badge ticked up, which reset the "already tried this" memory for every other
    element on the page too — the agent kept re-clicking the same first product
    instead of ever advancing to a different, still-unvisited one."""
    base = dict(
        url="https://www.saucedemo.com/inventory.html",
        title="Swag Labs",
        headings=[],
        visible_text_summary="Products",
    )
    cart_link = lambda count: InteractiveElement(  # noqa: E731
        element_id="el_007", tag="a", category="link", accessible_name=str(count), is_visible=True
    )
    fp_before = compute_fingerprint(**base, interactive_elements=[cart_link(0)])
    fp_after = compute_fingerprint(**base, interactive_elements=[cart_link(6)])
    assert fp_before == fp_after


def test_element_signature_ignores_ids():
    a = InteractiveElement(element_id="el_001", tag="button", accessible_name="OK", visible_text="OK")
    b = InteractiveElement(element_id="el_002", tag="button", accessible_name="OK", visible_text="OK")
    assert element_signature(a) == element_signature(b)


# ---------------------------------------------------------------------------
# Locator strategy
# ---------------------------------------------------------------------------


def test_locator_prefers_testid():
    strategy = choose_locator_strategy(
        {"tag": "button", "test_id": "save-btn", "text": "Save", "element_id": "el_001"}
    )
    assert strategy.kind == "testid"
    assert "data-testid" in (strategy.selector or "")


def test_locator_prefers_id_over_text():
    strategy = choose_locator_strategy(
        {"tag": "input", "id_attr": "email", "placeholder": "Email", "element_id": "el_002"}
    )
    assert strategy.kind == "id"
    assert strategy.selector == "#email"


def test_locator_role_and_name():
    strategy = choose_locator_strategy(
        {
            "tag": "button",
            "role": "button",
            "accessible_name": "Create customer",
            "element_id": "el_003",
        }
    )
    assert strategy.kind == "role"
    assert strategy.role == "button"
    assert strategy.name == "Create customer"


def test_locator_link_text():
    strategy = choose_locator_strategy(
        {"tag": "a", "visible_text": "Customers", "href": "/customers", "element_id": "el_004"}
    )
    assert strategy.kind == "link_text"
    assert strategy.text == "Customers"


def test_locator_nth_fallback():
    strategy = choose_locator_strategy({"tag": "div", "element_id": "el_005", "nth": 2})
    assert strategy.kind in {"nth", "css"}
    assert strategy.fallback_selectors


# ---------------------------------------------------------------------------
# Element normalization
# ---------------------------------------------------------------------------


def test_normalize_element_masks_password_and_assigns_strategy():
    el = normalize_element(
        {
            "element_id": "el_010",
            "tag": "input",
            "type": "password",
            "input_type": "password",
            "name": "password",
            "id_attr": "password",
            "current_value": "hunter2",
            "is_visible": True,
            "is_enabled": True,
        }
    )
    assert el.current_value == "***"
    assert el.locator_strategy is not None
    assert el.locator_strategy.kind == "id"


def test_observer_build_page_state_limits_and_fingerprint():
    observer = PageObserver(max_elements=5, text_max_chars=50)
    raw = {
        "title": "Demo",
        "headings": ["H1", "H2"],
        "visible_text": "x" * 500,
        "breadcrumbs": ["Home", "Demo"],
        "navigation_items": ["Home"],
        "interactive_elements": [
            {
                "element_id": f"el_{i:03d}",
                "tag": "a",
                "visible_text": f"Link {i}",
                "accessible_name": f"Link {i}",
                "href": f"/p{i}",
                "is_visible": True,
                "is_enabled": True,
                "category": "link",
            }
            for i in range(1, 12)
        ],
        "forms": [],
        "tables": [],
        "tabs": [],
        "dialogs": [],
        "modals": [],
        "toasts": [],
        "alerts": [],
        "pagination_controls": [],
        "search_fields": [],
        "filter_controls": [],
        "disabled_controls": [],
        "required_fields": [],
    }
    state = observer.build_page_state(raw=raw, url="https://example.com/demo")
    assert len(state.interactive_elements) == 5
    assert len(state.visible_text_summary) <= 53  # truncate adds ...
    assert state.state_fingerprint
    assert "el_001" in observer.registry.strategies
    assert state.breadcrumbs == ["Home", "Demo"]


# ---------------------------------------------------------------------------
# Network sanitization
# ---------------------------------------------------------------------------


def test_sanitize_network_url_redacts_tokens_and_userinfo():
    url = "https://user:secret@api.example.com/v1?access_token=abc123&page=1"
    cleaned = sanitize_network_url(url)
    assert "secret" not in cleaned
    assert "abc123" not in cleaned
    assert "REDACTED" in cleaned
    assert "page=1" in cleaned
    assert "user:" not in cleaned


def test_sanitize_network_url_redacts_password_query():
    cleaned = sanitize_network_url("https://example.com/login?password=hunter2&user=qa")
    assert "hunter2" not in cleaned
    assert "REDACTED" in cleaned
    assert "user=qa" in cleaned


# ---------------------------------------------------------------------------
# Executor validation / unknown element
# ---------------------------------------------------------------------------


def test_executor_unknown_element_raises():
    executor = ActionExecutor(run_id=new_id())
    page = AsyncMock()
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")
    locator = AsyncMock()
    locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=MagicMock(first=locator))

    action = BrowserAction(action=ActionType.CLICK, element_id="el_missing", reason="test")
    state = PageState(page_id=new_id(), url="https://example.com", interactive_elements=[])

    result = asyncio.run(executor.execute(page, action, page_state=state, capture_evidence=False))
    assert result.success is False
    assert result.error is not None
    assert "Unknown element_id" in result.error


def test_executor_finish_succeeds_without_element():
    executor = ActionExecutor(run_id=new_id())
    page = AsyncMock()
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")

    action = BrowserAction(action=ActionType.FINISH, reason="done")
    result = asyncio.run(executor.execute(page, action, capture_evidence=False))
    assert result.success is True
    assert result.before_url == "https://example.com"


def test_registry_resolve_selector():
    registry = LocatorRegistry()
    el = normalize_element(
        {
            "element_id": "el_001",
            "tag": "button",
            "test_id": "go",
            "visible_text": "Go",
            "is_visible": True,
            "is_enabled": True,
        }
    )
    registry.register(el)
    assert registry.resolve_selector("el_001")
    assert registry.get("el_001").kind == "testid"

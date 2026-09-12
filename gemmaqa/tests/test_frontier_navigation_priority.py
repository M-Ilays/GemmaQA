"""Navigation-candidate priority/semantic-operation correctness (Phase 5).

Regression coverage for two real bugs found via live SauceDemo runs:
1. "has a real href" was used as a proxy for "important in-app navigation", which
   backwards-favored off-site footer/social links (real absolute hrefs) over
   same-origin SPA content links (which often use href="#" with a JS click handler),
   letting exploration get stuck retrying external links while ignoring product
   pages.
2. infer_semantic_operation's "cancel"/"back" hint used plain substring matching, so
   a product named "Sauce Labs Backpack" was misclassified as a "navigate_back"
   control purely because it contains the substring "back".
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.frontier import FrontierBuilder, infer_semantic_operation  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.schemas import InteractiveElement, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402


def _inventory_like_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://example.com/inventory.html",
        title="Products",
        headings=["Products"],
        interactive_elements=[
            InteractiveElement(
                element_id="el_product",
                tag="a",
                category="link",
                accessible_name="Sauce Labs Backpack",
                text="Sauce Labs Backpack",
                href="#",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_twitter",
                tag="a",
                category="link",
                accessible_name="Twitter",
                text="Twitter",
                href="https://twitter.com/saucelabs",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_menu",
                tag="button",
                role="button",
                category="button",
                accessible_name="",
                id_attr="react-burger-menu-btn",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


def _memory() -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.remaining_action_budget = 30
    return memory


def test_in_scope_placeholder_href_link_beats_generic_menu_toggle():
    """A real SPA content link (href="#", click-handled) must outrank a same-priority
    generic UI control (menu toggle) instead of tying and losing on candidate_id order."""
    memory = _memory()
    page = _inventory_like_page()
    cands = FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)
    by_id = {c.element_id: c for c in cands if c.candidate_type == "navigation_control"}

    assert by_id["el_product"].priority < by_id["el_menu"].priority


def test_external_link_deprioritized_below_in_scope_content_link():
    memory = _memory()
    page = _inventory_like_page()
    cands = FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)
    by_id = {c.element_id: c for c in cands if c.candidate_type == "navigation_control"}

    assert by_id["el_product"].priority < by_id["el_twitter"].priority


def test_menu_toggle_classified_as_expand_region_via_id_attr():
    memory = _memory()
    page = _inventory_like_page()
    cands = FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)
    menu_cand = next(c for c in cands if c.element_id == "el_menu")
    assert menu_cand.semantic_operation == "expand_region"


def test_product_link_not_misclassified_as_navigate_back():
    el = InteractiveElement(
        element_id="el_product",
        tag="a",
        category="link",
        accessible_name="Sauce Labs Backpack",
        text="Sauce Labs Backpack",
        href="#",
        is_visible=True,
        is_enabled=True,
    )
    text = "sauce labs backpack"
    assert infer_semantic_operation(el, text) == "navigate"


def test_real_back_button_still_classified_as_navigate_back():
    el = InteractiveElement(
        element_id="el_back",
        tag="button",
        role="button",
        category="button",
        accessible_name="Back",
        text="Back",
        is_visible=True,
        is_enabled=True,
    )
    assert infer_semantic_operation(el, "back") == "navigate_back"


def test_cancel_button_ranks_below_finish_button_on_same_page():
    """Regression: SauceDemo's checkout Overview page has both "Cancel" and "Finish"
    buttons. Both used to match NAV_BUTTON_HINTS at the same priority, so the
    candidate_id tie-break (alphabetically "Cancel" < "Finish") picked Cancel every
    time, discarding an almost-complete checkout flow at its very last step."""
    memory = _memory()
    page = PageState(
        page_id=new_id(),
        url="https://example.com/checkout-step-two",
        title="Overview",
        headings=["Overview"],
        interactive_elements=[
            InteractiveElement(
                element_id="el_cancel",
                tag="button",
                role="button",
                category="button",
                accessible_name="Cancel",
                text="Cancel",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_finish",
                tag="button",
                role="button",
                category="button",
                accessible_name="Finish",
                text="Finish",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )
    cands = FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)
    by_id = {c.element_id: c for c in cands if c.candidate_type == "navigation_control"}
    assert by_id["el_finish"].priority < by_id["el_cancel"].priority


def test_expand_region_toggle_gets_bounded_retry_not_one_shot_exhaustion():
    """A menu/drawer toggle must survive being clicked a second time even though the
    page fingerprint (and thus its raw action signature) recurs identically once the
    menu is closed again — otherwise it becomes a permanent dead end for whatever it
    reveals, the first time some other action closes the menu without exhausting it."""
    memory = _memory()
    page = _inventory_like_page()
    auth = AuthenticationStrategy()

    first = FrontierBuilder(auth).build(page, memory=memory)
    menu1 = next(c for c in first if c.element_id == "el_menu")
    assert menu1.status == "available"

    sig = menu1.candidate_id  # nav_<element_id> — derive the raw signature the same way
    from app.agent.memory import action_signature

    real_sig = action_signature(
        page_fingerprint=page.state_fingerprint, action_type="click", element_id="el_menu"
    )
    memory.mark_signature(real_sig)  # simulate one real click

    second = FrontierBuilder(auth).build(page, memory=memory)
    menu2 = next(c for c in second if c.element_id == "el_menu")
    assert menu2.status == "available", "one prior attempt must not exhaust an expand_region toggle"

    memory.mark_signature(real_sig)  # simulate a second real click
    third = FrontierBuilder(auth).build(page, memory=memory)
    menu3 = next(c for c in third if c.element_id == "el_menu")
    assert menu3.status == "exhausted", "the bounded retry limit must still apply eventually"

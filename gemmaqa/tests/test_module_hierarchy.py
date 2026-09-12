"""Module/submodule hierarchy inference (Phase 9): AppModule.parent_module_id,
previously always None, gets populated from URL-hierarchy evidence — never a
hardcoded per-target-app name."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.application.store import ApplicationStore  # noqa: E402
from app.schemas import PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402

ROOT = "https://www.saucedemo.com"


def _page(url: str) -> PageState:
    return PageState(page_id=new_id(), url=url, title="Swag Labs", state_fingerprint=f"fp-{url}")


def test_existing_module_becomes_parent_of_its_hyphenated_sibling():
    """inventory-item.html shares the "inventory" prefix with the REAL inventory
    module — that module itself should become the parent, no synthetic module."""
    store = ApplicationStore("run-1", ROOT)
    store.observe_page(_page(f"{ROOT}/inventory.html"))
    store.observe_page(_page(f"{ROOT}/inventory-item.html"))
    store.sync_legacy_modules()

    by_key = {m.canonical_key: m for m in store.model.modules}
    assert "inventory" in by_key
    assert "inventory-item" in by_key
    assert by_key["inventory-item"].parent_module_id == by_key["inventory"].id
    assert by_key["inventory"].parent_module_id is None


def test_synthetic_parent_created_for_shared_prefix_with_no_exact_module():
    """None of checkout-step-one/-two/-complete literally equals "checkout", so a
    synthetic "Checkout" parent module must be created to group them."""
    store = ApplicationStore("run-2", ROOT)
    store.observe_page(_page(f"{ROOT}/checkout-step-one.html"))
    store.observe_page(_page(f"{ROOT}/checkout-step-two.html"))
    store.observe_page(_page(f"{ROOT}/checkout-complete.html"))
    store.sync_legacy_modules()

    by_key = {m.canonical_key: m for m in store.model.modules}
    assert "checkout" in by_key
    parent = by_key["checkout"]
    assert parent.source == "url_hierarchy"
    children = [m for m in store.model.modules if m.parent_module_id == parent.id]
    assert {m.canonical_key for m in children} == {
        "checkout-step-one",
        "checkout-step-two",
        "checkout-complete",
    }


def test_module_without_a_hyphenated_sibling_stays_standalone():
    store = ApplicationStore("run-3", ROOT)
    store.observe_page(_page(f"{ROOT}/cart.html"))
    store.sync_legacy_modules()
    cart = next(m for m in store.model.modules if m.canonical_key == "cart")
    assert cart.parent_module_id is None


def test_hierarchy_is_stable_across_repeated_syncs_no_duplicate_synthetic_parents():
    store = ApplicationStore("run-4", ROOT)
    store.observe_page(_page(f"{ROOT}/checkout-step-one.html"))
    store.observe_page(_page(f"{ROOT}/checkout-step-two.html"))
    store.sync_legacy_modules()
    store.sync_legacy_modules()
    store.sync_legacy_modules()

    checkout_parents = [m for m in store.model.modules if m.canonical_key == "checkout"]
    assert len(checkout_parents) == 1


def test_new_sibling_discovered_later_joins_the_existing_synthetic_parent():
    store = ApplicationStore("run-5", ROOT)
    store.observe_page(_page(f"{ROOT}/checkout-step-one.html"))
    store.observe_page(_page(f"{ROOT}/checkout-step-two.html"))
    store.sync_legacy_modules()
    parent_id_first = next(m.id for m in store.model.modules if m.canonical_key == "checkout")

    store.observe_page(_page(f"{ROOT}/checkout-complete.html"))
    store.sync_legacy_modules()

    checkout_parents = [m for m in store.model.modules if m.canonical_key == "checkout"]
    assert len(checkout_parents) == 1
    assert checkout_parents[0].id == parent_id_first
    complete_module = next(m for m in store.model.modules if m.canonical_key == "checkout-complete")
    assert complete_module.parent_module_id == parent_id_first


def test_synthetic_parent_survives_pruning_despite_having_no_pages_of_its_own():
    store = ApplicationStore("run-6", ROOT)
    store.observe_page(_page(f"{ROOT}/checkout-step-one.html"))
    store.observe_page(_page(f"{ROOT}/checkout-step-two.html"))
    store.sync_legacy_modules()
    parent = next(m for m in store.model.modules if m.canonical_key == "checkout")
    assert parent.page_ids == []
    store.sync_legacy_modules()  # a second pass must not prune the empty-page-ids parent
    assert any(m.canonical_key == "checkout" for m in store.model.modules)

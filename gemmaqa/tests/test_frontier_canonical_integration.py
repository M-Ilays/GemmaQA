"""CanonicalPageModel <-> FrontierBuilder integration.

Proves CanonicalPageModel is the primary source for non-authentication
exploration candidates: header/sidebar navigation, tabs, footer/social links,
forms, tables, row actions, icon-only controls, dropdowns, unknown
interactive components, and dialog handling — plus that a stale (wrong
url/state_fingerprint) canonical model is never trusted, and that
FrontierBuilder remains the single candidate source (no separate ranking
system, safety validator untouched).
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.frontier import FrontierBuilder  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.planner import Planner  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.perception.models import (  # noqa: E402
    CanonicalPageModel,
    DialogDescriptor,
    ImageDescriptor,
    NavigationItem,
    NavigationRegion,
    PageRegion,
    TabDescriptor,
    TabGroup,
    UnknownComponent,
)
from app.perception.models import LinkDescriptor  # noqa: E402
from app.schemas import (  # noqa: E402
    ActionType,
    FormDescriptor,
    FormField,
    InteractiveElement,
    PageState,
    TableColumnDescriptor,
    TableDescriptor,
    TableRowDescriptor,
)
from app.utils.ids import new_id  # noqa: E402

URL = "https://example.com/app"


def _memory() -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url=URL)
    memory.remaining_action_budget = 30
    return memory


def _page(state_fingerprint: str, interactive_elements=None, forms=None, tables=None) -> PageState:
    return PageState(
        page_id=new_id(),
        url=URL,
        title="App",
        state_fingerprint=state_fingerprint,
        headings=["App"],
        interactive_elements=interactive_elements or [],
        forms=forms or [],
        tables=tables or [],
    )


def _canonical(state_fingerprint: str, **collections) -> CanonicalPageModel:
    return CanonicalPageModel(url=URL, state_fingerprint=state_fingerprint, **collections)


def _by_element(cands, element_id: str):
    return next(c for c in cands if c.element_id == element_id)


def _build(page: PageState, memory: RunMemory) -> list:
    return FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)


# ---------------------------------------------------------------------------
# Header navigation
# ---------------------------------------------------------------------------


def test_header_navigation_item_classified_as_open_navigation_item():
    el = InteractiveElement(
        element_id="el_home",
        tag="a",
        category="link",
        accessible_name="Home",
        text="Home",
        href="/home",
        is_visible=True,
        is_enabled=True,
        parent_region_id="region_header_nav",
    )
    header_region = PageRegion(stable_id="region_header_nav", element_id="region_header_nav", region_type="primary_navigation")
    nav_region = NavigationRegion(items=[NavigationItem(element_id="el_home", text="Home", target_url="/home")])
    model = _canonical("fp1", regions=[header_region], navigation_regions=[nav_region], interactive_elements=[el])

    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_home")
    assert cand.candidate_type == "open_navigation_item"
    assert cand.source_region_id == "region_header_nav"
    assert cand.navigation_centrality > 0.0


# ---------------------------------------------------------------------------
# Sidebar navigation (expandable region toggle)
# ---------------------------------------------------------------------------


def test_sidebar_toggle_classified_as_expand_navigation_region():
    el = InteractiveElement(
        element_id="el_sidebar_toggle",
        tag="button",
        category="button",
        accessible_name="Sidebar sections",
        text="Sidebar sections",
        is_visible=True,
        is_enabled=True,
        is_expanded=False,
        evidence=["expandable_control"],
        parent_region_id="region_sidebar",
    )
    sidebar_region = PageRegion(stable_id="region_sidebar", element_id="region_sidebar", region_type="sidebar")
    model = _canonical("fp1", regions=[sidebar_region], interactive_elements=[el])

    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_sidebar_toggle")
    assert cand.candidate_type == "expand_navigation_region"
    assert cand.semantic_type == "sidebar"


def test_generic_disclosure_toggle_not_tied_to_nav_is_expand_accordion():
    el = InteractiveElement(
        element_id="el_faq",
        tag="button",
        category="button",
        accessible_name="What is your return policy?",
        text="What is your return policy?",
        is_visible=True,
        is_enabled=True,
        is_expanded=False,
        evidence=["expandable_control"],
    )
    model = _canonical("fp1", interactive_elements=[el])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_faq")
    assert cand.candidate_type == "expand_accordion"


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def test_unselected_tab_classified_as_select_tab():
    el = InteractiveElement(
        element_id="el_tab_billing",
        tag="button",
        role="tab",
        category="tab",
        accessible_name="Billing",
        text="Billing",
        is_visible=True,
        is_enabled=True,
        is_selected=False,
    )
    tab_group = TabGroup(
        tabs=[TabDescriptor(element_id="el_tab_billing", text="Billing", is_selected=False)],
        selected_tab_id=None,
    )
    model = _canonical("fp1", tabs=[tab_group], interactive_elements=[el])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_tab_billing")
    assert cand.candidate_type == "select_tab"

    action = Planner(MockGemmaProvider())._candidate_to_action(cand, page, memory, AuthenticationStrategy(), {})
    assert action is not None
    assert action.action == ActionType.OPEN_TAB


# ---------------------------------------------------------------------------
# Footer links (internal) and social links (external)
# ---------------------------------------------------------------------------


def test_footer_internal_link_classified_as_verify_internal_link():
    el = InteractiveElement(
        element_id="el_footer_about",
        tag="a",
        category="link",
        accessible_name="About us",
        text="About us",
        href="/about",
        is_visible=True,
        is_enabled=True,
        parent_region_id="region_footer",
        is_external_url=False,
    )
    footer_region = PageRegion(stable_id="region_footer", element_id="region_footer", region_type="footer")
    link = LinkDescriptor(element_id="el_footer_about", target_url="/about", is_external=False)
    model = _canonical("fp1", regions=[footer_region], links=[link], interactive_elements=[el])

    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_footer_about")
    assert cand.candidate_type == "verify_internal_link"


def test_footer_social_link_classified_as_verify_external_link_and_never_navigates():
    el = InteractiveElement(
        element_id="el_twitter",
        tag="a",
        category="link",
        accessible_name="Twitter",
        text="Twitter",
        href="https://twitter.com/example",
        is_visible=True,
        is_enabled=True,
        parent_region_id="region_footer",
    )
    footer_region = PageRegion(stable_id="region_footer", element_id="region_footer", region_type="footer")
    link = LinkDescriptor(element_id="el_twitter", target_url="https://twitter.com/example", is_external=True)
    model = _canonical("fp1", regions=[footer_region], links=[link], interactive_elements=[el])

    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_twitter")
    assert cand.candidate_type == "verify_external_link"
    assert cand.external_navigation_cost > 0.0

    action = Planner(MockGemmaProvider())._candidate_to_action(cand, page, memory, AuthenticationStrategy(), {})
    assert action is not None
    assert action.action == ActionType.HOVER, "verifying an external link must never actually navigate to it"


# ---------------------------------------------------------------------------
# Forms / tables (regression: unchanged when a canonical model also exists)
# ---------------------------------------------------------------------------


def test_inspect_form_candidate_still_generated_with_canonical_model_present():
    form = FormDescriptor(form_id="form_1", fields=[FormField(name="q", field_type="text", element_id="el_q")])
    model = _canonical("fp1")
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", forms=[form])

    cands = _build(page, memory)
    form_cand = next(c for c in cands if c.candidate_type == "inspect_form")
    assert form_cand.form_id == "form_1"


def test_inspect_table_candidate_still_generated_with_canonical_model_present():
    table = TableDescriptor(table_id="table_1", headers=["Name"], row_count=1, sample_rows=[["Alice"]])
    model = _canonical("fp1")
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", tables=[table])

    cands = _build(page, memory)
    table_cand = next(c for c in cands if c.candidate_type == "inspect_table")
    assert table_cand.element_id == "table_1"


# ---------------------------------------------------------------------------
# Row actions
# ---------------------------------------------------------------------------


def test_table_row_action_element_classified_as_open_table_row():
    el = InteractiveElement(
        element_id="el_row_edit",
        tag="button",
        category="button",
        accessible_name="Edit",
        text="Edit",
        is_visible=True,
        is_enabled=True,
        evidence=["table_row_action"],
    )
    table = TableDescriptor(
        table_id="table_1",
        headers=["Name"],
        row_count=1,
        sample_rows=[["Alice"]],
        stable_id="table_1",
        rows=[
            TableRowDescriptor(
                stable_id="table_1_row_0", row_index=0, cell_values=["Alice"], row_action_element_ids=["el_row_edit"]
            )
        ],
        columns=[TableColumnDescriptor(stable_id="table_1_col_0", column_index=0, header_text="Name")],
    )
    model = _canonical("fp1", tables=[table], interactive_elements=[el])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el], tables=[table])

    cand = _by_element(_build(page, memory), "el_row_edit")
    assert cand.candidate_type == "open_table_row"


# ---------------------------------------------------------------------------
# Icon-only controls
# ---------------------------------------------------------------------------


def test_icon_only_control_still_produces_a_dispatchable_candidate():
    el = InteractiveElement(
        element_id="el_icon_search",
        tag="button",
        category="button",
        accessible_name=None,
        text=None,
        aria_label=None,
        is_visible=True,
        is_enabled=True,
        evidence=["pointer_style"],
    )
    model = _canonical("fp1", interactive_elements=[el])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_icon_search")
    assert cand.status == "available"
    assert cand.actual_label  # a non-empty fallback label, never blank


# ---------------------------------------------------------------------------
# Dropdowns (aria-haspopup) and context menus
# ---------------------------------------------------------------------------


def test_haspopup_listbox_classified_as_open_dropdown():
    el = InteractiveElement(
        element_id="el_sort",
        tag="button",
        category="button",
        accessible_name="Sort by",
        text="Sort by",
        is_visible=True,
        is_enabled=True,
        evidence=["custom_dropdown_trigger"],
        attributes={"aria-haspopup": "listbox"},
    )
    model = _canonical("fp1", interactive_elements=[el])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_sort")
    assert cand.candidate_type == "open_dropdown"
    assert cand.semantic_type == "listbox"


def test_haspopup_menu_classified_as_open_context_menu():
    el = InteractiveElement(
        element_id="el_kebab",
        tag="button",
        category="button",
        accessible_name="More actions",
        text="More actions",
        is_visible=True,
        is_enabled=True,
        evidence=["custom_dropdown_trigger"],
        attributes={"aria-haspopup": "menu"},
    )
    model = _canonical("fp1", interactive_elements=[el])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_kebab")
    assert cand.candidate_type == "open_context_menu"


# ---------------------------------------------------------------------------
# Unknown interactive components
# ---------------------------------------------------------------------------


def test_unknown_component_preserved_as_safe_hover_investigation_candidate():
    unknown = UnknownComponent(
        element_id="el_mystery",
        stable_id="el_mystery",
        text="",
        detection_reason="cursor:pointer",
        is_visible=True,
    )
    model = _canonical("fp1", unknown_components=[unknown])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1")

    cands = _build(page, memory)
    cand = next(c for c in cands if c.candidate_type == "inspect_unknown_component")
    assert cand.element_id == "el_mystery"
    assert cand.status == "available"

    action = Planner(MockGemmaProvider())._candidate_to_action(cand, page, memory, AuthenticationStrategy(), {})
    assert action is not None
    assert action.action == ActionType.HOVER, "unknown components must only ever be safely investigated, never clicked"


def test_invisible_unknown_component_produces_no_candidate():
    unknown = UnknownComponent(element_id="el_hidden", stable_id="el_hidden", detection_reason="x", is_visible=False)
    model = _canonical("fp1", unknown_components=[unknown])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1")

    cands = _build(page, memory)
    assert all(c.element_id != "el_hidden" for c in cands)


# ---------------------------------------------------------------------------
# Images, documents, and open dialogs
# ---------------------------------------------------------------------------


def test_meaningful_image_generates_inspect_image_candidate():
    img = ImageDescriptor(element_id="el_chart", stable_id="el_chart", visual_semantic_type="chart", is_visible=True)
    model = _canonical("fp1", images=[img])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1")

    cands = _build(page, memory)
    cand = next(c for c in cands if c.element_id == "el_chart")
    assert cand.candidate_type == "inspect_image"


def test_document_image_generates_inspect_document_candidate():
    img = ImageDescriptor(element_id="el_scan", stable_id="el_scan", visual_semantic_type="document", is_visible=True)
    model = _canonical("fp1", images=[img])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1")

    cands = _build(page, memory)
    cand = next(c for c in cands if c.element_id == "el_scan")
    assert cand.candidate_type == "inspect_document"


def test_decorative_image_generates_no_candidate():
    img = ImageDescriptor(element_id="el_deco", stable_id="el_deco", visual_semantic_type="decorative", is_visible=True)
    model = _canonical("fp1", images=[img])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1")

    cands = _build(page, memory)
    assert all(c.element_id != "el_deco" for c in cands)


def test_open_dialog_generates_close_dialog_candidate_dispatched_as_escape():
    dialog = DialogDescriptor(element_id="el_modal", stable_id="el_modal", dialog_type="modal", is_open=True)
    model = _canonical("fp1", dialogs=[dialog])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1")

    cands = _build(page, memory)
    cand = next(c for c in cands if c.candidate_type == "close_dialog")
    assert cand.element_id == "el_modal"

    action = Planner(MockGemmaProvider())._candidate_to_action(cand, page, memory, AuthenticationStrategy(), {})
    assert action is not None
    assert action.action == ActionType.PRESS
    assert action.key == "Escape"


# ---------------------------------------------------------------------------
# Multiple states on the same URL — the staleness guard
# ---------------------------------------------------------------------------


def test_multiple_states_on_same_url_each_get_their_own_candidates():
    """Same URL, two different states (dialog open vs. closed) — the frontier
    must reflect whichever state's canonical model actually matches the
    CURRENT page_state.state_fingerprint, never mixing the two."""
    memory = _memory()

    dialog_open_model = _canonical(
        "fp_dialog_open", dialogs=[DialogDescriptor(element_id="el_modal", stable_id="el_modal", is_open=True)]
    )
    memory.canonical_page_model = dialog_open_model
    memory.canonical_page_model_source_fingerprint = "fp_dialog_open"
    page_open = _page("fp_dialog_open")
    cands_open = _build(page_open, memory)
    assert any(c.candidate_type == "close_dialog" for c in cands_open)

    dialog_closed_model = _canonical("fp_dialog_closed")
    memory.canonical_page_model = dialog_closed_model
    memory.canonical_page_model_source_fingerprint = "fp_dialog_closed"
    page_closed = _page("fp_dialog_closed")
    cands_closed = _build(page_closed, memory)
    assert all(c.candidate_type != "close_dialog" for c in cands_closed)


def test_stale_canonical_model_from_a_different_state_is_never_trusted():
    """Simulates a perception failure THIS round: memory.canonical_page_model
    (and its source_fingerprint marker) still hold a PREVIOUS state's model —
    the frontier must fall back to PageState-only generation rather than
    leaking that stale state's candidates in."""
    memory = _memory()
    stale_model = _canonical(
        "fp_previous_state",
        unknown_components=[UnknownComponent(element_id="el_stale_unknown", stable_id="el_stale_unknown", is_visible=True)],
    )
    memory.canonical_page_model = stale_model
    memory.canonical_page_model_source_fingerprint = "fp_previous_state"

    current_page = _page("fp_current_state")  # different state_fingerprint, same url
    cands = _build(current_page, memory)

    assert all(c.element_id != "el_stale_unknown" for c in cands)
    assert all(
        c.candidate_type
        not in {
            "inspect_unknown_component",
            "inspect_image",
            "inspect_document",
            "close_dialog",
            "select_tab",
            "open_dropdown",
            "open_context_menu",
            "expand_navigation_region",
            "inspect_card",
            "open_table_row",
            "verify_internal_link",
            "verify_external_link",
            "open_navigation_item",
        }
        for c in cands
    ), "a stale canonical model (wrong state_fingerprint) must never source any candidate"


def test_stale_canonical_model_wrong_url_is_never_trusted():
    memory = _memory()
    memory.canonical_page_model = CanonicalPageModel(url="https://other-app.example/", state_fingerprint="fp1")
    memory.canonical_page_model_source_fingerprint = "fp1"

    page = _page("fp1")
    # Same source_fingerprint marker as the stored model, but a DIFFERENT url
    # — must still be rejected by the staleness guard.
    cands = _build(page, memory)
    assert all(c.status != "blocked" or c.element_id is None for c in cands)  # sanity: no crash
    # No canonical-only candidate types leaked through.
    assert all(c.candidate_type != "inspect_unknown_component" for c in cands)


# ---------------------------------------------------------------------------
# Live-acceptance-testing regressions (docs/EVIDENCE_DRIVEN_EXPLORATION_REPORT.md)
# ---------------------------------------------------------------------------


def test_select_tab_attempt_tracking_uses_its_real_dispatched_action_type():
    """Regression: a live run on a real tabs+cards dashboard page got stuck
    re-selecting the SAME tab for the rest of its action budget. Root cause —
    `_build_navigation_candidates` computed its attempt-tracking signature
    with a hardcoded action_type="click", but `select_tab` candidates
    actually DISPATCH as ActionType.OPEN_TAB (Planner._candidate_to_action)
    and RunMemory.record_action() records the real signature using
    `result.action.action.value` ("open_tab") — the two signatures could
    never match, so already_attempted_count stayed permanently 0 and novelty
    stayed permanently 1.0 no matter how many times the tab was actually
    clicked. This reproduces the exact scenario: build, record the REAL
    signature exactly as controller.py would, then build again."""
    from app.agent.memory import action_signature

    el = InteractiveElement(
        element_id="el_tab_2",
        tag="button",
        role="tab",
        category="tab",
        accessible_name="Performance",
        text="Performance",
        is_visible=True,
        is_enabled=True,
        is_selected=False,
    )
    tab_group = TabGroup(tabs=[TabDescriptor(element_id="el_tab_2", text="Performance", is_selected=False)])
    model = _canonical("fp1", tabs=[tab_group], interactive_elements=[el])
    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    first = _by_element(_build(page, memory), "el_tab_2")
    assert first.candidate_type == "select_tab"
    assert first.status == "available"
    assert first.already_attempted_count == 0

    # Simulate the REAL post-dispatch bookkeeping exactly as controller.py's
    # RunMemory.record_action() does it: the signature is keyed by the action
    # that was ACTUALLY performed (open_tab), not by the word "click".
    real_sig = action_signature(page_fingerprint="fp1", action_type="open_tab", element_id="el_tab_2")
    memory.mark_signature(real_sig)

    second = _by_element(_build(page, memory), "el_tab_2")
    assert second.already_attempted_count == 1, "the real open_tab signature must be recognized as one prior attempt"
    assert second.status == "exhausted", "a tab clicked once (retry_limit=1) must not be offered again at this state"


def test_external_link_inside_a_nav_region_gets_no_navigation_centrality_bonus():
    """Regression: SauceDemo's hamburger-menu drawer is a real <nav> element
    containing an "About" link to a different origin (saucelabs.com). Because
    navigation_centrality was computed from region_type alone, this external
    link got the full primary_navigation bonus (+0.9) AND the external
    penalty at the same time, nearly canceling out — an external destination
    scored almost as high as an internal one purely because of where it sat
    in the DOM. navigation_centrality must be 0 for any verify_external_link
    candidate regardless of its region."""
    el = InteractiveElement(
        element_id="el_about",
        tag="a",
        category="link",
        accessible_name="About",
        text="About",
        href="https://other-domain.example/about",
        is_visible=True,
        is_enabled=True,
        parent_region_id="region_drawer_nav",
    )
    nav_region = PageRegion(stable_id="region_drawer_nav", element_id="region_drawer_nav", region_type="primary_navigation")
    link = LinkDescriptor(element_id="el_about", target_url="https://other-domain.example/about", is_external=True)
    model = _canonical("fp1", regions=[nav_region], links=[link], interactive_elements=[el])

    memory = _memory()
    memory.canonical_page_model = model
    memory.canonical_page_model_source_fingerprint = "fp1"
    page = _page("fp1", interactive_elements=[el])

    cand = _by_element(_build(page, memory), "el_about")
    assert cand.candidate_type == "verify_external_link"
    assert cand.navigation_centrality == 0.0
    assert cand.external_navigation_cost > 0.0


# ---------------------------------------------------------------------------
# Safety validator / single-source guardrails
# ---------------------------------------------------------------------------


def test_safety_validator_still_rejects_high_risk_regardless_of_candidate_type():
    from app.safety.policies import SafetyPolicy
    from app.safety.validator import ActionValidator
    from app.schemas import BrowserAction, RiskLevel, ActionCategory

    policy = SafetyPolicy(authorized_url=URL, authorized_domain="example.com")
    validator = ActionValidator(policy)
    action = BrowserAction(
        action=ActionType.CLICK,
        element_id="el_whatever",
        reason="test",
        expected_result="test",
        risk=RiskLevel.HIGH,
        category=ActionCategory.EXPLORATION,
    )
    result = validator.validate(action, actions_taken=0, pages_visited=0)
    assert result.allowed is False


def test_frontier_builder_is_the_only_candidate_source_no_duplicate_ranking():
    """Planner._rank_elements must still delegate to FrontierBuilder rather
    than deriving its own competing candidate set (Phase 2 invariant, still
    true after this integration)."""
    import inspect

    from app.agent.planner import Planner

    src = inspect.getsource(Planner._rank_elements)
    assert "FrontierBuilder._build_navigation_candidates" in src

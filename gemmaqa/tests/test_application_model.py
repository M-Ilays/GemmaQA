"""Tests for canonical application structure foundation."""

from __future__ import annotations

from pathlib import Path

from app.application.coverage import compute_coverage
from app.application.evidence_paths import sanitize_evidence_index_entry, to_relative_evidence_path
from app.application.models import ExplorationStatus
from app.application.purpose import infer_application_purpose
from app.application.store import ApplicationStore, canonical_module_key, scenario_key
from app.application.url_normalize import normalize_url, origin_of, same_origin
from app.gemma.parser import fallback_safe_action
from app.reporting.mermaid_builder import build_navigation_mermaid
from app.reporting.report_builder import ReportBuilder
from app.schemas import (
    ActionCategory,
    ActionType,
    BrowserAction,
    FormDescriptor,
    FormField,
    InteractiveElement,
    PageClassification,
    PageState,
    RiskLevel,
)
from app.agent.memory import NavigationEdge, RunMemory
from app.utils.ids import new_id


ROOT = "https://thinking-tester-contact-list.herokuapp.com"


def _page(
    url: str,
    *,
    title: str = "Contact List App",
    heading: str = "",
    page_type: str = "unknown",
    module: str = "",
    forms: list | None = None,
    elements: list | None = None,
) -> PageState:
    return PageState(
        page_id=new_id(),
        url=url,
        title=title,
        headings=[heading] if heading else [],
        classification=PageClassification(
            page_type=page_type,
            confidence=0.8,
            purpose=heading or title,
            module_guess=module or page_type,
        ),
        forms=forms or [],
        interactive_elements=elements or [],
        state_fingerprint=f"fp-{url}",
    )


def test_url_normalization_strips_fragment_and_tracking():
    raw = f"{ROOT}/addUser/?utm_source=x&b=2&a=1#section"
    assert normalize_url(raw) == f"{ROOT}/addUser?a=1&b=2"
    assert origin_of(raw) == ROOT
    assert same_origin(f"{ROOT}/login", ROOT)
    assert not same_origin("https://www.postman.com/docs", ROOT)


def test_page_dedup_and_visit_count():
    store = ApplicationStore("run-1", f"{ROOT}/addUser")
    p1 = _page(f"{ROOT}/addUser", heading="Add User", module="Sign Up", page_type="authentication")
    a = store.observe_page(p1)
    b = store.observe_page(
        _page(f"{ROOT}/addUser/", heading="Add User", module="Sign Up", page_type="authentication")
    )
    assert a is not None and b is not None
    assert a.id == b.id
    assert len(store.model.pages) == 1
    assert store.model.pages[0].visit_count == 2


def test_module_alias_merge_authentication():
    key, name = canonical_module_key("Sign Up", "/addUser")
    assert key == "authentication"
    assert name == "Authentication"
    store = ApplicationStore("run-2", ROOT)
    store.observe_page(
        _page(f"{ROOT}/addUser", heading="Add User", module="Sign Up", page_type="authentication")
    )
    store.observe_page(
        _page(f"{ROOT}/login", heading="Login", module="Auth", page_type="authentication")
    )
    assert len(store.model.modules) == 1
    mod = store.model.modules[0]
    assert mod.canonical_key == "authentication"
    assert len(mod.page_ids) == 2


def test_module_page_counts_match_unique_pages():
    store = ApplicationStore("run-3", ROOT)
    store.observe_page(_page(f"{ROOT}/addUser", module="Sign Up", heading="Add User"))
    store.observe_page(_page(f"{ROOT}/addUser", module="Sign Up", heading="Add User"))
    store.observe_page(_page(f"{ROOT}/login", module="Auth", heading="Login"))
    legacy = store.sync_legacy_modules()
    assert len(legacy) == 1
    assert len(set(legacy[0].page_ids)) == 2
    cov = compute_coverage(store.model, actions_taken=3, action_budget=20)
    assert cov.pages_discovered == 2


def test_navigation_edge_dedup_and_labels():
    store = ApplicationStore("run-4", ROOT)
    store.observe_page(_page(f"{ROOT}/addUser", heading="Add User", module="Sign Up"))
    store.observe_page(_page(f"{ROOT}/login", heading="Login", module="Auth"))
    e1 = store.record_navigation(
        from_url=f"{ROOT}/addUser",
        to_url=f"{ROOT}/login",
        action_type="click",
        action_label="Cancel",
        element_id="el1",
        reason="nav",
    )
    e2 = store.record_navigation(
        from_url=f"{ROOT}/addUser",
        to_url=f"{ROOT}/login",
        action_type="click",
        action_label="Cancel",
        element_id="el1",
    )
    assert e1 is not None and e2 is not None
    assert e1.id == e2.id
    assert e1.occurrence_count == 2
    assert len(store.model.navigation_edges) == 1

    from app.schemas import PageState as PS

    pages = [
        PS(page_id=p.id, url=p.canonical_url, title=p.title, headings=[p.heading])
        for p in store.model.pages
    ]
    edges = [
        NavigationEdge(
            source_url=f"{ROOT}/addUser",
            target_url=f"{ROOT}/login",
            via_action="click",
            action_label="Cancel",
        )
    ]
    chart = build_navigation_mermaid(pages, edges)
    assert "Cancel" in chart
    assert chart.count("Contact List App") == 0 or "Add User" in chart or "addUser" in chart.lower() or "Add User" in chart


def test_scenario_deduplication():
    k1 = scenario_key(
        page_id="p1",
        form_fingerprint="f1",
        field_key="first-name",
        category="form",
        data_class="empty",
    )
    k2 = scenario_key(
        page_id="p1",
        form_fingerprint="f1",
        field_key="first-name",
        category="form",
        data_class="empty",
    )
    assert k1 == k2
    store = ApplicationStore("run-5", ROOT)
    page = store.observe_page(_page(f"{ROOT}/addUser", heading="Add User", module="Sign Up"))
    assert page
    a = store.add_scenario(
        title="Empty value on First Name",
        category="form",
        page_id=page.id,
        form_fp="f1",
        field_key="first-name",
        data_class="empty",
    )
    b = store.add_scenario(
        title="Empty value on First Name",
        category="form",
        page_id=page.id,
        form_fp="f1",
        field_key="first-name",
        data_class="empty",
    )
    assert a is not None
    assert b is None
    assert len(store.model.scenarios) == 1


def test_external_url_exclusion():
    store = ApplicationStore("run-6", ROOT)
    store.observe_page(
        _page(
            f"{ROOT}/addUser",
            heading="Add User",
            module="Sign Up",
            elements=[
                InteractiveElement(
                    element_id="el-ext",
                    tag="a",
                    href="https://www.postman.com/jackvony/thinking-tester/",
                    text="API docs",
                    is_visible=True,
                )
            ],
        )
    )
    assert all("postman" not in p.canonical_url for p in store.model.pages)
    assert any("postman" in r.url for r in store.model.external_references)
    filtered = store.filter_unexplored(
        [f"{ROOT}/login", "https://www.postman.com/jackvony/thinking-tester/"]
    )
    assert filtered == [f"{ROOT}/login"]


def test_evidence_path_sanitization(tmp_path: Path):
    run_root = tmp_path / "evidence" / "run-x"
    shot = run_root / "screenshots" / "a.png"
    shot.parent.mkdir(parents=True)
    shot.write_text("x")
    rel = to_relative_evidence_path(shot, run_root)
    assert not rel.lower().startswith("d:")
    assert "screenshots/a.png" in rel.replace("\\", "/")
    entry = sanitize_evidence_index_entry(
        run_id="run-x",
        evidence_id="e1",
        kind="screenshot",
        path=str(shot),
        run_root=run_root,
    )
    assert entry["public_url"].startswith("/api/runs/run-x/evidence/file/")
    assert "D:" not in entry["relative_path"] and "d:" not in entry["relative_path"]


def test_purpose_inference_contact_list():
    store = ApplicationStore("run-7", ROOT)
    store.observe_page(_page(f"{ROOT}/addUser", heading="Add User", module="Sign Up"))
    store.observe_page(_page(f"{ROOT}/login", heading="Login", module="Auth"))
    purpose, conf, evidence = infer_application_purpose(store.model)
    assert conf >= 0.55
    assert "contact" in purpose.lower()
    assert purpose != "Under analysis"
    assert evidence


def test_mock_fallback_prefers_cancel_button():
    page = _page(
        f"{ROOT}/addUser",
        heading="Add User",
        elements=[
            InteractiveElement(
                element_id="btn-cancel",
                tag="button",
                text="Cancel",
                accessible_name="Cancel",
                is_visible=True,
            )
        ],
    )
    action = fallback_safe_action(
        page_state=page,
        recent_actions=[],
        remaining_action_budget=10,
        reason="Mock provider selected a deterministic safe action.",
    )
    assert action.action == ActionType.CLICK
    assert action.element_id == "btn-cancel"
    assert "Cancel" in (action.reason or "")


def test_documentation_consistency_from_canonical_model():
    memory = RunMemory(run_id="run-doc", start_url=f"{ROOT}/addUser")
    memory.remember_page(
        _page(
            f"{ROOT}/addUser",
            heading="Add User",
            module="Sign Up",
            page_type="authentication",
            forms=[
                FormDescriptor(
                    form_id="f1",
                    fields=[FormField(element_id="fn", label="First Name", field_type="text")],
                )
            ],
            elements=[
                InteractiveElement(
                    element_id="btn-cancel",
                    tag="button",
                    text="Cancel",
                    accessible_name="Cancel",
                    is_visible=True,
                )
            ],
        ),
        explored=True,
    )
    memory.remember_page(
        _page(f"{ROOT}/login", heading="Login", module="Auth", page_type="authentication"),
        explored=True,
    )
    memory.remember_edge(
        f"{ROOT}/addUser",
        f"{ROOT}/login",
        "click",
        "btn-cancel",
        action_label="Cancel",
    )
    # revisit should not inflate page inventory
    memory.remember_page(
        _page(f"{ROOT}/addUser", heading="Add User", module="Sign Up", page_type="authentication")
    )

    report = ReportBuilder().build(memory)
    assert report.coverage is not None
    assert report.coverage.pages_discovered == 2
    assert len(report.page_inventory) == 2
    assert len(report.modules) == 1
    assert report.modules[0].name == "Authentication"
    assert len(set(report.modules[0].page_ids)) == 2
    assert "Cancel" in report.navigation_structure
    assert report.application_purpose != "Under analysis"
    assert "contact" in report.application_purpose.lower()
    # No absolute Windows paths in evidence (empty ok)
    for e in report.evidence_index:
        assert "D:\\" not in str(e.get("path", ""))
        assert "D:/" not in str(e.get("path", ""))
    # Section bodies should not duplicate H2 titles
    for title, body in report.sections_markdown.items():
        assert not body.strip().startswith(f"## {title}")


def test_coverage_executed_scenarios_not_count_not_run():
    store = ApplicationStore("run-8", ROOT)
    page = store.observe_page(_page(f"{ROOT}/addUser", heading="Add User", module="Sign Up"))
    assert page
    store.add_scenario(
        title="Empty First Name",
        category="form",
        page_id=page.id,
        field_key="first-name",
        data_class="empty",
    )
    cov = compute_coverage(store.model, actions_taken=2, action_budget=20)
    assert cov.tests_generated == 1
    assert cov.tests_executed == 0


def test_contact_list_canonical_structure_consistency():
    """Fixture-based Contact List expectations (no live network)."""
    memory = RunMemory(run_id="run-contact", start_url=f"{ROOT}/addUser")
    add_user = _page(
        f"{ROOT}/addUser",
        heading="Add User",
        module="Sign Up",
        page_type="authentication",
        forms=[
            FormDescriptor(
                form_id="signup",
                fields=[
                    FormField(element_id="fn", label="First Name", field_type="text", required=True),
                    FormField(element_id="ln", label="Last Name", field_type="text", required=True),
                    FormField(element_id="em", label="Email", field_type="email", required=True),
                    FormField(element_id="pw", label="Password", field_type="password", required=True),
                ],
            )
        ],
        elements=[
            InteractiveElement(
                element_id="btn-cancel",
                tag="button",
                text="Cancel",
                accessible_name="Cancel",
                is_visible=True,
            ),
            InteractiveElement(
                element_id="link-postman",
                tag="a",
                href="https://documenter.getpostman.com/view/example",
                text="API documentation",
                is_visible=True,
            ),
        ],
    )
    login = _page(
        f"{ROOT}/login",
        title="Contact List App",
        heading="Login",
        module="Auth",
        page_type="authentication",
        forms=[
            FormDescriptor(
                form_id="login",
                fields=[
                    FormField(element_id="em2", label="Email", field_type="email", required=True),
                    FormField(element_id="pw2", label="Password", field_type="password", required=True),
                ],
            )
        ],
        elements=[
            InteractiveElement(
                element_id="btn-signup",
                tag="button",
                text="Sign up",
                accessible_name="Sign up",
                is_visible=True,
            )
        ],
    )
    memory.remember_page(add_user, explored=True)
    memory.remember_page(login, explored=True)
    memory.remember_page(add_user, explored=True)  # revisit
    memory.remember_edge(f"{ROOT}/addUser", f"{ROOT}/login", "click", "btn-cancel", action_label="Cancel")
    memory.remember_edge(f"{ROOT}/login", f"{ROOT}/addUser", "click", "btn-signup", action_label="Sign up")
    memory.note_unexplored_url(f"{ROOT}/contactList")
    memory.app_store.mark_form_inspected("signup")

    # Generate scenarios twice — must not duplicate
    import asyncio
    from app.agent.tester import Tester

    tester = Tester()

    async def _gen() -> None:
        await tester.propose_for_page(add_user, "run-contact", app_store=memory.app_store)
        first = len(tester.scenarios)
        await tester.propose_for_page(add_user, "run-contact", app_store=memory.app_store)
        assert len(tester.scenarios) == first

    asyncio.run(_gen())
    memory.scenarios = list(tester.scenarios)
    memory.executions = list(tester.executions)

    report = ReportBuilder().build(memory)
    assert report.coverage.pages_discovered == len(report.page_inventory) == 2
    assert len(report.modules) == 1
    assert report.modules[0].name == "Authentication"
    assert len(report.modules[0].page_ids) == 2
    assert all("postman" not in (p.get("url") or "").lower() for p in report.page_inventory)
    assert all("postman" not in t.lower() for t in report.recommended_next_testing_areas)
    assert "Cancel" in report.navigation_structure
    assert "Sign up" in report.navigation_structure or "Sign Up" in report.navigation_structure
    assert report.application_purpose != "Under analysis"
    assert "contact" in report.application_purpose.lower()
    assert any("Cancel" in j for j in report.user_journeys)
    titles = [s.title for s in report.test_scenarios]
    assert len(titles) == len(set(titles)) or len(report.test_scenarios) == len(
        {getattr(s, "test_id", id(s)) for s in report.test_scenarios}
    )
    # Unique scenario keys in store
    keys = [s.scenario_key for s in memory.app_store.model.scenarios]
    assert len(keys) == len(set(keys))
    for e in report.evidence_index:
        assert "D:\\" not in str(e.get("path", ""))
        assert "D:/" not in str(e.get("path", ""))


def test_mock_continues_when_unexplored_urls_remain():
    page = _page(f"{ROOT}/login", heading="Login", module="Auth")
    action = fallback_safe_action(
        page_state=page,
        recent_actions=[{"action": "click", "element_id": "x", "success": True}],
        remaining_action_budget=1,
        reason="Mock provider selected a deterministic safe action.",
        unexplored_urls=[f"{ROOT}/addUser"],
    )
    assert action.action == ActionType.OPEN_URL
    assert action.url == f"{ROOT}/addUser"

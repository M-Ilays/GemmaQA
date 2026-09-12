"""Focused tests: canonical page discovery and coverage consistency."""

from __future__ import annotations

from app.application.coverage import canonical_pages, compute_coverage
from app.application.models import ExplorationStatus
from app.application.store import ApplicationStore
from app.application.url_normalize import normalize_url, same_origin
from app.agent.memory import RunMemory
from app.reporting.report_builder import ReportBuilder
from app.schemas import PageClassification, PageState
from app.utils.ids import new_id

ROOT = "https://thinking-tester-contact-list.herokuapp.com"


def _obs(url: str, *, title: str = "", heading: str = "") -> PageState:
    return PageState(
        page_id=new_id(),
        url=url,
        title=title or "Contact List App",
        headings=[heading] if heading else [],
        classification=PageClassification(page_type="authentication", confidence=0.9),
        state_fingerprint=f"fp-{normalize_url(url) or url}",
    )


def test_same_url_twice_one_page():
    store = ApplicationStore("pd-1", f"{ROOT}/login")
    a = store.upsert_page(_obs(f"{ROOT}/login", heading="Login"))
    b = store.upsert_page(_obs(f"{ROOT}/login", heading="Login"))
    assert a is not None and b is not None
    assert a.id == b.id
    assert len(canonical_pages(store.model)) == 1


def test_revisit_increments_visit_count_not_explored():
    store = ApplicationStore("pd-2", ROOT)
    first = store.upsert_page(_obs(f"{ROOT}/login"), explored=False)
    assert first is not None
    assert first.visit_count == 1
    assert first.exploration_status == ExplorationStatus.DISCOVERED
    second = store.upsert_page(_obs(f"{ROOT}/login"), explored=False)
    assert second is not None
    assert second.id == first.id
    assert second.visit_count == 2
    assert second.exploration_status == ExplorationStatus.DISCOVERED


def test_fragment_does_not_duplicate():
    store = ApplicationStore("pd-3", ROOT)
    store.upsert_page(_obs(f"{ROOT}/login"))
    store.upsert_page(_obs(f"{ROOT}/login#section"))
    assert len(canonical_pages(store.model)) == 1
    assert normalize_url(f"{ROOT}/login#section") == f"{ROOT}/login"


def test_trailing_slash_does_not_duplicate():
    store = ApplicationStore("pd-4", ROOT)
    store.upsert_page(_obs(f"{ROOT}/login"))
    store.upsert_page(_obs(f"{ROOT}/login/"))
    assert len(canonical_pages(store.model)) == 1


def test_external_urls_not_application_pages():
    store = ApplicationStore("pd-5", ROOT)
    page = store.upsert_page(
        _obs("https://documenter.getpostman.com/view/thinking-tester")
    )
    assert page is None
    assert len(canonical_pages(store.model)) == 0
    assert not same_origin(
        "https://documenter.getpostman.com/view/x", store.model.allowed_origin
    )


def test_coverage_discovered_equals_canonical_page_count():
    store = ApplicationStore("pd-6", ROOT)
    store.upsert_page(_obs(f"{ROOT}/"), explored=True)
    store.upsert_page(_obs(f"{ROOT}/addUser", heading="Add User"), explored=True)
    store.upsert_page(_obs(f"{ROOT}/login", heading="Login"), explored=False)
    store.register_candidate_url(f"{ROOT}/contactList")
    cov = compute_coverage(store.model)
    assert cov.pages_discovered == len(canonical_pages(store.model)) == 3
    assert cov.pages_explored == 2
    assert cov.observed_coverage_pct == round(2 / 3 * 100, 1)


def test_page_inventory_equals_coverage_discovered():
    memory = RunMemory(run_id="pd-7", start_url=f"{ROOT}/addUser")
    memory.remember_page(_obs(f"{ROOT}/addUser", heading="Add User"), explored=True)
    memory.remember_page(_obs(f"{ROOT}/login", heading="Login"), explored=True)
    memory.remember_page(_obs(f"{ROOT}/"), explored=False)
    memory.note_unexplored_url(f"{ROOT}/contactList")
    report = ReportBuilder().build(memory)
    assert report.coverage is not None
    assert len(report.page_inventory) == report.coverage.pages_discovered
    assert report.coverage.pages_discovered == 3


def test_navigation_references_canonical_page_ids():
    store = ApplicationStore("pd-8", ROOT)
    a = store.upsert_page(_obs(f"{ROOT}/addUser", heading="Add User"))
    b = store.upsert_page(_obs(f"{ROOT}/login", heading="Login"))
    assert a and b
    edge = store.record_navigation(
        from_url=f"{ROOT}/addUser",
        to_url=f"{ROOT}/login",
        action_type="click",
        action_label="Cancel",
        element_id="btn-cancel",
    )
    assert edge is not None
    assert edge.from_page_id == a.id
    assert edge.to_page_id == b.id
    # Navigation must not invent a third page
    assert len(canonical_pages(store.model)) == 2


def test_navigation_does_not_create_stub_pages():
    store = ApplicationStore("pd-9", ROOT)
    store.upsert_page(_obs(f"{ROOT}/login"))
    edge = store.record_navigation(
        from_url=f"{ROOT}/login",
        to_url=f"{ROOT}/contactList",
        action_type="click",
        action_label="Open",
        element_id="x",
    )
    # Destination not observed yet — no edge, no stub page
    assert edge is None
    assert len(canonical_pages(store.model)) == 1
    assert f"{ROOT}/contactList" in store.model.candidate_urls


def test_zero_page_coverage_safe():
    store = ApplicationStore("pd-10", ROOT)
    cov = compute_coverage(store.model)
    assert cov.pages_discovered == 0
    assert cov.pages_explored == 0
    assert cov.observed_coverage_pct == 0.0


def test_postman_never_recommended():
    memory = RunMemory(run_id="pd-postman", start_url=f"{ROOT}/")
    memory.remember_page(_obs(f"{ROOT}/", heading="Login"), explored=True)
    memory.unexplored_urls.append(
        "https://documenter.getpostman.com/view/4012288/TzK2bEa8"
    )
    memory.note_unexplored_url(f"{ROOT}/addUser")
    report = ReportBuilder().build(memory)
    joined = " ".join(report.recommended_next_testing_areas).lower()
    assert "postman" not in joined
    assert "documenter" not in joined
    assert len(report.page_inventory) == report.coverage.pages_discovered


def test_planner_overrides_premature_finish():
    from app.agent.planner import Planner
    from app.gemma.mock_provider import MockGemmaProvider
    from app.schemas import FormDescriptor, FormField, InteractiveElement

    memory = RunMemory(run_id="pd-finish", start_url=f"{ROOT}/")
    page = PageState(
        page_id=new_id(),
        url=f"{ROOT}/",
        title="Contact List App",
        headings=["Login"],
        forms=[
            FormDescriptor(
                form_id="login",
                fields=[
                    FormField(element_id="e", label="Email", field_type="email"),
                    FormField(element_id="p", label="Password", field_type="password"),
                ],
            )
        ],
        interactive_elements=[
            InteractiveElement(
                element_id="signup",
                tag="a",
                href=f"{ROOT}/addUser",
                text="Sign up",
                accessible_name="Sign up",
                is_visible=True,
            )
        ],
        classification=PageClassification(page_type="authentication", confidence=0.9),
        state_fingerprint="fp-login-home",
    )
    memory.remember_page(page, explored=False)
    memory.bootstrap_budgets(
        __import__("app.schemas", fromlist=["RunConfiguration"]).RunConfiguration(
            max_actions=40, max_pages=10
        )
    )
    planner = Planner(MockGemmaProvider())
    # Force a FINISH from provider, then ensure planner overrides
    action = planner.plan_by_priority(
        page,
        memory,
        {"authorized_domain": "thinking-tester-contact-list.herokuapp.com", "safe_mode": True},
    )
    assert action.action.value != "finish"


def test_thinking_tester_fixture_three_unique_pages():
    """Mocked Contact List observations → exactly /, /addUser, /login."""
    memory = RunMemory(run_id="pd-tt", start_url=f"{ROOT}/addUser")
    observations = [
        _obs(f"{ROOT}/addUser", heading="Add User"),
        _obs(f"{ROOT}/addUser/", heading="Add User"),  # trailing slash revisit
        _obs(f"{ROOT}/login#top", heading="Login"),
        _obs(f"{ROOT}/login", heading="Login"),
        _obs(f"{ROOT}/", title="Contact List App"),
        _obs(f"{ROOT}", title="Contact List App"),
    ]
    for obs in observations:
        memory.remember_page(obs, explored=False)
    # Meaningful action explores login only
    memory.app_store.mark_explored(f"{ROOT}/login")

    pages = canonical_pages(memory.app_store.model)
    paths = sorted(p.normalized_path for p in pages)
    assert paths == ["/", "/addUser", "/login"]
    assert len(pages) == 3

    # visit_count > 1 for revisited URLs
    by_path = {p.normalized_path: p for p in pages}
    assert by_path["/addUser"].visit_count == 2
    assert by_path["/login"].visit_count == 2
    assert by_path["/"].visit_count == 2

    report = ReportBuilder().build(memory)
    assert len(report.page_inventory) == 3
    assert report.coverage.pages_discovered == 3
    assert report.coverage.pages_explored == 1
    assert report.coverage.observed_coverage_pct == round(1 / 3 * 100, 1)

    # Edges after both ends observed
    memory.remember_edge(
        f"{ROOT}/addUser",
        f"{ROOT}/login",
        "click",
        "btn-cancel",
        action_label="Cancel",
    )
    ids = {p.id for p in pages}
    for e in memory.app_store.model.navigation_edges:
        assert e.from_page_id in ids
        assert e.to_page_id in ids


def test_navigation_to_external_does_not_crash():
    store = ApplicationStore("pd-ext", ROOT)
    page = store.upsert_page(_obs(f"{ROOT}/login"))
    assert page is not None
    edge = store.record_navigation(
        from_url=f"{ROOT}/login",
        to_url="https://orangehrm.com/",
        action_type="click",
        action_label="Help",
        element_id="el_help",
    )
    assert edge is None
    assert any("orangehrm.com" in ref.url for ref in store.model.external_references)

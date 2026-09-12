"""An admin create-user form is not a login barrier.

Found by running GemmaQA against OrangeHRM. It logged in correctly, reached
Admin -> Add User, and then spent 170 actions repeating one three-action cycle
across four pages until the operator cancelled the run.

The page is headed "Add User" and its fields are Username, Password, Confirm
Password. Three faults compounded:

  1. `REGISTER_HINTS` contains "add user", so the page classified as
     `registration`.
  2. `_looks_authenticated` let the presence of a password form cancel out the
     logout control it could see, so the agent believed it was signed out --
     inside a live session, with the sidebar and user menu on screen.
  3. `note_page` then declared SESSION_EXPIRED, cleared `authenticated`, and
     re-armed the login workflow against the admin form; `should_stop` waves
     through every stall while auth is in progress, so loop detection never ran.

Every admin panel has a create-user form carrying username and password -- this
is WordPress, Django admin, Jira, and every HR system, not one demo site.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.memory import MAX_AUTH_STALL_CHECKS, RunMemory  # noqa: E402
from app.schemas import (  # noqa: E402
    FormDescriptor,
    FormField,
    InteractiveElement,
    PageState,
    RunConfiguration,
)


def _el(element_id: str, tag: str, text: str, **kw) -> InteractiveElement:
    return InteractiveElement(
        element_id=element_id, tag=tag, type=kw.pop("type", tag),
        visible_text=text, selector=f"#{element_id}", **kw
    )


def _admin_add_user_page() -> PageState:
    """OrangeHRM Admin -> Add User, exactly as observed.

    NOTE there is no Logout element: OrangeHRM keeps it inside a collapsed user
    dropdown, so it is not in the DOM. An earlier fix keyed on a logout control
    and passed a fixture that helpfully included one — then failed live. The
    fixture must be no kinder than the real page."""
    return PageState(
        page_id="page_add_user",
        url="https://opensource-demo.orangehrmlive.com/web/index.php/admin/saveSystemUser",
        title="OrangeHRM",
        state_fingerprint="fp_add_user",
        headings=["Admin", "Add User"],
        interactive_elements=[
            _el("el_001", "a", "Admin"),
            _el("el_002", "a", "PIM"),
            _el("el_003", "a", "Leave"),
            _el("el_023", "input", "", type="text", label="Username"),
            _el("el_024", "input", "", type="password", label="Password"),
            _el("el_025", "button", "Save"),
        ],
        forms=[
            FormDescriptor(
                form_id="form_121",
                submit_element_id="el_025",
                fields=[
                    FormField(name="userRole", field_type="select", label="User Role", element_id="el_021"),
                    FormField(name="employeeName", field_type="text", label="Employee Name", element_id="el_022"),
                    FormField(name="username", field_type="text", label="Username", element_id="el_023"),
                    FormField(name="password", field_type="password", label="Password", element_id="el_024"),
                    FormField(name="confirm", field_type="password", label="Confirm Password", element_id="el_027"),
                ],
            )
        ],
    )


def _real_login_page() -> PageState:
    """A genuine login screen: no session shell, no logout, nothing but the form."""
    return PageState(
        page_id="page_login",
        url="https://opensource-demo.orangehrmlive.com/web/index.php/auth/login",
        title="OrangeHRM",
        state_fingerprint="fp_login",
        headings=["Login"],
        interactive_elements=[
            _el("el_002", "input", "", type="text", label="Username"),
            _el("el_003", "input", "", type="password", label="Password"),
            _el("el_004", "button", "Login"),
        ],
        forms=[
            FormDescriptor(
                form_id="form_login",
                submit_element_id="el_004",
                fields=[
                    FormField(name="username", field_type="text", label="Username", element_id="el_002"),
                    FormField(name="password", field_type="password", label="Password", element_id="el_003"),
                ],
            )
        ],
    )


def _authenticated() -> AuthenticationStrategy:
    auth = AuthenticationStrategy()
    auth.authenticated = True
    return auth


# ===========================================================================
# A — the page is recognised as part of a live session
# ===========================================================================


def test_an_admin_create_user_page_is_not_classified_as_an_auth_page():
    """It classified as `registration`, because the heading says "Add User" and
    REGISTER_HINTS contains that phrase. Only the FORM can settle it: this one
    also asks for User Role, Employee Name and Status, which no login or sign-up
    form does."""
    assert _authenticated().classify_page(_admin_add_user_page()) == "authenticated"


def test_a_real_login_page_is_still_classified_as_login():
    """The fix must not blind the agent to actual login screens."""
    assert _authenticated().classify_page(_real_login_page()) == "login"


def test_the_admin_form_is_not_credentials_only_but_a_login_form_is():
    auth = _authenticated()
    admin_form = _admin_add_user_page().forms[0]
    login_form = _real_login_page().forms[0]

    assert auth._is_credentials_only_form(admin_form) is False
    assert auth._is_credentials_only_form(login_form) is True


def test_a_public_sign_up_form_is_still_credentials_only():
    """Registration asks for the new user's own name — that must NOT be mistaken
    for the business fields of an admin form, or sign-up would stop working."""
    signup = FormDescriptor(
        form_id="form_signup",
        submit_element_id="el_005",
        fields=[
            FormField(name="firstName", field_type="text", label="First Name", element_id="el_001"),
            FormField(name="lastName", field_type="text", label="Last Name", element_id="el_002"),
            FormField(name="email", field_type="text", label="Email", element_id="el_003"),
            FormField(name="password", field_type="password", label="Password", element_id="el_004"),
        ],
    )
    assert _authenticated()._is_credentials_only_form(signup) is True


# ===========================================================================
# B — an authenticated session survives the admin form
# ===========================================================================


def test_the_session_is_not_thrown_away_on_an_admin_create_user_page():
    """THE bug: the agent concluded it had been logged out, cleared
    `authenticated`, and re-armed the login workflow against this form."""
    auth = _authenticated()
    auth.note_page(_admin_add_user_page())

    assert auth.authenticated is True
    assert auth.status.value != "session_expired"


def test_a_genuine_session_expiry_is_still_detected():
    """The other half of the guarantee — being bounced back to a bare login page
    must still register as expiry, or a lost session would go unnoticed."""
    auth = _authenticated()
    auth.note_page(_real_login_page())

    assert auth.authenticated is False
    assert auth.status.value == "session_expired"


def test_the_admin_form_is_not_offered_as_a_login_form_to_fill():
    """It filled this form's username and clicked Save believing it was
    submitting a login, 55 times."""
    auth = _authenticated()
    page = _admin_add_user_page()
    auth.note_page(page)

    blocker = auth.unresolved_auth_blocker(page, allow_login=True, allow_registration=True)
    assert blocker is None


# ===========================================================================
# C — loop detection can no longer be disabled for the whole run
# ===========================================================================


def _stalled_memory(auth_status: str) -> RunMemory:
    memory = RunMemory(
        run_id="run_1",
        start_url="https://app.example.com/",
        configuration=RunConfiguration(max_actions=500, max_pages=200),
    )
    memory.authenticated = False
    memory.auth_status = auth_status
    memory.no_progress_streak = 99  # unmistakably stalled
    return memory


def test_an_in_progress_login_is_given_room_before_the_run_gives_up():
    """A real multi-step login must not be mistaken for a stall."""
    memory = _stalled_memory("submitted")
    stop, _ = memory.should_stop(no_progress_limit=5, build_frontier=lambda: [])
    assert stop is False


def test_a_misdiagnosed_auth_state_cannot_stall_the_run_forever():
    """It used to return "do not stop" unconditionally, so one wrong auth
    reading disabled loop detection for the whole run — 170 actions on four
    pages, ended only by the operator."""
    memory = _stalled_memory("session_expired")
    for _ in range(MAX_AUTH_STALL_CHECKS):
        assert memory.should_stop(no_progress_limit=5, build_frontier=lambda: [])[0] is False

    stop, reason = memory.should_stop(no_progress_limit=5, build_frontier=lambda: [])
    assert stop is True
    assert reason == "unresolved_authentication"


def test_the_allowance_is_only_spent_while_stalled():
    """A healthy run must never accumulate toward the cap, or a long normal run
    would eventually be cut off for an auth problem it does not have."""
    memory = _stalled_memory("submitted")
    memory.no_progress_streak = 0

    for _ in range(MAX_AUTH_STALL_CHECKS * 3):
        assert memory.should_stop(no_progress_limit=5, build_frontier=lambda: [])[0] is False
    assert memory.auth_stall_checks == 0


def test_an_authenticated_run_is_unaffected_by_the_cap():
    memory = _stalled_memory("authenticated")
    memory.authenticated = True
    # Falls through to the ordinary loop/stall handling rather than the auth path.
    assert memory.auth_stall_checks == 0
    memory.should_stop(no_progress_limit=5, build_frontier=lambda: [])
    assert memory.auth_stall_checks == 0


# ===========================================================================
# D — the create control is not offered again after it has been clicked
#
# Every other generator in frontier.py checks `has_seen_signature`; the
# safe-test-data-create generator did not. So the same "Add" control was
# re-offered after every click — 21 times in 70 actions on OrangeHRM — and
# because each click counted as progress, the stall counter never rose and loop
# detection never engaged.
# ===========================================================================


def _admin_list_page() -> PageState:
    return PageState(
        page_id="page_users",
        url="https://opensource-demo.orangehrmlive.com/web/index.php/admin/viewSystemUsers",
        title="OrangeHRM",
        state_fingerprint="fp_users",
        headings=["Admin", "System Users"],
        # "Add User", not "Add": the generator matches on "add " WITH a trailing
        # space, so a lone "Add" only matches live because the same label arrives
        # in several text fields at once and joins to "add add".
        interactive_elements=[_el("el_026", "button", "Add User")],
    )


def _authenticated_builder():
    from app.agent.frontier import FrontierBuilder

    return FrontierBuilder(_authenticated())


def _memory() -> RunMemory:
    return RunMemory(
        run_id="run_1",
        start_url="https://opensource-demo.orangehrmlive.com/",
        configuration=RunConfiguration(max_actions=500, max_pages=200),
    )


def test_the_create_control_is_offered_when_it_has_not_been_clicked():
    candidates = _authenticated_builder().build(
        _admin_list_page(), allow_safe_test_data=True, memory=_memory()
    )
    assert any(c.candidate_type == "safe_test_data_create" for c in candidates)


def test_the_create_control_is_not_offered_again_after_it_was_clicked():
    from app.agent.memory import action_signature

    memory = _memory()
    page = _admin_list_page()
    memory.mark_signature(
        action_signature(
            page_fingerprint=page.state_fingerprint, action_type="click", element_id="el_026"
        )
    )

    candidates = _authenticated_builder().build(page, allow_safe_test_data=True, memory=memory)
    assert not any(c.candidate_type == "safe_test_data_create" for c in candidates)


# ===========================================================================
# E — a nav item already visited is not offered again
#
# A sidebar item appears on every page, and candidate attempt counts are signed
# with the page fingerprint — so the count reset on every page and the run kept
# returning to the same nav item between modules: 12 clicks on one sidebar link
# in 59 actions on OrangeHRM.
# ===========================================================================


def test_visited_is_asked_the_same_way_it_is_recorded():
    """A raw href checked against normalized `visited_urls` would always answer
    "no", which is how this kind of check silently does nothing."""
    memory = _memory()
    memory.visited_urls.add("https://opensource-demo.orangehrmlive.com/web/index.php/admin/viewSystemUsers")

    assert memory.has_visited("https://opensource-demo.orangehrmlive.com/web/index.php/admin/viewSystemUsers/")
    assert memory.has_visited(
        "https://opensource-demo.orangehrmlive.com/web/index.php/admin/viewSystemUsers#top"
    )
    assert not memory.has_visited("https://opensource-demo.orangehrmlive.com/web/index.php/pim/viewEmployeeList")
    assert not memory.has_visited(None)
    assert not memory.has_visited("")


def test_the_frontier_judges_a_nav_item_by_its_destination():
    from pathlib import Path as _P

    source = (_P(__file__).resolve().parents[1] / "backend/app/agent/frontier.py").read_text(encoding="utf-8")
    assert "memory.has_visited(nav_target)" in source


# ===========================================================================
# F — a navigation item already followed is not offered again
#
# Action signatures are keyed on the page fingerprint, which is right for a
# button but wrong for persistent chrome: a sidebar item appears on every page,
# so its count reset each time and the run returned to it between almost every
# module — 11 clicks on one sidebar link in 52 actions on OrangeHRM.
#
# Judged by DESTINATION when there is one, and by LABEL when there is not (a
# button-driven SPA route). "Admin" is the same navigation item wherever it is
# clicked from.
# ===========================================================================


def _nav_click(label: str, candidate_id: str = "nav_el_004"):
    from app.schemas import ActionCategory, ActionResult, ActionType, BrowserAction, RiskLevel

    return ActionResult(
        action_id="a1", run_id="run_1", success=True,
        action=BrowserAction(
            action=ActionType.CLICK, element_id="el_004", reason="open_navigation_item",
            expected_result="A module opens.", risk=RiskLevel.LOW,
            category=ActionCategory.NAVIGATION_TEST,
            metadata={"candidate_id": candidate_id, "action_label": label},
        ),
    )


def test_following_a_navigation_item_is_remembered_by_label():
    memory = _memory()
    memory.note_navigation_taken(_nav_click("Admin").action)

    assert memory.has_taken_navigation("Admin")
    assert memory.has_taken_navigation("  admin  ")  # case and padding are not identity
    assert not memory.has_taken_navigation("PIM")


def test_it_is_remembered_wherever_the_run_records_actions():
    """Computed in one place and counted in another is what made the first
    attempt at this fix a silent no-op."""
    memory = _memory()
    memory.remember_action(_nav_click("Admin"), before_fingerprint="fp_a", made_progress=True)

    assert memory.has_taken_navigation("Admin")


def test_a_non_navigation_click_is_not_remembered_as_navigation():
    """Only navigation candidates carry this identity; an ordinary button on one
    page is genuinely a different action on another."""
    memory = _memory()
    memory.note_navigation_taken(_nav_click("Save", candidate_id="safe_create_el_009").action)

    assert not memory.has_taken_navigation("Save")


def test_an_unlabelled_navigation_click_records_nothing():
    memory = _memory()
    memory.note_navigation_taken(_nav_click("").action)

    assert memory.navigation_labels_taken == set()


def test_an_empty_label_never_counts_as_already_taken():
    """Otherwise every unlabelled control would look visited."""
    memory = _memory()
    memory.note_navigation_taken(_nav_click("Admin").action)

    assert not memory.has_taken_navigation("")
    assert not memory.has_taken_navigation(None)


def test_the_frontier_falls_back_to_the_label_when_there_is_no_destination():
    from pathlib import Path as _P

    source = (_P(__file__).resolve().parents[1] / "backend/app/agent/frontier.py").read_text(
        encoding="utf-8"
    )
    assert "memory.has_taken_navigation(" in source
    assert "memory.has_visited(nav_target)" in source

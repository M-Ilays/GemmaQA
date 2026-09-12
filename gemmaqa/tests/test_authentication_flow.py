"""Authentication strategy, frontier, and stopping-policy tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.credentials import CredentialProfile, CredentialVault  # noqa: E402
from app.agent.form_lifecycle import FormLifecycle  # noqa: E402
from app.agent.frontier import (  # noqa: E402
    FrontierBuilder,
    pending_cleanup_blocks_logout,
    this_run_owns_temporary_records,
)
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.temporary_record_registry import CleanupPlan, TemporaryRecordRegistry  # noqa: E402
from app.agent.planner import Planner  # noqa: E402
from app.browser.executor import ActionExecutor  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.gemma.base import ActionGenerationRequest  # noqa: E402
from app.reporting.report_builder import ReportBuilder  # noqa: E402
from app.safety.policies import SafetyPolicy  # noqa: E402
from app.safety.validator import ActionValidator  # noqa: E402
from app.schemas import (  # noqa: E402
    ActionType,
    BrowserAction,
    FormDescriptor,
    FormField,
    InteractiveElement,
    PageState,
    RiskLevel,
)
from app.utils.ids import new_id  # noqa: E402


def _login_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://example.com/login",
        title="Login",
        headings=["Login"],
        forms=[
            FormDescriptor(
                form_id="login_form",
                fields=[
                    FormField(element_id="el_email", label="Email", field_type="email", required=True),
                    FormField(
                        element_id="el_pass",
                        label="Password",
                        field_type="password",
                        required=True,
                    ),
                ],
                submit_element_id="el_submit",
            )
        ],
        interactive_elements=[
            InteractiveElement(
                element_id="el_email",
                tag="input",
                input_type="email",
                category="input",
                accessible_name="Email",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_pass",
                tag="input",
                input_type="password",
                category="input",
                accessible_name="Password",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_submit",
                tag="button",
                role="button",
                category="button",
                accessible_name="Submit",
                text="Submit",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_signup",
                tag="a",
                category="link",
                accessible_name="Sign up",
                text="Sign up",
                href="/addUser",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


def _register_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://example.com/addUser",
        title="Sign Up",
        headings=["Sign Up"],
        forms=[
            FormDescriptor(
                form_id="registration_form",
                fields=[
                    FormField(element_id="el_fn", label="First Name", field_type="text", required=True),
                    FormField(element_id="el_ln", label="Last Name", field_type="text", required=True),
                    FormField(element_id="el_email", label="Email", field_type="email", required=True),
                    FormField(
                        element_id="el_pass",
                        label="Password",
                        field_type="password",
                        required=True,
                    ),
                ],
                submit_element_id="el_submit",
            )
        ],
        interactive_elements=[
            InteractiveElement(
                element_id="el_fn",
                tag="input",
                input_type="text",
                category="input",
                accessible_name="First Name",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_ln",
                tag="input",
                input_type="text",
                category="input",
                accessible_name="Last Name",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_email",
                tag="input",
                input_type="email",
                category="input",
                accessible_name="Email",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_pass",
                tag="input",
                input_type="password",
                category="input",
                accessible_name="Password",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_submit",
                tag="button",
                role="button",
                accessible_name="Submit",
                text="Submit",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


def _authed_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://example.com/contactList",
        title="Contact List",
        headings=["Contact List"],
        tables=[],
        interactive_elements=[
            InteractiveElement(
                element_id="el_logout",
                tag="a",
                accessible_name="Logout",
                text="Logout",
                href="/logout",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_add",
                tag="a",
                accessible_name="Add Contact",
                text="Add Contact",
                href="/addContact",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


def test_inspect_does_not_mark_form_fully_explored():
    auth = AuthenticationStrategy()
    page = _login_page()
    forms = auth.detect_forms(page)
    assert forms
    fid = forms[0].form_id
    auth.note_form_inspected(fid)
    assert auth.form_lifecycle[fid] == FormLifecycle.POSITIVE_SUBMISSION_AVAILABLE
    assert auth.form_still_actionable(fid)


def test_login_candidate_when_credentials_exist():
    vault = CredentialVault()
    vault.store(
        CredentialProfile("primary", "user@example.com", "Secret123!"),
        make_active=True,
    )
    auth = AuthenticationStrategy(vault)
    page = _login_page()
    cands = FrontierBuilder(auth).build(page, allow_login=True, allow_registration=True)
    types = {c.candidate_type for c in cands}
    assert "authenticate_with_credentials" in types


def test_registration_candidate_without_credentials():
    auth = AuthenticationStrategy()
    page = _register_page()
    cands = FrontierBuilder(auth).build(page, allow_login=True, allow_registration=True)
    types = {c.candidate_type for c in cands}
    assert "create_test_account" in types


def _feedback_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://example.com/feedback",
        title="Feedback",
        headings=["Feedback"],
        forms=[
            FormDescriptor(
                form_id="feedback_form",
                fields=[
                    FormField(element_id="el_comment", label="Comment", field_type="text", required=True),
                ],
                submit_element_id="el_fsubmit",
            )
        ],
        interactive_elements=[
            InteractiveElement(
                element_id="el_comment",
                tag="input",
                input_type="text",
                category="input",
                accessible_name="Comment",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_fsubmit",
                tag="button",
                role="button",
                category="button",
                accessible_name="Submit",
                text="Submit",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


def test_inspect_form_candidate_survives_real_observe_then_plan_ordering():
    """Regression: production always calls memory.remember_page() (which adds every
    form's id to memory.known_form_ids the instant it's first seen) before
    Planner._select_auth_candidate() runs. _build_full_frontier used to pass
    inspected_form_ids=set(memory.known_form_ids) — "every form ever seen" — so a form
    was excluded from inspect_form candidate generation the moment it was observed,
    before it could ever actually be inspected. Real per-form inspection state lives in
    auth.form_lifecycle (DISCOVERED -> INSPECTED via note_form_inspected()), which must
    be the only gate."""
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = AuthenticationStrategy()
    memory.remaining_action_budget = 20
    page = _feedback_page()

    memory.remember_page(page, explored=False)
    assert "feedback_form" in memory.known_form_ids

    planner = Planner(MockGemmaProvider())
    action = planner._select_auth_candidate(page, memory, {})
    assert action is not None
    assert action.action == ActionType.INSPECT_FORM
    assert action.element_id == "feedback_form"

    # Once genuinely inspected, the same form must not be offered again.
    memory.auth_strategy.note_form_inspected("feedback_form")
    action2 = planner._select_auth_candidate(page, memory, {})
    assert action2 is None or action2.action != ActionType.INSPECT_FORM


def test_login_page_with_forgot_password_link_not_misclassified():
    """A "Forgot Password?" link is near-universal on login pages and must not cause
    the whole page to be classified as a password-reset flow — only the page's own
    title/headings/path should drive that classification, not every interactive
    element's text (which previously included incidental links)."""
    auth = AuthenticationStrategy()
    page = _login_page()
    page.interactive_elements.append(
        InteractiveElement(
            element_id="el_forgot",
            tag="a",
            accessible_name="Forgot Password?",
            text="Forgot Password?",
            href="/forgot-password",
            is_visible=True,
            is_enabled=True,
        )
    )
    assert auth.classify_page(page) == "login"


def test_actual_reset_page_still_classified_correctly():
    auth = AuthenticationStrategy()
    page = PageState(
        page_id=new_id(),
        url="https://example.com/forgot-password",
        title="Forgot Password",
        headings=["Forgot Password?"],
    )
    assert auth.classify_page(page) == "password_reset"


def test_inconclusive_login_result_allows_bounded_retry():
    """A submit with zero signals either way (no error text, no redirect, form still
    present) is often just a slow async sign-in — the form must stay retriable for a
    bounded number of attempts rather than being marked attempted forever after one
    ambiguous result."""
    vault = CredentialVault()
    vault.store(CredentialProfile("primary", "user@example.com", "Secret123!"), make_active=True)
    auth = AuthenticationStrategy(vault)
    page = _login_page()
    form = auth.detect_forms(page)[0]
    wf = auth.build_login_workflow(page, form)
    assert wf
    auth.form_lifecycle[form.form_id] = FormLifecycle.POSITIVE_SUBMISSION_ATTEMPTED

    # Same page, same form, no signals — inconclusive.
    result = auth.evaluate_after_submit(page, page, method="login")
    assert result.authenticated is False
    assert auth.form_lifecycle[form.form_id] == FormLifecycle.POSITIVE_SUBMISSION_AVAILABLE
    assert auth.active_workflow is None

    # Second inconclusive attempt exhausts retries and gives a truthful blocker.
    wf2 = auth.build_login_workflow(page, form)
    assert wf2
    auth.form_lifecycle[form.form_id] = FormLifecycle.POSITIVE_SUBMISSION_ATTEMPTED
    result2 = auth.evaluate_after_submit(page, page, method="login")
    assert result2.authenticated is False
    assert auth.form_lifecycle[form.form_id] == FormLifecycle.EXHAUSTED
    assert auth.blocker == "authentication_failed"


def test_extracts_credentials_displayed_on_login_page():
    """Some demo/staging apps publish working test credentials directly on the login
    page (SauceDemo's exact pattern). When no credentials were supplied, GemmaQA should
    read and use what the page itself already displays, rather than only knowing how to
    self-register a brand-new account."""
    auth = AuthenticationStrategy()
    page = PageState(
        page_id=new_id(),
        url="https://www.saucedemo.com/",
        title="Swag Labs",
        visible_text_summary=(
            "Swag Labs\nUsername\nPassword\nLogin\n"
            "Accepted usernames are:\nstandard_user\nlocked_out_user\nproblem_user\n"
            "performance_glitch_user\nerror_user\nvisual_user\n"
            "Password for all users:\nsecret_sauce"
        ),
    )
    profile = auth.extract_displayed_credentials(page)
    assert profile is not None
    assert profile.username == "standard_user"
    assert profile.password == "secret_sauce"
    assert profile.source == "page_displayed"


def test_note_page_adopts_displayed_credentials_when_none_supplied():
    auth = AuthenticationStrategy()  # empty vault — nothing supplied
    page = _login_page()
    page.visible_text_summary = (
        "Accepted usernames are:\nstandard_user\nPassword for all users:\nsecret_sauce"
    )
    auth.note_page(page)
    assert auth.vault.get() is not None
    assert auth.vault.get().username == "standard_user"
    assert auth.status.value == "ready"


def test_displayed_credentials_never_override_real_supplied_ones():
    vault = CredentialVault()
    vault.store(CredentialProfile("primary", "real.user@example.com", "RealPass1!"), make_active=True)
    auth = AuthenticationStrategy(vault)
    page = _login_page()
    page.visible_text_summary = (
        "Accepted usernames are:\nstandard_user\nPassword for all users:\nsecret_sauce"
    )
    auth.note_page(page)
    assert auth.vault.get().username == "real.user@example.com"


def test_no_displayed_credentials_extracted_from_plain_login_page():
    """The bare word "Username"/"Password" as field labels (no actual values shown)
    must not be mistaken for displayed credentials."""
    auth = AuthenticationStrategy()
    page = _login_page()
    page.visible_text_summary = "Swag Labs\nUsername\nPassword\nLogin"
    assert auth.extract_displayed_credentials(page) is None


def test_username_field_named_user_name_not_rejected():
    """Regression: a real username field whose `name` attribute is "user-name" (SauceDemo's
    actual markup) was being rejected because the field-mapping heuristic used a bare
    "name" substring check to exclude first/last-name fields — which also matched inside
    "user-name" itself. Meanwhile a `<input type="submit" name="login-button">` was
    wrongly accepted as the username field because "login-button" contains "login" and
    happens not to contain "name"."""
    vault = CredentialVault()
    vault.store(CredentialProfile("primary", "standard_user", "secret_sauce"), make_active=True)
    auth = AuthenticationStrategy(vault)
    page = PageState(
        page_id=new_id(),
        url="https://example.com/",
        title="Swag Labs",
        forms=[
            FormDescriptor(
                form_id="login_form",
                fields=[
                    FormField(element_id="el_001", name="user-name", field_type="text"),
                    FormField(element_id="el_002", name="password", field_type="password"),
                ],
                submit_element_id="el_003",
            )
        ],
    )
    form = auth.detect_forms(page)[0]
    wf = auth.build_login_workflow(page, form)
    assert wf is not None
    fills = {s.element_id: s for s in wf.steps if s.action == ActionType.FILL}
    assert "el_001" in fills, "real username field must receive a fill step"
    assert (fills["el_001"].metadata or {}).get("credential_ref", "").endswith("username")
    # The submit control itself must never be treated as a fillable field, even if it
    # were (incorrectly) present in the fields list.
    submit_like_field = FormField(
        element_id="el_003", name="login-button", field_type="submit"
    )
    ref, value = auth._map_field_value(submit_like_field, vault.get(), method="login", used=set())
    assert ref is None
    assert value is None


def test_no_fake_registration_when_real_credentials_supplied():
    """Landing on a signup page while real (user-supplied) credentials are already
    available must not spend the run creating a decoy account — only the anonymous
    (self-registered) flow should ever do that."""
    vault = CredentialVault()
    vault.store(
        CredentialProfile("primary", "real.user@example.com", "RealPass123!", source="supplied"),
        make_active=True,
    )
    auth = AuthenticationStrategy(vault)
    page = _register_page()
    cands = FrontierBuilder(auth).build(page, allow_login=True, allow_registration=True)
    types = {c.candidate_type for c in cands}
    assert "create_test_account" not in types


def test_raw_credentials_never_in_gemma_snapshot():
    vault = CredentialVault()
    vault.store(CredentialProfile("primary", "user@example.com", "SuperSecret!"), make_active=True)
    auth = AuthenticationStrategy(vault)
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = auth
    snap = memory.memory_snapshot()
    blob = str(snap).lower()
    assert "supersecret" not in blob
    assert snap.get("credential_profile_available") is True
    assert snap.get("credential_profile_id") == "primary"


@pytest.mark.asyncio
async def test_mock_provider_opens_registration_without_creds():
    provider = MockGemmaProvider()
    page = _login_page()
    action = await provider.generate_action(
        ActionGenerationRequest(
            page_state=page,
            memory={"credential_profile_available": False, "authenticated": False},
            remaining_action_budget=20,
        )
    )
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_signup"


@pytest.mark.asyncio
async def test_planner_chooses_login_with_credentials():
    vault = CredentialVault()
    vault.store(CredentialProfile("primary", "a@b.com", "Pass123!Aa"), make_active=True)
    auth = AuthenticationStrategy(vault)
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = auth
    memory.remaining_action_budget = 40
    planner = Planner(MockGemmaProvider())
    page = _login_page()
    action = await planner.next_action(
        page,
        [],
        [],
        {"allow_login": True, "allow_test_account_creation": True, "safe_mode": True},
        memory=memory,
    )
    assert action.action == ActionType.FILL
    assert (action.metadata or {}).get("auth_write") is True
    assert "password" not in (action.value or "").lower() or (action.metadata or {}).get(
        "credential_ref"
    )


@pytest.mark.asyncio
async def test_planner_chooses_registration_without_credentials():
    auth = AuthenticationStrategy()
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = auth
    memory.remaining_action_budget = 40
    planner = Planner(MockGemmaProvider())
    page = _register_page()
    action = await planner.next_action(
        page,
        [],
        [],
        {"allow_login": True, "allow_test_account_creation": True, "safe_mode": True},
        memory=memory,
    )
    assert action.action == ActionType.FILL
    assert (action.metadata or {}).get("auth_method") == "registration"
    assert auth.vault.get("generated") is not None
    # Unique email per run
    assert "@example.com" in (auth.vault.get("generated").email or "")


@pytest.mark.asyncio
async def test_login_fill_submit_via_adapter():
    vault = CredentialVault()
    vault.store(CredentialProfile("primary", "a@b.com", "Pass123!Aa"), make_active=True)
    auth = AuthenticationStrategy(vault)
    page = _login_page()
    form = auth.detect_forms(page)[0]
    wf = auth.build_login_workflow(page, form)
    assert wf and len(wf.steps) >= 3

    adapter = MagicMock()
    adapter.name = "direct_playwright"
    adapter.capabilities.return_value = MagicMock(
        **{k: True for k in [
            "navigate", "click", "fill", "select", "check", "press", "hover",
            "go_back", "reload", "wait", "screenshots", "console_events",
            "network_events", "tabs", "accessibility_snapshot", "observe", "submit",
        ]}
    )
    # capabilities() returns object with attributes
    from app.browser.adapters.types import AdapterCapabilities

    adapter.capabilities = MagicMock(return_value=AdapterCapabilities())
    adapter.get_current_url = AsyncMock(return_value=page.url)
    adapter.get_console_events = AsyncMock(return_value=[])
    adapter.get_network_events = AsyncMock(return_value=[])
    adapter.resolve_target = AsyncMock(side_effect=lambda t: t)
    adapter.fill_target = AsyncMock()
    adapter.click_target = AsyncMock()
    adapter.wait = AsyncMock()
    adapter.wait_for_page_stable = AsyncMock()
    adapter.get_page_title = AsyncMock(return_value="Login")

    executor = ActionExecutor(new_id(), adapter=adapter, credential_vault=vault)
    fill = wf.steps[0]
    result = await executor.execute(action=fill, page_state=page, capture_evidence=False)
    assert result.success
    adapter.fill_target.assert_awaited()
    # Password step resolves from vault
    pwd_step = next(s for s in wf.steps if (s.metadata or {}).get("credential_ref", "").endswith("password"))
    await executor.execute(action=pwd_step, page_state=page, capture_evidence=False)
    args = adapter.fill_target.await_args_list[-1].args
    assert args[1] == "Pass123!Aa"


def test_auth_success_multi_signal():
    auth = AuthenticationStrategy()
    before = _login_page()
    after = _authed_page()
    result = auth.evaluate_after_submit(before, after, method="login")
    assert result.authenticated is True
    assert result.confidence >= 0.5
    assert len(result.signals) >= 2
    assert auth.authenticated is True
    assert auth.checkpoint_url == after.url


def test_checkpoint_saved_on_success():
    auth = AuthenticationStrategy()
    auth.evaluate_after_submit(_login_page(), _authed_page(), method="registration")
    assert auth.checkpoint_url
    assert auth.status.value == "authenticated"


def test_frontier_repopulated_after_auth():
    auth = AuthenticationStrategy()
    auth.authenticated = True
    page = _authed_page()
    cands = FrontierBuilder(auth).build(
        page, allow_safe_test_data=True, unexplored_urls=["https://example.com/addContact"]
    )
    types = {c.candidate_type for c in cands}
    assert "open_url" in types or "safe_test_data_create" in types


def test_frontier_exhaustion_not_success_while_unauthenticated():
    auth = AuthenticationStrategy()
    page = _login_page()
    auth.detect_forms(page)
    for fid in list(auth.form_lifecycle):
        auth.form_lifecycle[fid] = FormLifecycle.POSITIVE_SUBMISSION_AVAILABLE
    blocker = auth.unresolved_auth_blocker(
        page, allow_login=True, allow_registration=True
    )
    assert blocker in {
        "credentials_missing",
        "candidate_generation_failure",
        "authentication_required",
    }


def test_failed_auth_truthful_blocker():
    auth = AuthenticationStrategy()
    before = _login_page()
    after = _login_page()
    after.visible_text_summary = "Incorrect email or password"
    after.alerts = ["Incorrect email or password"]
    result = auth.evaluate_after_submit(before, after, method="login")
    assert result.authenticated is False
    assert result.rejected is True or auth.blocker == "authentication_failed" or "error" in " ".join(
        result.signals
    ).lower() or "incorrect" in (after.visible_text_summary or "").lower()


def test_delete_remains_blocked():
    policy = SafetyPolicy(
        authorized_url="https://example.com",
        authorized_domain="example.com",
        safe_mode=True,
        allow_login=True,
    )
    validator = ActionValidator(policy)
    page = _authed_page()
    page.interactive_elements.append(
        InteractiveElement(
            element_id="el_del",
            tag="button",
            accessible_name="Delete",
            text="Delete",
            is_visible=True,
            is_enabled=True,
        )
    )
    action = BrowserAction(
        action=ActionType.CLICK,
        element_id="el_del",
        reason="Delete contact",
        risk=RiskLevel.LOW,
    )
    result = validator.validate(action, page_state=page)
    assert result.allowed is False


def test_auth_password_fill_allowed_with_flag():
    policy = SafetyPolicy(
        authorized_url="https://example.com",
        authorized_domain="example.com",
        safe_mode=True,
        allow_login=True,
        allow_controlled_writes=False,
    )
    validator = ActionValidator(policy)
    page = _login_page()
    action = BrowserAction(
        action=ActionType.FILL,
        element_id="el_pass",
        value="",
        reason="Authentication write: fill Password",
        metadata={
            "auth_write": True,
            "auth_method": "login",
            "credential_ref": "primary.password",
            "risk_class": "authentication_write",
        },
    )
    result = validator.validate(action, page_state=page)
    assert result.allowed is True
    assert result.action_level.value == "authentication_write"


def test_report_distinguishes_anonymous_vs_authenticated():
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.anonymous_page_count = 2
    memory.authenticated_page_count = 3
    memory.authenticated = True
    memory.auth_status = "authenticated"
    memory.auth_method = "login"
    report = ReportBuilder().build(memory)
    section = report.sections_markdown.get("Coverage Summary") or ""
    assert "anonymous" in section.lower()
    assert "authenticated" in section.lower()
    assert "100% coverage" not in section.lower() or "not complete" in section.lower()


def test_login_form_retriable_after_session_expiry():
    """Regression: once a login form SUCCEEDED, form_lifecycle stayed SUCCEEDED forever,
    which made `_form_actionable()` refuse it permanently — including after a completely
    legitimate logout where the same credentials should be able to log back in to keep
    exploring. Session expiry must reset that form back to actionable."""
    vault = CredentialVault()
    vault.store(CredentialProfile("primary", "standard_user", "secret_sauce"), make_active=True)
    auth = AuthenticationStrategy(vault)
    login_page = _login_page()
    form = auth.detect_forms(login_page)[0]
    auth.form_lifecycle[form.form_id] = FormLifecycle.SUCCEEDED
    auth.authenticated = True

    # Session ends — the same login page (same form_id) reappears.
    auth.note_page(login_page)

    assert auth.authenticated is False
    assert auth.form_lifecycle[form.form_id] != FormLifecycle.SUCCEEDED
    assert auth.form_still_actionable(form.form_id)
    cands = FrontierBuilder(auth).build(login_page, allow_login=True, allow_registration=True)
    assert "authenticate_with_credentials" in {c.candidate_type for c in cands}


def test_session_expiry_detection():
    from app.agent.form_lifecycle import AuthWorkflowStatus

    auth = AuthenticationStrategy()
    auth.authenticated = True
    auth.status = AuthWorkflowStatus.AUTHENTICATED
    auth.note_page(_login_page())
    assert auth.status == AuthWorkflowStatus.SESSION_EXPIRED
    assert auth.authenticated is False


def test_registration_generates_unique_emails():
    a1 = AuthenticationStrategy()
    a2 = AuthenticationStrategy()
    p1 = a1.ensure_registration_profile()
    p2 = a2.ensure_registration_profile()
    assert p1.email != p2.email
    assert p1.password != p2.password


@pytest.mark.asyncio
async def test_planner_selects_safe_test_data_candidate_when_authenticated():
    """Planner._select_auth_candidate must still build the frontier once authenticated.

    Regression test: the guard used to read `if auth is None or auth.authenticated: return None`,
    which bailed out *before* FrontierBuilder.build() ever ran — making the authenticated-state
    `safe_test_data_create` candidate (Add Contact discovery) permanently unreachable even though
    FrontierBuilder itself produced it correctly (see test_frontier_repopulated_after_auth, which
    only exercises FrontierBuilder directly and therefore never caught this).
    """
    auth = AuthenticationStrategy()
    auth.authenticated = True
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = auth
    memory.remaining_action_budget = 40
    planner = Planner(MockGemmaProvider())
    page = _authed_page()
    action = await planner.next_action(
        page,
        [],
        [],
        {
            "allow_login": True,
            "allow_test_account_creation": True,
            "allow_safe_test_data_creation": True,
            "safe_mode": True,
        },
        memory=memory,
    )
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_add"
    assert (action.metadata or {}).get("decision") == "safe_test_data_creation"


def test_fallback_defers_logout_until_last_resort():
    """Logout must be deprioritized until every other safe target is exhausted.

    Regression test: fallback_safe_action's link/button scans used to return the first
    unvisited element regardless of label, so an unvisited Logout link (common on an
    authenticated landing page) would be clicked before Add Contact / other modules
    were ever discovered.
    """
    from app.gemma.parser import fallback_safe_action

    page = _authed_page()  # has both "Logout" and "Add Contact", both unvisited
    action = fallback_safe_action(page_state=page, remaining_action_budget=10)
    assert action.element_id != "el_logout"
    assert action.element_id == "el_add"

    # Once Add Contact is the only thing left (already visited), logout is chosen.
    action2 = fallback_safe_action(
        page_state=page,
        remaining_action_budget=10,
        recent_actions=[{"action": "click", "element_id": "el_add", "url": page.url}],
        visited_urls=["https://example.com/addContact"],
    )
    assert action2.element_id == "el_logout"


def test_logout_omitted_while_temporary_record_awaits_cleanup():
    """Contact List run 048c112b allowed destructive actions, then clicked
    Logout before cleanup. Delete stayed 1/0 and the test contact was left
    behind. Logout must not be offered while a run-owned record is pending."""
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True
    registry = TemporaryRecordRegistry(memory.run_id)
    entry = registry.register_created(
        record_type="email", generated_identity="gemmaqa_test_cleanup@example.com"
    )
    registry.mark_verified(entry.temporary_record_id, evidence=["list_row"])
    memory.temporary_record_registry = registry
    assert pending_cleanup_blocks_logout(memory) is True

    page = _authed_page()
    cands = FrontierBuilder(memory.auth_strategy).build(
        page,
        memory=memory,
        unexplored_urls=["https://example.com/logout"],
    )
    assert not any(
        "logout" in (c.actual_label or "").lower() or c.element_id == "el_logout"
        for c in cands
    )
    assert not any("logout" in (c.url or "").lower() for c in cands)


def test_logout_omitted_after_this_run_already_cleaned_up():
    """Run b6509a34 deleted the contact, then Logout → empty login Submit."""
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True
    registry = TemporaryRecordRegistry(memory.run_id)
    entry = registry.register_created(
        record_type="email", generated_identity="gemmaqa_test_cleanup@example.com"
    )
    registry.mark_verified(entry.temporary_record_id, evidence=["list_row"])
    registry.request_cleanup(
        entry.temporary_record_id,
        plan=CleanupPlan(delete_control_element_id="el_del"),
    )
    registry.mark_delete_action_validated(entry.temporary_record_id)
    registry.mark_deleted(entry.temporary_record_id)
    registry.mark_absence_verified(entry.temporary_record_id)
    memory.temporary_record_registry = registry
    assert pending_cleanup_blocks_logout(memory) is False
    assert this_run_owns_temporary_records(memory) is True

    page = _authed_page()
    cands = FrontierBuilder(memory.auth_strategy).build(
        page,
        memory=memory,
        unexplored_urls=["https://example.com/logout"],
    )
    assert not any(
        "logout" in (c.actual_label or "").lower() or c.element_id == "el_logout"
        for c in cands
    )


@pytest.mark.asyncio
async def test_stop_reason_not_frontier_exhausted_when_auth_open():
    auth = AuthenticationStrategy()
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = auth
    memory.remaining_action_budget = 30
    planner = Planner(MockGemmaProvider())
    page = _login_page()
    # Inspect once then ensure we don't finish with fake exhaustion
    action = await planner.next_action(
        page,
        [],
        [],
        {"allow_login": True, "allow_test_account_creation": True, "safe_mode": True},
        memory=memory,
    )
    assert action.action != ActionType.FINISH or action.reason in {
        "credentials_missing",
        "authentication_required",
        "candidate_generation_failure",
        "registration_not_permitted",
    }
    # Without credentials on login, should open signup or report credentials_missing
    if action.action == ActionType.FINISH:
        assert "frontier" not in (action.reason or "").lower()
    else:
        assert action.action in {ActionType.CLICK, ActionType.FILL, ActionType.INSPECT_FORM}


def _authenticated_page_with_control(label: str, element_id: str = "el_control") -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://example.com/items",
        title="Items",
        headings=["Items"],
        interactive_elements=[
            InteractiveElement(
                element_id=element_id,
                tag="button",
                role="button",
                category="button",
                accessible_name=label,
                text=label,
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


def _authenticated_memory() -> RunMemory:
    vault = CredentialVault()
    vault.store(CredentialProfile("primary", "user@example.com", "Secret123!"), make_active=True)
    auth = AuthenticationStrategy(vault)
    auth.authenticated = True
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = auth
    memory.remaining_action_budget = 30
    return memory


@pytest.mark.parametrize(
    "real_label",
    ["Add to cart", "Create Customer", "Add Contact", "Add to Wishlist", "New Job"],
)
def test_safe_test_data_create_label_matches_real_element_text(real_label):
    """Regression: planner.py used to hardcode action_label="Add contact" for every
    safe_test_data_create candidate regardless of what was actually clicked — SauceDemo's
    "Add to cart" and ServiceFlow's "Create Customer" were both mislabeled identically.
    The label must always reflect the real clicked element, whatever app it belongs to."""
    memory = _authenticated_memory()
    page = _authenticated_page_with_control(real_label)
    planner = Planner(MockGemmaProvider())
    action = planner._select_auth_candidate(
        page, memory, {"allow_safe_test_data_creation": True}
    )
    assert action is not None
    assert action.metadata.get("action_label") == real_label


def test_element_display_label_falls_back_gracefully_when_unlabeled():
    """The shared label-resolution helper (used by the safe_test_data_create branch)
    must fall back to a generic phrase — never the old hardcoded "Add contact" —
    when a control genuinely has no accessible name/text (e.g. an icon-only button)."""
    from app.agent.frontier import element_display_label

    page = _authenticated_page_with_control("placeholder", element_id="el_icon")
    page.interactive_elements[0].accessible_name = None
    page.interactive_elements[0].visible_text = None
    page.interactive_elements[0].text = None
    label = element_display_label(page, "el_icon", fallback="Open creation form")
    assert label == "Open creation form"
    assert label != "Add contact"


def test_action_label_does_not_contaminate_unrelated_domain_inference():
    """A safe_test_data_create action on a non-contact app must never leak a
    Contact-List-specific keyword into the navigation-edge labels that
    infer_application_purpose() reads."""
    from app.application.purpose import infer_application_purpose
    from app.application.store import ApplicationStore

    store = ApplicationStore(new_id(), "https://serviceflow.example.com/")
    memory = _authenticated_memory()
    page = _authenticated_page_with_control("Create Customer")
    planner = Planner(MockGemmaProvider())
    action = planner._select_auth_candidate(
        page, memory, {"allow_safe_test_data_creation": True}
    )
    assert action is not None
    store.record_navigation(
        from_url="https://serviceflow.example.com/customers",
        to_url="https://serviceflow.example.com/customers/new",
        action_type=action.action.value,
        action_label=action.metadata.get("action_label", ""),
        element_id=action.element_id,
    )
    purpose, confidence, evidence = infer_application_purpose(store.model)
    assert "contact" not in purpose.lower()

"""Generic authentication detection, planning, and success evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from app.agent.credentials import CredentialProfile, CredentialVault
from app.agent.form_lifecycle import AuthWorkflowStatus, FormLifecycle
from app.agent.test_data import identifiable_name, run_tag
from app.schemas import (
    ActionCategory,
    ActionType,
    BrowserAction,
    FormDescriptor,
    InteractiveElement,
    PageState,
    RiskLevel,
)
from app.utils.auth_trace import trace as auth_trace
from app.utils.logging import get_logger

logger = get_logger("agent.auth")

LOGIN_HINTS = (
    "login",
    "log in",
    "sign in",
    "signin",
    "authenticate",
)
REGISTER_HINTS = (
    "sign up",
    "signup",
    "register",
    "create account",
    "add user",
    "new account",
)
RESET_HINTS = ("reset password", "forgot password", "password reset", "recover")
# An inconclusive login result (no success/failure signal at all) is often just a slow
# async redirect rather than a real rejection — allow this many attempts before giving up.
MAX_LOGIN_ATTEMPTS = 2
# Fields a credentials form is made of. A login form contains ONLY these; a
# registration form contains only these plus a person's name. Anything else in a
# password-bearing form -- a role selector, an employee lookup, an account status
# -- means the form administers somebody else's account rather than signing this
# session in.
CREDENTIAL_FIELD_HINTS = (
    "username", "user name", "userid", "user id", "login", "email", "e-mail",
    "password", "passwd", "confirm", "otp", "one-time", "verification code",
    "remember", "captcha",
    # Registration legitimately asks for the NEW USER's own name -- spelled out,
    # never a bare "name". OrangeHRM's admin form asks for "Employee Name", which
    # a bare "name" hint accepted, so the form still read as credentials-only.
    "first name", "firstname", "last name", "lastname", "full name", "your name",
)

# Verbs a form uses when it saves a record. An authentication form says Login,
# Sign in, or Register -- never Save, and never next to a Cancel button.
DATA_ENTRY_SUBMIT_HINTS = ("save", "update", "apply", "create", "add", "submit changes")
DISCARD_CONTROL_HINTS = ("cancel", "discard", "reset")
AUTH_SUBMIT_HINTS = (
    "login", "log in", "sign in", "signin", "sign up", "signup", "register",
    "create account", "continue", "next", "submit", "send", "verify",
)

# Sign-in asks for two or three things; sign-up for a few more. A password-
# bearing form longer than this is administering an account, not entering one.
MAX_CREDENTIALS_FORM_FIELDS = 6

AUTHENTICATED_HINTS = (
    "logout",
    "log out",
    "sign out",
    "dashboard",
    "my account",
    "profile",
    "welcome",
)
SESSION_EXPIRED_HINTS = (
    "session expired",
    "please log in",
    "please sign in",
    "unauthorized",
    "authentication required",
    "not authenticated",
)

# Some demo/staging apps publish working test credentials directly on the login page
# (classic pattern: "Accepted usernames are: ... / Password for all users: ..."). These
# match against ordinary page text, never against secret storage — nothing is invented,
# only what the page itself already displays publicly is extracted.
_DISPLAYED_USERNAME_RE = re.compile(
    r"user\s*name(?:s)?(?:\s+are)?\s*:?\s*\n?\s*([a-zA-Z][\w.\-]{2,30})",
    re.IGNORECASE,
)
_DISPLAYED_PASSWORD_RE = re.compile(
    r"password(?:s)?(?:\s+for\s+all\s+users)?\s*:?\s*\n?\s*(\S{4,40})",
    re.IGNORECASE,
)
# Bare field labels ("Username", "Password") with no actual value shown must never be
# mistaken for a real displayed credential.
_DISPLAYED_CRED_STOPWORDS = {
    "username", "usernames", "password", "passwords", "login", "log", "email",
    "submit", "sign", "signin", "signup", "required", "account", "user", "pass",
    "here", "field", "input", "enter", "required.",
}


@dataclass
class AuthFormInfo:
    form_id: str
    kind: str  # login | registration | password_reset | unknown
    page_url: str
    field_element_ids: list[str] = field(default_factory=list)
    submit_element_id: str | None = None
    confidence: float = 0.5


@dataclass
class AuthEvaluation:
    authenticated: bool
    method: str
    confidence: float
    signals: list[str] = field(default_factory=list)
    rejected: bool = False
    session_expired: bool = False


@dataclass
class AuthWorkflow:
    """Multi-step authentication plan executed one BrowserAction at a time."""

    workflow_id: str
    method: str  # login | registration
    form_id: str
    status: AuthWorkflowStatus = AuthWorkflowStatus.READY
    steps: list[BrowserAction] = field(default_factory=list)
    step_index: int = 0
    credential_profile_id: str | None = None
    attempts: int = 0
    last_error: str | None = None

    def next_action(self) -> BrowserAction | None:
        if self.step_index >= len(self.steps):
            return None
        action = self.steps[self.step_index]
        self.status = AuthWorkflowStatus.FILLING
        return action

    def advance(self) -> None:
        self.step_index += 1
        if self.step_index >= len(self.steps):
            self.status = AuthWorkflowStatus.SUBMITTED


class AuthenticationStrategy:
    """Detect auth barriers and produce structured fill/submit plans."""

    def __init__(self, vault: CredentialVault | None = None) -> None:
        self.vault = vault or CredentialVault()
        self.status = AuthWorkflowStatus.DETECTED
        self.authenticated = False
        self.method: str | None = None
        self.checkpoint_url: str | None = None
        self.signals: list[str] = []
        self.active_workflow: AuthWorkflow | None = None
        self.blocker: str | None = None
        self.form_lifecycle: dict[str, FormLifecycle] = {}
        self.login_attempts: dict[str, int] = {}
        self.anonymous_pages: set[str] = set()
        self.authenticated_pages: set[str] = set()
        self.controlled_writes: list[str] = []

    def mark_authenticated(self, *, method: str, checkpoint_url: str | None = None) -> None:
        """Records a login that succeeded OUTSIDE this class's own detect ->
        fill -> submit -> `evaluate_after_submit()` workflow — the direct-
        credentials path (`BrowserManager.login()`, driven by
        `CreateRunRequest.username`/`password` at run start) authenticates
        the real browser session perfectly well but never runs an
        `AuthWorkflow` through this strategy, so `self.authenticated` stayed
        `False` forever without this call. Live-verification finding: every
        precondition check gated on "actor requires an authenticated
        session" (`app.intelligence.autonomous_investigation.
        precondition_validator`) silently deferred FOREVER for a
        direct-credentials run, because THIS flag, not the real browser
        session, is what that check reads. Mirrors the same fields
        `evaluate_after_submit()` sets on its own success path (status/
        checkpoint_url/blocker) so both paths converge on one consistent
        "authenticated" representation."""
        self.authenticated = True
        self.method = method
        self.status = AuthWorkflowStatus.AUTHENTICATED
        if checkpoint_url:
            self.checkpoint_url = checkpoint_url
        self.blocker = None

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def page_blob(self, page: PageState) -> str:
        bits = [
            page.url or "",
            page.title or "",
            " ".join(page.headings or []),
            page.visible_text_summary or "",
        ]
        for el in page.interactive_elements or []:
            bits.append(el.accessible_name or "")
            bits.append(el.visible_text or "")
            bits.append(el.label or "")
            bits.append(el.text or "")
        for form in page.forms or []:
            for f in form.fields:
                bits.append(f.label or "")
                bits.append(f.name or "")
                bits.append(f.field_type or "")
        return " ".join(bits).lower()

    def extract_displayed_credentials(self, page: PageState) -> CredentialProfile | None:
        """Detect test credentials the target app itself displays on the login page
        (e.g. SauceDemo's "Accepted usernames are: ... / Password for all users: ...").

        Reads only the page's own visible text — never invents a value. Deliberately
        conservative: both a username-shaped and a password-shaped match must be found,
        and they must differ, or nothing is returned.
        """
        text = page.visible_text_summary or ""
        if not text:
            return None

        def first_real_value(pattern: re.Pattern[str]) -> str | None:
            # A page can mention "Username"/"Password" more than once (a bare field
            # label, then the actual displayed value) — skip matches that only
            # recaptured another label word and keep looking.
            for m in pattern.finditer(text):
                candidate = m.group(1).strip()
                if candidate and candidate.lower() not in _DISPLAYED_CRED_STOPWORDS:
                    return candidate
            return None

        username = first_real_value(_DISPLAYED_USERNAME_RE)
        password = first_real_value(_DISPLAYED_PASSWORD_RE)
        if not username or not password or username.lower() == password.lower():
            return None
        return CredentialProfile(
            profile_id="page_displayed",
            username=username,
            password=password,
            source="page_displayed",
            email=username if "@" in username else None,
        )

    def classify_page(self, page: PageState) -> str:
        blob = self.page_blob(page)
        path = urlparse(page.url or "").path.lower()
        # Prefer path / heading signals over incidental "Sign up" links on login pages.
        # Registration paths are computed before the admin data-entry early-return:
        # Contact List /addUser is titled "Add User" and has Cancel — the same
        # shape as OrangeHRM Admin -> Add User. Only an explicit registration
        # path may override that heuristic.
        path_is_login = any(k in path for k in ("/login", "/signin", "/sign-in", "/auth/login"))
        path_is_reg = any(
            k in path for k in ("/signup", "/sign-up", "/register", "/adduser", "/add-user")
        )
        # Match reset hints against title/headings/path only — the full blob includes
        # every interactive element's text, so an incidental "Forgot Password?" link on
        # an ordinary login page would otherwise misclassify the whole page as a reset flow.
        page_level_blob = " ".join([page.title or "", *(page.headings or [])]).lower()
        if any(h in page_level_blob or h in path for h in RESET_HINTS):
            return "password_reset"
        # A page whose password-bearing forms ALL carry non-credential business
        # fields administers accounts; it is not a door to walk through. Checked
        # before the heading hints because "Add User" is both the label of a
        # public sign-up page and the label of an admin screen, and only the form
        # itself says which one this is. Skipped on a recognized registration path.
        password_forms = [f for f in (page.forms or []) if self._form_has_password(f)]
        if (
            password_forms
            and not path_is_reg
            and all(
                not self._is_credentials_only_form(f) or self._looks_like_data_entry(f, page)
                for f in password_forms
            )
        ):
            return "authenticated" if self.authenticated else "unknown"

        if self._looks_authenticated(page, blob):
            return "authenticated"

        heading = " ".join(page.headings or []).lower()
        title = (page.title or "").lower()
        heading_login = any(h in heading or h in title for h in LOGIN_HINTS)
        heading_reg = any(h in heading or h in title for h in REGISTER_HINTS)

        if path_is_reg or (heading_reg and not path_is_login):
            return "registration"
        if path_is_login or heading_login:
            return "login"
        if self._registration_field_score(page) >= 3 and self._has_password_form(page):
            return "registration"
        if self._has_password_form(page):
            return "login"
        if any(h in blob for h in SESSION_EXPIRED_HINTS):
            return "auth_required"
        return "unknown"

    def _looks_like_data_entry(self, form: FormDescriptor, page: PageState) -> bool:
        """Does this form SAVE a record rather than sign a session in?

        Read off the controls, because they survive everything else. OrangeHRM's
        Admin -> Add User screen defeated every field-based rule: its User Role
        and Status pickers are custom components rather than <select> elements,
        so the extractor never saw them, and what reached the classifier was
        Employee Name + Username + Password + Confirm Password — the exact shape
        of a sign-up form.

        What a human sees immediately, and what remains in the model, is the pair
        of buttons: **Save**, next to **Cancel**. No login or sign-up form offers
        to cancel, and none of them says Save.
        """
        submit_text = ""
        if form.submit_element_id:
            for el in page.interactive_elements or []:
                if el.element_id == form.submit_element_id:
                    submit_text = (
                        el.accessible_name or el.visible_text or el.text or el.label or ""
                    ).strip().lower()
                    break

        if submit_text:
            if any(h in submit_text for h in AUTH_SUBMIT_HINTS):
                return False
            if any(h in submit_text for h in DATA_ENTRY_SUBMIT_HINTS):
                return True

        # No usable submit label. A discard control alongside the form still
        # settles it: authentication is not something you cancel out of.
        field_ids = {f.element_id for f in (form.fields or []) if f.element_id}
        for el in page.interactive_elements or []:
            if el.element_id in field_ids or el.element_id == form.submit_element_id:
                continue
            text = (el.accessible_name or el.visible_text or el.text or el.label or "").strip().lower()
            if text and any(h == text or text.startswith(h) for h in DISCARD_CONTROL_HINTS):
                return True
        return False

    def _is_credentials_only_form(self, form: FormDescriptor) -> bool:
        """Does this password-bearing form ask ONLY for credentials (and, for
        registration, the new user's own name)?

        This is what separates "sign this session in" from "administer an
        account". OrangeHRM's Admin -> Add User form carries Username, Password
        and Confirm Password — and also User Role, Employee Name and Status. A
        login form never asks who the employee is.

        Without this distinction the agent classified that page as a login
        barrier, decided its live session must have expired, and looped on the
        form for 170 actions.
        """
        fields = [
            f for f in (form.fields or [])
            if (f.field_type or "").lower() not in {"hidden", "submit", "button", "image"}
        ]
        if not fields:
            return False
        if not any(
            (f.field_type or "").lower() == "password" or "password" in (f.label or "").lower()
            for f in fields
        ):
            return False

        # Shape first, because it survives an application whose labels the
        # extractor cannot read. OrangeHRM renders field labels in sibling
        # elements rather than <label for>, so every field arrives unlabelled —
        # and a label-only rule then judged its Add User form "credentials only"
        # and kept the whole loop alive.
        #
        # No sign-in or sign-up form asks you to pick from a list. A dropdown or
        # radio group in a password-bearing form means it is describing an
        # account rather than entering one.
        if any(
            (f.field_type or "").lower() in {"select", "select-one", "select-multiple", "radio"}
            for f in fields
        ):
            return False
        if len(fields) > MAX_CREDENTIALS_FORM_FIELDS:
            return False

        for field in fields:
            blob = f"{field.label or ''} {field.name or ''}".strip().lower()
            if not blob:
                # Unlabelled and unnamed: shape has already had its say.
                continue
            if not any(hint in blob for hint in CREDENTIAL_FIELD_HINTS):
                return False
        return True

    def _has_credentials_only_form(self, page: PageState) -> bool:
        return any(self._is_credentials_only_form(f) for f in page.forms or [])

    def _has_password_form(self, page: PageState) -> bool:
        for form in page.forms or []:
            for f in form.fields:
                ft = (f.field_type or "").lower()
                label = f"{f.label or ''} {f.name or ''}".lower()
                if ft == "password" or "password" in label:
                    return True
        for el in page.interactive_elements or []:
            if (el.input_type or el.type or "").lower() == "password":
                return True
        return False

    def _registration_field_score(self, page: PageState) -> int:
        score = 0
        for form in page.forms or []:
            for f in form.fields:
                blob = f"{f.label or ''} {f.name or ''} {f.field_type or ''}".lower()
                if any(k in blob for k in ("first", "last", "name", "email", "password", "confirm")):
                    score += 1
        return score

    def _looks_authenticated(self, page: PageState, blob: str | None = None) -> bool:
        blob = blob if blob is not None else self.page_blob(page)
        has_logout = any(
            any(h in ((el.accessible_name or el.visible_text or el.text or "")).lower() for h in ("logout", "log out", "sign out"))
            for el in page.interactive_elements or []
        )
        has_login_form = self._has_password_form(page) and any(
            h in blob for h in LOGIN_HINTS + REGISTER_HINTS
        )
        # A logout control is DIRECT evidence that the session is live, and it
        # outranks the mere presence of a password form. Every admin panel has a
        # create-user form carrying username/password fields; on OrangeHRM that
        # page is headed "Add User", which matches REGISTER_HINTS, so
        # `has_login_form` cancelled the logout evidence and the agent concluded
        # it had been logged out — 55 times in a row, inside a live session,
        # while the sidebar and user menu were right there on the page.
        #
        # An application that has actually taken the session away does not leave
        # you a logout button.
        if has_logout:
            return True
        if self.authenticated and not has_login_form:
            # Stay authenticated until login form reappears
            if any(h in blob for h in AUTHENTICATED_HINTS) or page.tables or (
                page.classification and page.classification.page_type in {"dashboard", "list", "detail"}
            ):
                return True
        return False

    def detect_forms(self, page: PageState) -> list[AuthFormInfo]:
        out: list[AuthFormInfo] = []
        page_kind = self.classify_page(page)
        for form in page.forms or []:
            kind = self._classify_form(form, page, page_kind)
            if kind == "unknown" and not self._form_has_password(form):
                continue
            fields = [f.element_id for f in form.fields if f.element_id]
            submit = form.submit_element_id or self._find_submit(page, kind)
            info = AuthFormInfo(
                form_id=form.form_id,
                kind=kind if kind != "unknown" else ("registration" if page_kind == "registration" else "login"),
                page_url=page.url,
                field_element_ids=fields,
                submit_element_id=submit,
                confidence=0.85 if kind != "unknown" else 0.55,
            )
            out.append(info)
            self.form_lifecycle.setdefault(form.form_id, FormLifecycle.DISCOVERED)
        auth_trace(
            "detect_forms",
            page_kind=page_kind,
            url=page.url,
            forms=[(f.form_id, f.kind, len(f.field_element_ids), bool(f.submit_element_id)) for f in out],
        )
        return out

    def _form_has_password(self, form: FormDescriptor) -> bool:
        for f in form.fields:
            if (f.field_type or "").lower() == "password" or "password" in (f.label or "").lower():
                return True
        return False

    def _classify_form(self, form: FormDescriptor, page: PageState, page_kind: str) -> str:
        blob = " ".join(
            [
                form.form_id or "",
                form.action or "",
                " ".join((f.label or "") + " " + (f.name or "") for f in form.fields),
                page.title or "",
                " ".join(page.headings or []),
            ]
        ).lower()
        # The form's own shape decides before any text does. `blob` includes the
        # page title and headings, so on OrangeHRM's Admin -> Add User screen the
        # heading alone made this return "registration" — independently of
        # `classify_page`, which is why fixing the page classification was not
        # enough on its own and the loop survived it.
        # A recognized registration path (e.g. Contact List /addUser) is a public
        # door, not an admin record form — skip the Cancel/data-entry override.
        path = urlparse(page.url or "").path.lower()
        path_is_reg = any(
            k in path for k in ("/signup", "/sign-up", "/register", "/adduser", "/add-user")
        )
        if (
            not path_is_reg
            and self._form_has_password(form)
            and (
                not self._is_credentials_only_form(form)
                or self._looks_like_data_entry(form, page)
            )
        ):
            return "unknown"
        if any(h in blob for h in RESET_HINTS):
            return "password_reset"
        if page_kind == "registration" or any(h in blob for h in REGISTER_HINTS):
            return "registration"
        if page_kind == "login" or any(h in blob for h in LOGIN_HINTS):
            return "login"
        if self._form_has_password(form):
            if self._registration_field_score(page) >= 3:
                return "registration"
            return "login"
        return "unknown"

    def _find_submit(self, page: PageState, kind: str) -> str | None:
        prefer = (
            ("submit", "sign up", "signup", "register", "create")
            if kind == "registration"
            else ("submit", "login", "log in", "sign in")
        )
        for el in page.interactive_elements or []:
            text = " ".join(
                filter(
                    None,
                    [el.accessible_name, el.visible_text, el.text, el.label, el.name],
                )
            ).lower()
            itype = (el.input_type or el.type or "").lower()
            tag = (el.tag or "").lower()
            role = (el.role or "").lower()
            if itype == "submit" or (
                (tag == "button" or role == "button" or itype == "button")
                and any(p in text for p in prefer)
            ):
                return el.element_id
        return None

    # ------------------------------------------------------------------
    # Lifecycle / observation updates
    # ------------------------------------------------------------------

    def note_form_inspected(self, form_id: str) -> None:
        current = self.form_lifecycle.get(form_id, FormLifecycle.DISCOVERED)
        if current in {FormLifecycle.DISCOVERED, FormLifecycle.INSPECTED}:
            self.form_lifecycle[form_id] = FormLifecycle.INSPECTED
            # Inspection is not completion — positive path remains available
            self.form_lifecycle[form_id] = FormLifecycle.POSITIVE_SUBMISSION_AVAILABLE

    def note_page(self, page: PageState) -> None:
        kind = self.classify_page(page)
        url = page.url or ""
        # Session expiry: previously authenticated, login/auth form returned.
        #
        # `_has_password_form` alone is NOT evidence of expiry. An authenticated
        # operator opening Admin -> Add User sees username/password fields inside
        # a live session, and treating that as expiry made the agent throw away a
        # working session, re-arm the login workflow against an admin form, and
        # loop on it until the operator cancelled the run. Expiry means the
        # application took the session AWAY, so it must also have stopped looking
        # like a page you are signed in to.
        if self.authenticated and kind in {
            "login",
            "registration",
            "auth_required",
        } and self._has_password_form(page) and not self._looks_authenticated(page):
            self.authenticated = False
            self.status = AuthWorkflowStatus.SESSION_EXPIRED
            self.checkpoint_url = None
            # A prior SUCCEEDED/EXHAUSTED lifecycle described the OLD session, not this
            # one — the session just ended, so any login form here must be retriable
            # again rather than treated as already resolved forever.
            for f in self.detect_forms(page):
                if f.kind == "login":
                    self.form_lifecycle[f.form_id] = FormLifecycle.POSITIVE_SUBMISSION_AVAILABLE
                    self.login_attempts.pop(f.form_id, None)
            return

        if kind in {"login", "registration", "password_reset", "auth_required"}:
            self.anonymous_pages.add(url)
            if not self.authenticated:
                if kind == "password_reset":
                    self.status = AuthWorkflowStatus.DETECTED
                elif self.vault.get():
                    self.status = AuthWorkflowStatus.READY
                else:
                    discovered = self.extract_displayed_credentials(page)
                    if discovered and not self.vault.get():
                        self.vault.store(discovered, make_active=True)
                        self.status = AuthWorkflowStatus.READY
                        auth_trace(
                            "note_page.discovered_displayed_credentials",
                            url=url,
                        )
                    else:
                        self.status = AuthWorkflowStatus.CREDENTIALS_REQUIRED
        elif kind == "authenticated" or (self.authenticated and not self._has_password_form(page)):
            self.authenticated_pages.add(url)
            self.authenticated = True
            self.status = AuthWorkflowStatus.AUTHENTICATED
            self.checkpoint_url = self.checkpoint_url or url

    def form_still_actionable(self, form_id: str) -> bool:
        state = self.form_lifecycle.get(form_id, FormLifecycle.DISCOVERED)
        return state not in {
            FormLifecycle.SUCCEEDED,
            FormLifecycle.BLOCKED,
            FormLifecycle.EXHAUSTED,
            FormLifecycle.POSITIVE_SUBMISSION_ATTEMPTED,
            FormLifecycle.SUBMITTED,
        }

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def ensure_registration_profile(self) -> CredentialProfile:
        existing = self.vault.get("generated")
        if existing:
            return existing
        tag = run_tag().lower()
        profile = CredentialProfile(
            profile_id="generated",
            username=f"{tag}@example.com",
            password=f"GqA!{uuid4().hex[:10]}Aa1",
            source="generated_registration",
            email=f"{tag}@example.com",
            first_name="GemmaQA",
            last_name="Test",
        )
        self.vault.store(profile, make_active=True)
        return profile

    def build_login_workflow(self, page: PageState, form: AuthFormInfo) -> AuthWorkflow | None:
        profile = self.vault.get()
        if not profile:
            self.blocker = "credentials_missing"
            auth_trace("build_login_workflow.none", reason="credentials_missing", form_id=form.form_id)
            return None
        steps = self._build_fill_steps(page, form, profile, method="login")
        auth_trace(
            "build_login_workflow",
            form_id=form.form_id,
            field_count=len(form.field_element_ids),
            fill_steps=len(steps),
        )
        if not steps:
            auth_trace("build_login_workflow.none", reason="zero_fill_steps", form_id=form.form_id)
            return None
        self.form_lifecycle[form.form_id] = FormLifecycle.DATA_PLAN_CREATED
        wf = AuthWorkflow(
            workflow_id=f"auth_login_{form.form_id}",
            method="login",
            form_id=form.form_id,
            status=AuthWorkflowStatus.READY,
            steps=steps,
            credential_profile_id=profile.profile_id,
        )
        self.active_workflow = wf
        return wf

    def build_registration_workflow(self, page: PageState, form: AuthFormInfo) -> AuthWorkflow | None:
        profile = self.ensure_registration_profile()
        steps = self._build_fill_steps(page, form, profile, method="registration")
        auth_trace(
            "build_registration_workflow",
            form_id=form.form_id,
            field_count=len(form.field_element_ids),
            fill_steps=len(steps),
            has_submit=any(s.metadata and s.metadata.get("auth_submit") for s in steps),
        )
        if not steps:
            auth_trace("build_registration_workflow.none", reason="zero_fill_steps", form_id=form.form_id)
            return None
        self.form_lifecycle[form.form_id] = FormLifecycle.DATA_PLAN_CREATED
        wf = AuthWorkflow(
            workflow_id=f"auth_register_{form.form_id}",
            method="registration",
            form_id=form.form_id,
            status=AuthWorkflowStatus.READY,
            steps=steps,
            credential_profile_id=profile.profile_id,
        )
        self.active_workflow = wf
        return wf

    def _build_fill_steps(
        self,
        page: PageState,
        form: AuthFormInfo,
        profile: CredentialProfile,
        *,
        method: str,
    ) -> list[BrowserAction]:
        steps: list[BrowserAction] = []
        desc = next((f for f in (page.forms or []) if f.form_id == form.form_id), None)
        fields = list(desc.fields) if desc else []
        auth_trace(
            "build_fill_steps.form_lookup",
            form_id=form.form_id,
            method=method,
            form_descriptor_found=bool(desc),
            field_count=len(fields),
        )
        # Map interactive password/email inputs if form fields incomplete
        if not fields:
            for el in page.interactive_elements or []:
                if (el.input_type or el.type or "").lower() in {
                    "email",
                    "text",
                    "password",
                } or (el.category or "") == "input":
                    from app.schemas import FormField

                    fields.append(
                        FormField(
                            element_id=el.element_id,
                            label=el.label or el.accessible_name or el.name,
                            field_type=el.input_type or el.type or "text",
                            name=el.name,
                        )
                    )

        used_refs: set[str] = set()
        for field in fields:
            if not field.element_id or field.disabled:
                auth_trace(
                    "build_fill_steps.field_skipped",
                    reason="no_element_id" if not field.element_id else "disabled",
                    field_type=field.field_type,
                    field_name=field.name,
                )
                continue
            ref, value = self._map_field_value(field, profile, method=method, used=used_refs)
            auth_trace(
                "build_fill_steps.field_mapped",
                element_id=field.element_id,
                field_type=field.field_type,
                semantic_ref=ref,
                value_resolved=value is not None,
            )
            if value is None:
                continue
            meta: dict[str, Any] = {
                "auth_write": True,
                "risk_class": "authentication_write",
                "form_id": form.form_id,
                "auth_method": method,
                "value_category": "auth_credential" if "password" in (ref or "") else "auth_identity",
                "action_label": field.label or field.name or field.element_id,
            }
            if ref:
                meta["credential_ref"] = f"{profile.profile_id}.{ref}"
                # Do not put secret in action.value for password — executor resolves
                action_value = None if ref == "password" else value
                if ref != "password":
                    action_value = value
                else:
                    action_value = ""  # placeholder; executor fills from vault
            else:
                action_value = value
            steps.append(
                BrowserAction(
                    action=ActionType.FILL,
                    element_id=field.element_id,
                    value=action_value,
                    reason=f"Authentication write: fill {field.label or field.name or 'field'}",
                    expected_result="Field accepts authentication test data.",
                    risk=RiskLevel.LOW,
                    category=ActionCategory.FORM_INTERACTION,
                    metadata=meta,
                )
            )

        submit_id = form.submit_element_id
        if submit_id:
            steps.append(
                BrowserAction(
                    action=ActionType.CLICK,
                    element_id=submit_id,
                    reason=f"Submit {method} form to resolve authentication barrier.",
                    expected_result="Authentication succeeds or validation feedback appears.",
                    risk=RiskLevel.LOW,
                    category=ActionCategory.FORM_INTERACTION,
                    metadata={
                        "auth_write": True,
                        "risk_class": "authentication_write",
                        "form_id": form.form_id,
                        "auth_method": method,
                        "auth_submit": True,
                        "action_label": "Submit authentication form",
                    },
                )
            )
        return steps

    def _map_field_value(
        self,
        field: Any,
        profile: CredentialProfile,
        *,
        method: str,
        used: set[str],
    ) -> tuple[str | None, str | None]:
        blob = f"{getattr(field, 'label', '')} {getattr(field, 'name', '')} {getattr(field, 'field_type', '')} {getattr(field, 'placeholder', '')}".lower()
        ftype = (getattr(field, "field_type", None) or "").lower()

        def take(ref: str, value: str | None) -> tuple[str | None, str | None]:
            if ref in used or value is None:
                return None, None
            used.add(ref)
            return ref, value

        if ftype in {"submit", "button", "reset"}:
            # Never treat the submit control itself as a fillable field — it is only
            # ever driven via the dedicated submit step appended in _build_fill_steps.
            return None, None
        if ftype == "password" or "password" in blob:
            if "confirm" in blob and "confirm_password" not in used:
                return take("password", profile.password)
            return take("password", profile.password)
        if ftype == "email" or "email" in blob:
            return take("email", profile.email or profile.username)
        if "first" in blob and "name" in blob:
            return take("first_name", profile.first_name or "GemmaQA")
        if "last" in blob and "name" in blob:
            return take("last_name", profile.last_name or "Test")
        # "name" alone is too naive an exclusion here — real username attributes like
        # "user-name" contain the substring "name" and would otherwise be rejected while
        # a submit button named "login-button" would wrongly pass through. First/last
        # name fields are already handled above, so no further "name" guard is needed.
        if any(k in blob for k in ("user", "login", "email", "account")):
            return take("username", profile.username)
        if "name" in blob and method == "registration" and "first_name" not in used:
            return take("first_name", profile.first_name or identifiable_name("User"))
        # Skip unknown non-required fields rather than inventing
        if getattr(field, "required", False):
            if "phone" in blob or ftype == "tel":
                return None, "+1-555-0100"
            return None, identifiable_name("Field")
        return None, None

    def next_workflow_action(self) -> BrowserAction | None:
        if not self.active_workflow:
            return None
        action = self.active_workflow.next_action()
        return action

    def on_action_result(self, action: BrowserAction, success: bool) -> None:
        if not self.active_workflow:
            return
        meta = action.metadata or {}
        if meta.get("form_id") == self.active_workflow.form_id or meta.get("auth_write"):
            self.active_workflow.advance()
            if meta.get("auth_submit"):
                self.form_lifecycle[self.active_workflow.form_id] = (
                    FormLifecycle.POSITIVE_SUBMISSION_ATTEMPTED
                )
                self.controlled_writes.append(f"{self.active_workflow.method}_submit")
            elif action.action == ActionType.FILL:
                self.controlled_writes.append(f"fill:{action.element_id}")
            if not success:
                self.active_workflow.last_error = "action_failed"
                self.active_workflow.attempts += 1

    def evaluate_after_submit(
        self,
        before: PageState,
        after: PageState,
        *,
        method: str,
    ) -> AuthEvaluation:
        signals: list[str] = []
        before_blob = self.page_blob(before)
        after_blob = self.page_blob(after)
        before_had_password = self._has_password_form(before)
        after_has_password = self._has_password_form(after)
        after_logout = any(
            any(
                h in ((el.accessible_name or el.visible_text or el.text or "")).lower()
                for h in ("logout", "log out", "sign out")
            )
            for el in after.interactive_elements or []
        )
        url_changed = (before.url or "") != (after.url or "")
        errorish = any(
            k in after_blob
            for k in ("incorrect", "invalid", "failed", "error", "wrong password", "already exists")
        )
        session_expired = any(h in after_blob for h in SESSION_EXPIRED_HINTS)

        if before_had_password and not after_has_password:
            signals.append("Login form disappeared")
        if after_logout:
            signals.append("Logout control appeared")
        if url_changed and not after_has_password:
            signals.append("URL changed appropriately")
        if after.tables and not after_has_password:
            signals.append("Authenticated data surface appeared")
        if after.headings and not after_has_password:
            heading = (after.headings[0] or "").lower()
            if any(k in heading for k in ("contact", "dashboard", "home", "welcome", "list")):
                signals.append(f"Heading appeared: {after.headings[0]}")
        if self.classify_page(after) == "authenticated":
            signals.append("Page classified as authenticated")

        authenticated = (
            (before_had_password and not after_has_password and (after_logout or url_changed or after.tables))
            or after_logout
            or (len(signals) >= 2 and not errorish)
        )
        # Registration may land on login — not yet authenticated
        if method == "registration" and after_has_password and any(
            h in after_blob for h in LOGIN_HINTS
        ):
            authenticated = False
            signals.append("Registration returned to login form")

        rejected = bool(errorish and after_has_password and not authenticated)
        if rejected:
            signals.append("Authentication error message present")

        confidence = min(0.99, 0.4 + 0.15 * len(signals))
        if authenticated:
            self.authenticated = True
            self.method = method
            self.status = AuthWorkflowStatus.AUTHENTICATED
            self.checkpoint_url = after.url
            self.signals = signals
            if self.active_workflow:
                self.form_lifecycle[self.active_workflow.form_id] = FormLifecycle.SUCCEEDED
                self.active_workflow.status = AuthWorkflowStatus.AUTHENTICATED
                self.active_workflow = None
            self.blocker = None
        elif rejected:
            self.status = AuthWorkflowStatus.REJECTED
            self.blocker = "authentication_failed"
            if self.active_workflow:
                self.form_lifecycle[self.active_workflow.form_id] = FormLifecycle.FAILED
                self.active_workflow.status = AuthWorkflowStatus.REJECTED
                self.active_workflow = None
        elif method == "registration" and after_has_password and "login" in after_blob:
            # Ready to login with generated account
            self.status = AuthWorkflowStatus.READY
            if self.active_workflow:
                self.form_lifecycle[self.active_workflow.form_id] = FormLifecycle.SUBMITTED
                self.active_workflow = None
            signals.append("Registration submitted; login required next")
        elif method == "login" and self.active_workflow:
            # No signal either way (no success cue, no error text) — this is commonly a
            # slow async sign-in redirect rather than a genuine rejection. Reopen the form
            # for a bounded number of retries instead of permanently marking it attempted.
            fid = self.active_workflow.form_id
            attempts = self.login_attempts.get(fid, 0) + 1
            self.login_attempts[fid] = attempts
            if attempts < MAX_LOGIN_ATTEMPTS:
                self.form_lifecycle[fid] = FormLifecycle.POSITIVE_SUBMISSION_AVAILABLE
                self.status = AuthWorkflowStatus.READY
                signals.append(f"Inconclusive result (attempt {attempts}); retrying login")
            else:
                self.form_lifecycle[fid] = FormLifecycle.EXHAUSTED
                self.status = AuthWorkflowStatus.REJECTED
                self.blocker = "authentication_failed"
                signals.append("Login inconclusive after retries; treating as failed")
            self.active_workflow = None

        return AuthEvaluation(
            authenticated=authenticated,
            method=method,
            confidence=confidence,
            signals=signals,
            rejected=rejected,
            session_expired=session_expired,
        )

    def public_status(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "auth_status": self.status.value,
            "auth_method": self.method,
            "checkpoint_url": self.checkpoint_url,
            "auth_signals": list(self.signals)[:12],
            "auth_blocker": self.blocker,
            "anonymous_pages": len(self.anonymous_pages),
            "authenticated_pages": len(self.authenticated_pages),
            "controlled_writes": len(self.controlled_writes),
            **self.vault.public_flags(),
        }

    def unresolved_auth_blocker(
        self,
        page: PageState,
        *,
        allow_login: bool,
        allow_registration: bool,
    ) -> str | None:
        """Truthful stop reason when auth is still blocking discovery."""
        if self.authenticated:
            return None
        kind = self.classify_page(page)
        forms = self.detect_forms(page)
        if kind not in {"login", "registration", "auth_required"} and not forms:
            return None
        actionable = [f for f in forms if self.form_still_actionable(f.form_id)]
        if not actionable and forms:
            # Forms existed but were marked exhausted without success
            reason = self.blocker or "authentication_failed"
            auth_trace("unresolved_auth_blocker", reason=reason, path="forms_not_actionable")
            return reason
        login_forms = [f for f in actionable if f.kind == "login"]
        reg_forms = [f for f in actionable if f.kind == "registration"]
        if login_forms and not self.vault.get() and not (allow_registration and reg_forms):
            auth_trace("unresolved_auth_blocker", reason="credentials_missing", path="login_no_creds_no_reg")
            return "credentials_missing"
        if login_forms and self.vault.get() and allow_login:
            auth_trace(
                "unresolved_auth_blocker",
                reason="candidate_generation_failure",
                path="login_creds_available_but_no_candidate",
            )
            return "candidate_generation_failure"
        if reg_forms and not allow_registration and not self.vault.get():
            auth_trace("unresolved_auth_blocker", reason="registration_not_permitted", path="reg_disallowed")
            return "registration_not_permitted"
        if reg_forms and allow_registration:
            auth_trace(
                "unresolved_auth_blocker",
                reason="candidate_generation_failure",
                path="reg_allowed_but_no_candidate",
            )
            return "candidate_generation_failure"
        if forms or kind in {"login", "registration", "auth_required"}:
            auth_trace("unresolved_auth_blocker", reason="authentication_required", path="fallback")
            return "authentication_required"
        return None

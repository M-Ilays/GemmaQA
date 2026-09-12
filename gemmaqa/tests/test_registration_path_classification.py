"""Registration-path classification must beat the OrangeHRM Cancel heuristic.

Contact List /addUser is titled "Add User" and has Cancel. That shape used to
return unknown/login before the /adduser path check ran, so create_test_account
never reached the frontier.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.frontier import FrontierBuilder  # noqa: E402
from app.schemas import FormDescriptor, FormField, InteractiveElement, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402

HEADING = "Sign up to begin adding your contacts!"


def _el(element_id: str, tag: str, text: str = "", **kw) -> InteractiveElement:
    return InteractiveElement(
        element_id=element_id,
        tag=tag,
        type=kw.pop("type", tag),
        visible_text=text,
        accessible_name=kw.pop("accessible_name", text),
        selector=f"#{element_id}",
        is_visible=True,
        is_enabled=True,
        **kw,
    )


def _contact_list_signup(url: str) -> PageState:
    """Observed Contact List signup: heading leaked as every field label + Cancel."""
    return PageState(
        page_id=new_id(),
        url=url,
        title="Add User",
        headings=[HEADING],
        interactive_elements=[
            _el("el_001", "input", type="text", label=HEADING, accessible_name=HEADING),
            _el("el_002", "input", type="text", label=HEADING, accessible_name=HEADING),
            _el("el_003", "input", type="text", label=HEADING, accessible_name=HEADING),
            _el("el_004", "input", type="password", label=HEADING, accessible_name=HEADING),
            _el("el_005", "button", "Submit", accessible_name="Submit"),
            _el("el_006", "button", "Cancel", accessible_name="Cancel"),
        ],
        forms=[
            FormDescriptor(
                form_id="form_signup",
                submit_element_id="el_005",
                fields=[
                    FormField(
                        name=HEADING,
                        field_type="input",
                        label=HEADING,
                        element_id="el_001",
                    ),
                    FormField(
                        name=HEADING,
                        field_type="input",
                        label=HEADING,
                        element_id="el_002",
                    ),
                    FormField(
                        name=HEADING,
                        field_type="input",
                        label=HEADING,
                        element_id="el_003",
                    ),
                    FormField(
                        name=HEADING,
                        field_type="password",
                        label=HEADING,
                        element_id="el_004",
                    ),
                ],
            )
        ],
    )


def _orangehrm_admin_add_user() -> PageState:
    return PageState(
        page_id="page_add_user",
        url="https://opensource-demo.orangehrmlive.com/web/index.php/admin/saveSystemUser",
        title="OrangeHRM",
        headings=["Admin", "Add User"],
        interactive_elements=[
            _el("el_001", "a", "Admin"),
            _el("el_023", "input", type="text", label="Username"),
            _el("el_024", "input", type="password", label="Password"),
            _el("el_025", "button", "Save"),
            _el("el_026", "button", "Cancel"),
        ],
        forms=[
            FormDescriptor(
                form_id="form_121",
                submit_element_id="el_025",
                fields=[
                    FormField(
                        name="userRole",
                        field_type="select",
                        label="User Role",
                        element_id="el_021",
                    ),
                    FormField(
                        name="employeeName",
                        field_type="text",
                        label="Employee Name",
                        element_id="el_022",
                    ),
                    FormField(
                        name="username",
                        field_type="text",
                        label="Username",
                        element_id="el_023",
                    ),
                    FormField(
                        name="password",
                        field_type="password",
                        label="Password",
                        element_id="el_024",
                    ),
                    FormField(
                        name="confirm",
                        field_type="password",
                        label="Confirm Password",
                        element_id="el_027",
                    ),
                ],
            )
        ],
    )


def _login_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://thinking-tester-contact-list.herokuapp.com/login",
        title="Contact List App",
        headings=["Contact List App"],
        interactive_elements=[
            _el("el_email", "input", type="email", label="Email", accessible_name="Email"),
            _el("el_pass", "input", type="password", label="Password", accessible_name="Password"),
            _el("el_submit", "button", "Submit", accessible_name="Submit"),
            _el("el_signup", "a", "Sign up", accessible_name="Sign up"),
        ],
        forms=[
            FormDescriptor(
                form_id="form_login",
                submit_element_id="el_submit",
                fields=[
                    FormField(
                        name="email",
                        field_type="email",
                        label="Email",
                        element_id="el_email",
                    ),
                    FormField(
                        name="password",
                        field_type="password",
                        label="Password",
                        element_id="el_pass",
                    ),
                ],
            )
        ],
    )


def test_adduser_path_is_registration():
    page = _contact_list_signup("https://thinking-tester-contact-list.herokuapp.com/addUser")
    assert AuthenticationStrategy().classify_page(page) == "registration"


def test_signup_path_is_registration():
    page = _contact_list_signup("https://example.com/signup")
    assert AuthenticationStrategy().classify_page(page) == "registration"


def test_register_path_is_registration():
    page = _contact_list_signup("https://example.com/register")
    assert AuthenticationStrategy().classify_page(page) == "registration"


def test_orangehrm_admin_add_user_still_not_registration():
    """Cancel + Add User on a non-registration path stays an admin record form."""
    page = _orangehrm_admin_add_user()
    auth = AuthenticationStrategy()
    assert auth.classify_page(page) == "unknown"
    kinds = {f.kind for f in auth.detect_forms(page)}
    assert "registration" not in kinds

    authed = AuthenticationStrategy()
    authed.authenticated = True
    assert authed.classify_page(page) == "authenticated"


def test_create_test_account_on_contact_list_adduser():
    page = _contact_list_signup("https://thinking-tester-contact-list.herokuapp.com/addUser")
    auth = AuthenticationStrategy()
    forms = auth.detect_forms(page)
    assert any(f.kind == "registration" for f in forms)

    types = {
        c.candidate_type
        for c in FrontierBuilder(auth).build(page, allow_login=True, allow_registration=True)
    }
    assert "create_test_account" in types


def test_login_classification_unchanged():
    page = _login_page()
    auth = AuthenticationStrategy()
    assert auth.classify_page(page) == "login"
    kinds = {f.kind for f in auth.detect_forms(page)}
    assert kinds == {"login"}
    types = {
        c.candidate_type
        for c in FrontierBuilder(auth).build(page, allow_login=True, allow_registration=True)
    }
    assert "create_test_account" not in types
    assert "open_registration" in types

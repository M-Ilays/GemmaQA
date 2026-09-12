"""Quality fixes: auth noise, workflows, scenarios, safe fallback policy."""

from __future__ import annotations

from app.agent.bug_analyzer import BugAnalyzer, is_benign_client_noise
from app.agent.memory import RunMemory
from app.agent.tester import Tester
from app.gemma.parser import fallback_safe_action
from app.schemas import (
    ActionCategory,
    ActionResult,
    ActionType,
    BrowserAction,
    FormDescriptor,
    FormField,
    InteractiveElement,
    PageState,
    RiskLevel,
)
from app.utils.ids import new_id

ROOT = "https://thinking-tester-contact-list.herokuapp.com"


def test_benign_auth_401_is_noise():
    assert is_benign_client_noise(
        "Failed to load resource: the server responded with a status of 401 (Unauthorized)"
    )
    assert is_benign_client_noise("401 POST https://x/users/login")
    assert is_benign_client_noise("400 POST https://x/users")
    assert is_benign_client_noise(
        "400 POST https://thinking-tester-contact-list.herokuapp.com/contacts"
    )
    assert is_benign_client_noise(
        "Failed to load resource: the server responded with a status of 400 (Bad Request)"
    )
    assert not is_benign_client_noise("TypeError: x is undefined")
    assert not is_benign_client_noise("500 GET https://x/api")


def test_bug_analyzer_skips_auth_401_console_noise():
    analyzer = BugAnalyzer(gemma=None)
    page = PageState(
        page_id=new_id(),
        url=f"{ROOT}/login",
        title="Contact List App",
        console_errors=[
            "Failed to load resource: the server responded with a status of 401 (Unauthorized)"
        ],
        network_failures=["401 POST https://thinking-tester-contact-list.herokuapp.com/users/login"],
    )
    findings = analyzer.deterministic_signals(page)
    assert findings == []


def test_finalize_workflow_not_duplicated():
    memory = RunMemory(run_id=new_id(), start_url=ROOT)
    action = BrowserAction(
        action=ActionType.CLICK,
        element_id="btn-signup",
        reason="Exploring navigation control: Sign up",
        expected_result="nav",
        risk=RiskLevel.LOW,
        category=ActionCategory.NAVIGATION_TEST,
        metadata={"action_label": "Sign up"},
    )
    result = ActionResult(
        action_id=new_id(),
        run_id=memory.run_id,
        action=action,
        success=True,
        before_url=ROOT,
        after_url=f"{ROOT}/addUser",
    )
    memory.pages.append(
        PageState(
            page_id=new_id(),
            url=ROOT,
            title="Contact List App",
            interactive_elements=[
                InteractiveElement(
                    element_id="btn-signup",
                    tag="button",
                    text="Sign up",
                    accessible_name="Sign up",
                )
            ],
        )
    )
    memory.remember_action(result, before_fingerprint="fp", made_progress=True)
    memory.stop_reason = "done"
    first = memory.finalize_workflow()
    second = memory.finalize_workflow()
    assert first is not None
    assert second is None
    assert len(memory.workflows) == 1


def test_field_click_not_in_workflow():
    memory = RunMemory(run_id=new_id(), start_url=ROOT)
    memory.pages.append(
        PageState(
            page_id=new_id(),
            url=f"{ROOT}/login",
            title="Login",
            interactive_elements=[
                InteractiveElement(
                    element_id="el_email",
                    tag="input",
                    category="input",
                    input_type="email",
                    text="Email",
                    accessible_name="Email",
                )
            ],
        )
    )
    action = BrowserAction(
        action=ActionType.CLICK,
        element_id="el_email",
        reason="Click Email",
        expected_result="focus",
        risk=RiskLevel.LOW,
        category=ActionCategory.FORM_INSPECTION,
    )
    result = ActionResult(
        action_id=new_id(),
        run_id=memory.run_id,
        action=action,
        success=True,
        before_url=f"{ROOT}/login",
        after_url=f"{ROOT}/login",
    )
    memory.remember_action(result, before_fingerprint="fp", made_progress=False)
    assert memory.current_workflow_steps == []


def test_fallback_prefers_unexplored_over_nav_loop():
    page = PageState(
        page_id=new_id(),
        url=f"{ROOT}/addUser",
        title="Add User",
        interactive_elements=[
            InteractiveElement(
                element_id="btn-cancel",
                tag="button",
                text="Cancel",
                accessible_name="Cancel",
                is_visible=True,
            )
        ],
        forms=[
            FormDescriptor(
                form_id="f1",
                fields=[FormField(element_id="fn", label="First Name", field_type="text")],
            )
        ],
    )
    # Form already inspected — should open unexplored URL, not Cancel again
    action = fallback_safe_action(
        page_state=page,
        recent_actions=[
            {"action": "inspect_form", "element_id": "f1", "success": True},
            {
                "action": "click",
                "element_id": "btn-cancel",
                "success": True,
                "label": "Cancel",
                "reason": "Exploring navigation control: Cancel",
            },
        ],
        remaining_action_budget=10,
        reason="Mock provider selected a deterministic safe action.",
        unexplored_urls=[f"{ROOT}/login"],
        visited_urls=[f"{ROOT}/addUser"],
    )
    assert action.action == ActionType.OPEN_URL
    assert action.url == f"{ROOT}/login"


def test_scenario_titles_use_field_labels_not_el_ids():
    tester = Tester(gemma=None)
    page = PageState(
        page_id=new_id(),
        url=f"{ROOT}/addUser",
        title="Add User",
        forms=[
            FormDescriptor(
                form_id="f1",
                fields=[
                    FormField(
                        element_id="el_001",
                        label="First Name",
                        name="firstName",
                        field_type="text",
                        required=True,
                    ),
                    FormField(
                        element_id="el_002",
                        label="",
                        name="",
                        placeholder="Email",
                        field_type="email",
                        required=True,
                    ),
                ],
            )
        ],
    )
    specs = tester._deterministic_specs(page)
    titles = [s["title"] for s in specs]
    assert any("First Name" in t for t in titles)
    assert any("Email" in t for t in titles)
    assert not any("el_001" in t or "el_002" in t for t in titles)
    assert any(t.startswith("Smoke:") and "/addUser" in t for t in titles)

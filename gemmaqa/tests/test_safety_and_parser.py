"""Basic unit tests for safety and parsing (no browser required)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.gemma.parser import parse_action  # noqa: E402
from app.schemas import ActionType, BrowserAction, PageState, RiskLevel  # noqa: E402
from app.safety.policies import SafetyPolicy  # noqa: E402
from app.safety.validator import ActionValidator  # noqa: E402
from app.utils.ids import is_valid_id, new_id  # noqa: E402
from app.utils.sanitization import mask_secret, sanitize_dict  # noqa: E402


def test_new_id_is_uuid():
    assert is_valid_id(new_id())


def test_mask_secret():
    assert "***" in mask_secret("supersecret")


def test_sanitize_dict_masks_password():
    data = sanitize_dict({"username": "qa", "password": "hunter2"})
    assert data["username"] == "qa"
    assert "hunter2" not in str(data["password"])


def test_validator_blocks_payment_control():
    policy = SafetyPolicy(
        authorized_url="https://example.com",
        authorized_domain="example.com",
        safe_mode=True,
    )
    validator = ActionValidator(policy)
    action = BrowserAction(
        action=ActionType.CLICK,
        element_id="el_1",
        reason="Click checkout and purchase",
        risk=RiskLevel.LOW,
    )
    result = validator.validate(action)
    assert result.allowed is False


def test_dashboard_metric_list_inconsistency_detected():
    from app.agent.bug_analyzer import BugAnalyzer
    from app.schemas import InteractiveElement

    analyzer = BugAnalyzer(gemma=None)
    page = PageState(
        page_id=new_id(),
        url="https://example.com/dashboard",
        title="Dashboard",
        headings=["Dashboard", "Customers", "Recent customers"],
        visible_text_summary="Dashboard Customers 0 Active jobs 2 Recent customers Ada Lovelace Grace Hopper",
        interactive_elements=[
            InteractiveElement(
                element_id="el_a",
                tag="a",
                text="Ada Lovelace",
                href="/customers/c1",
            ),
            InteractiveElement(
                element_id="el_b",
                tag="a",
                text="Grace Hopper",
                href="/customers/c2",
            ),
        ],
    )
    findings = analyzer.deterministic_signals(page)
    assert any("count is 0" in (f.title or "").lower() for f in findings)


def test_parse_action_json():
    raw = """
    {
      "action": "click",
      "element_id": "el_12",
      "value": null,
      "reason": "Explore customers",
      "expected_result": "Customers page opens",
      "risk": "low",
      "category": "exploration"
    }
    """
    action = parse_action(raw)
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_12"


def test_validator_blocks_delete():
    policy = SafetyPolicy(
        authorized_url="https://example.com",
        authorized_domain="example.com",
        safe_mode=True,
    )
    validator = ActionValidator(policy)
    page = PageState(
        page_id=new_id(),
        url="https://example.com",
        interactive_elements=[],
    )
    # Inject a fake element via model rebuild — use reason text for pattern match
    action = BrowserAction(
        action=ActionType.CLICK,
        element_id="el_1",
        reason="Click delete workspace",
        risk=RiskLevel.LOW,
    )
    result = validator.validate(action, page_state=page)
    assert result.allowed is False


def test_validator_blocks_writes_in_safe_mode():
    policy = SafetyPolicy(
        authorized_url="https://example.com",
        authorized_domain="example.com",
        safe_mode=True,
        allow_controlled_writes=False,
    )
    validator = ActionValidator(policy)
    action = BrowserAction(
        action=ActionType.FILL,
        element_id="el_2",
        value="hello",
        reason="Fill name",
        risk=RiskLevel.LOW,
    )
    result = validator.validate(action)
    assert result.allowed is False

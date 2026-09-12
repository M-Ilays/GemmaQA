"""Mocked Gemma provider / parser tests (no real model required)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.gemma.base import ActionGenerationRequest  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.gemma.parser import (  # noqa: E402
    ActionParseError,
    extract_json,
    fallback_safe_action,
    parse_and_validate_action,
    parse_bug_analysis,
)
from app.schemas import (  # noqa: E402
    ActionCategory,
    ActionType,
    InteractiveElement,
    PageState,
    RiskLevel,
)
from app.utils.ids import new_id  # noqa: E402


def _page(*element_ids: str) -> PageState:
    elements = [
        InteractiveElement(
            element_id=eid,
            tag="a",
            category="link",
            accessible_name=f"Link {eid}",
            visible_text=f"Link {eid}",
            href=f"/{eid}",
            is_visible=True,
            is_enabled=True,
        )
        for eid in element_ids
    ]
    return PageState(
        page_id=new_id(),
        url="https://app.example.com/home",
        title="Home",
        interactive_elements=elements,
    )


def _valid_action_json(element_id: str = "el_004") -> str:
    return json.dumps(
        {
            "action": "click",
            "element_id": element_id,
            "value": None,
            "reason": "This menu opens an unexplored workflow.",
            "expected_result": "A new module page should open.",
            "risk": "low",
            "category": "exploration",
        }
    )


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


def test_valid_action_response_parsing():
    action = parse_and_validate_action(
        _valid_action_json("el_004"),
        known_element_ids={"el_004", "el_001"},
    )
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_004"
    assert action.risk == RiskLevel.LOW
    assert action.category == ActionCategory.EXPLORATION


def test_extract_json_strips_markdown_fences():
    fenced = "```json\n" + _valid_action_json("el_004") + "\n```"
    data = extract_json(fenced)
    assert data["action"] == "click"


def test_unknown_element_rejection():
    with pytest.raises(ActionParseError, match="Unknown element_id"):
        parse_and_validate_action(
            _valid_action_json("el_999"),
            known_element_ids={"el_004"},
        )


def test_unsupported_action_rejection():
    payload = {
        "action": "explode",
        "element_id": None,
        "value": None,
        "reason": "nope",
        "expected_result": "boom",
        "risk": "low",
        "category": "exploration",
    }
    with pytest.raises(ActionParseError, match="Unsupported action"):
        parse_and_validate_action(payload, known_element_ids=set())


def test_high_risk_action_rejection():
    payload = json.loads(_valid_action_json("el_004"))
    payload["risk"] = "critical"
    with pytest.raises(ActionParseError, match="High-risk"):
        parse_and_validate_action(payload, known_element_ids={"el_004"})


def test_missing_required_fields_rejection():
    with pytest.raises(ActionParseError, match="Missing required field"):
        parse_and_validate_action(
            {"action": "finish", "risk": "low", "category": "completion"},
            known_element_ids=set(),
        )


def test_empty_output_handling():
    with pytest.raises(ActionParseError, match="Empty"):
        extract_json("")
    with pytest.raises(ActionParseError, match="Empty"):
        extract_json("   ")


def test_fallback_action_selection():
    page = _page("el_001")
    action = fallback_safe_action(
        page_state=page,
        recent_actions=[],
        remaining_action_budget=10,
    )
    assert action.action in {ActionType.CLICK, ActionType.FINISH, ActionType.INSPECT_FORM}
    assert action.risk == RiskLevel.LOW


def test_bug_analysis_hypothesis_field():
    raw = {
        "classification": "suspected_bug",
        "title": "Broken nav",
        "module": "nav",
        "severity": "medium",
        "priority": "medium",
        "preconditions": [],
        "steps": ["Click Customers"],
        "expected_result": "List opens",
        "actual_result": "404",
        "business_impact": "Users blocked",
        "possible_root_cause": "Hypothesis only: missing route",
        "confidence": 0.6,
        "evidence_ids": [],
    }
    result = parse_bug_analysis(raw)
    assert "Hypothesis" in result.possible_root_cause or result.possible_root_cause


# ---------------------------------------------------------------------------
# Provider integration (mocked generate)
# ---------------------------------------------------------------------------


def test_invalid_json_correction_then_success():
    provider = MockGemmaProvider(
        scripted_responses=[
            "NOT JSON AT ALL",
            _valid_action_json("el_004"),
        ]
    )
    request = ActionGenerationRequest(
        page_state=_page("el_004"),
        remaining_action_budget=10,
    )
    action = asyncio.run(provider.generate_action(request))
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_004"
    assert len(provider.calls) == 2  # initial + correction


def test_unknown_element_triggers_retry_then_fallback_or_success():
    provider = MockGemmaProvider(
        scripted_responses=[
            _valid_action_json("el_missing"),
            _valid_action_json("el_004"),
        ]
    )
    request = ActionGenerationRequest(
        page_state=_page("el_004"),
        remaining_action_budget=10,
    )
    action = asyncio.run(provider.generate_action(request))
    assert action.element_id == "el_004"


def test_unsupported_action_retry_then_valid():
    bad = {
        "action": "self_destruct",
        "element_id": None,
        "value": None,
        "reason": "bad",
        "expected_result": "bad",
        "risk": "low",
        "category": "exploration",
    }
    provider = MockGemmaProvider(
        scripted_responses=[json.dumps(bad), _valid_action_json("el_004")]
    )
    action = asyncio.run(
        provider.generate_action(ActionGenerationRequest(page_state=_page("el_004")))
    )
    assert action.action == ActionType.CLICK


def test_high_risk_rejected_then_corrected():
    risky = json.loads(_valid_action_json("el_004"))
    risky["risk"] = "high"
    provider = MockGemmaProvider(
        scripted_responses=[json.dumps(risky), _valid_action_json("el_004")]
    )
    action = asyncio.run(
        provider.generate_action(ActionGenerationRequest(page_state=_page("el_004")))
    )
    assert action.risk == RiskLevel.LOW


def test_empty_output_falls_back_after_retry():
    provider = MockGemmaProvider(scripted_responses=["", ""])
    page = _page("el_001")
    action = asyncio.run(
        provider.generate_action(
            ActionGenerationRequest(page_state=page, remaining_action_budget=5)
        )
    )
    assert action.risk == RiskLevel.LOW
    assert action.action in {ActionType.CLICK, ActionType.FINISH, ActionType.INSPECT_FORM}


def test_model_timeout_handling_uses_fallback():
    provider = MockGemmaProvider(raise_timeout=True)
    page = _page("el_001")
    action = asyncio.run(
        provider.generate_action(
            ActionGenerationRequest(page_state=page, remaining_action_budget=5)
        )
    )
    assert action.risk == RiskLevel.LOW
    assert "fallback" in action.reason.lower() or action.action in {
        ActionType.CLICK,
        ActionType.FINISH,
        ActionType.INSPECT_FORM,
    }


def test_mock_provider_default_heuristic_no_scripts():
    provider = MockGemmaProvider()
    page = _page("el_001")
    action = asyncio.run(
        provider.generate_action(
            ActionGenerationRequest(page_state=page, remaining_action_budget=8)
        )
    )
    assert action.action in {ActionType.CLICK, ActionType.FINISH}
    assert action.element_id in {None, "el_001"}

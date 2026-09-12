"""Once the screenshot budget is gone, do not plan another take_screenshot.

If no other safe candidate remains, finish immediately instead of looping
on a validator block.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.frontier import FrontierCandidate  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.planner import Planner, screenshot_budget_exhausted  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.schemas import ActionType, BrowserAction, PageState, RiskLevel, RunConfiguration  # noqa: E402
from app.utils.ids import new_id  # noqa: E402


def _page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="http://x/",
        title="T",
        interactive_elements=[],
        state_fingerprint="fp",
    )


def _memory() -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url="http://x/")
    memory.configuration = RunConfiguration(max_actions=50, max_pages=20)
    memory.bootstrap_budgets(memory.configuration)
    return memory


def test_screenshot_budget_helper():
    assert screenshot_budget_exhausted(None) is False
    assert screenshot_budget_exhausted({}) is False
    assert screenshot_budget_exhausted({"screenshots_taken": 5}) is False
    assert screenshot_budget_exhausted({"screenshots_taken": 4, "max_screenshots": 5}) is False
    assert screenshot_budget_exhausted({"screenshots_taken": 5, "max_screenshots": 5}) is True
    assert screenshot_budget_exhausted({"screenshots_taken": 6, "max_screenshots": 5}) is True


def test_inspect_image_not_dispatched_when_budget_exhausted():
    planner = Planner(MockGemmaProvider())
    cand = FrontierCandidate(
        candidate_id="img1",
        candidate_type="inspect_image",
        action="take_screenshot",
        element_id="el_chart",
        reason="Capture supporting evidence",
        actual_label="chart",
    )
    page = _page()
    memory = _memory()
    action = planner._candidate_to_action(
        cand, page, memory, None, {"screenshots_taken": 5, "max_screenshots": 5}
    )
    assert action is None

    action = planner._candidate_to_action(cand, page, memory, None, {})
    assert action is not None
    assert action.action == ActionType.TAKE_SCREENSHOT


def test_planner_rewrites_screenshot_to_finish_when_budget_gone():
    shot = json.dumps(
        {
            "action": "take_screenshot",
            "reason": "Capture supporting evidence",
            "expected_result": "Evidence captured.",
            "risk": "low",
            "category": "evidence_capture",
        }
    )
    planner = Planner(MockGemmaProvider(scripted_responses=[shot]))
    page = _page()
    memory = _memory()
    context = {
        "screenshots_taken": 5,
        "max_screenshots": 5,
        "remaining_action_budget": 10,
        "safe_mode": True,
        "authorized_domain": "x",
    }
    action = asyncio.run(
        planner.next_action(
            page_state=page,
            previous_actions=[],
            unexplored=[],
            context=context,
            memory=memory,
        )
    )
    assert action.action != ActionType.TAKE_SCREENSHOT
    assert action.action == ActionType.FINISH


def test_planner_keeps_screenshot_when_budget_remains():
    shot = json.dumps(
        {
            "action": "take_screenshot",
            "reason": "Capture supporting evidence",
            "expected_result": "Evidence captured.",
            "risk": "low",
            "category": "evidence_capture",
        }
    )
    planner = Planner(MockGemmaProvider(scripted_responses=[shot]))
    page = _page()
    memory = _memory()
    context = {
        "screenshots_taken": 1,
        "max_screenshots": 5,
        "remaining_action_budget": 10,
        "safe_mode": True,
        "authorized_domain": "x",
    }
    action = asyncio.run(
        planner.next_action(
            page_state=page,
            previous_actions=[],
            unexplored=[],
            context=context,
            memory=memory,
        )
    )
    assert action.action == ActionType.TAKE_SCREENSHOT
    assert action.reason == "Capture supporting evidence"


def test_avoid_exhausted_screenshot_picks_other_candidate():
    planner = Planner(MockGemmaProvider())
    page = _page()
    memory = _memory()
    shot = BrowserAction(
        action=ActionType.TAKE_SCREENSHOT,
        reason="Capture supporting evidence",
        expected_result="Evidence captured.",
        risk=RiskLevel.LOW,
    )
    rewritten = planner._avoid_exhausted_screenshot(
        shot,
        page,
        memory,
        {"screenshots_taken": 5, "max_screenshots": 5, "remaining_action_budget": 10},
    )
    assert rewritten.action != ActionType.TAKE_SCREENSHOT
    assert rewritten.action == ActionType.FINISH

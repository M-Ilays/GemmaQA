"""Action/page *budgets* are gone. Counters, Pause/Continue/End Run, and safety stay."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"
sys.path.insert(0, str(BACKEND))

from app.agent.controller import AgentController  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.browser.pacing import ExecutionPacing, PAUSE_VALUES, SPEED_VALUES  # noqa: E402
from app.schemas import (  # noqa: E402
    ActionResult,
    ActionType,
    BrowserAction,
    PageState,
    RiskLevel,
    RunConfiguration,
    RunStatusEnum,
)
from app.safety.policies import SafetyPolicy  # noqa: E402
from app.safety.validator import ActionValidator  # noqa: E402
from app.utils.ids import new_id  # noqa: E402


def _policy(**kwargs) -> SafetyPolicy:
    base = dict(
        authorized_url="https://app.example.com",
        authorized_domain="app.example.com",
        safe_mode=True,
        allow_controlled_writes=False,
        allow_subdomains=False,
        allow_local_targets=False,
        max_actions=2,
        max_pages=1,
    )
    base.update(kwargs)
    return SafetyPolicy(**base)


def _click() -> BrowserAction:
    return BrowserAction(action=ActionType.CLICK, reason="explore", risk=RiskLevel.LOW)


def _remember(memory: RunMemory, n: int) -> None:
    for i in range(n):
        result = ActionResult(
            action_id=new_id(),
            run_id=memory.run_id,
            action=_click(),
            success=True,
            before_url="http://x",
            after_url=f"http://x/{i}",
        )
        memory.remember_action(result, before_fingerprint="fp", made_progress=True)


def _bare_controller() -> AgentController:
    controller = AgentController.__new__(AgentController)
    controller._cancel = asyncio.Event()
    controller._run_gate = asyncio.Event()
    controller._run_gate.set()
    controller._paused_total_s = 0.0
    controller._pause_started_at = None
    controller._started_at = None
    controller.sm = type("SM", (), {"is_terminal": False, "status": RunStatusEnum.OBSERVING})()
    controller._update_run = AsyncMock()
    controller.emit = AsyncMock()
    controller.pacing = ExecutionPacing()
    return controller


def test_a_b_new_run_ui_has_no_maximum_actions_or_pages():
    src = (FRONTEND / "pages" / "NewRunPage.tsx").read_text(encoding="utf-8")
    assert "Maximum Actions" not in src
    assert "Maximum Pages" not in src
    assert "Maximum actions" not in src
    assert "Maximum pages" not in src
    assert 'label="Maximum' not in src
    assert "Unlimited" not in src


def test_legacy_max_fields_still_parse_but_default_to_none():
    assert RunConfiguration().max_actions is None
    assert RunConfiguration().max_pages is None
    legacy = RunConfiguration(max_actions=3, max_pages=2)
    assert legacy.max_actions == 3
    assert legacy.max_pages == 2


def test_c_backend_does_not_stop_on_action_count():
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=2, max_pages=10))
    _remember(memory, 40)
    memory.remaining_action_budget = 0
    stop, reason = memory.should_stop()
    assert stop is False
    assert reason is None

    v = ActionValidator(_policy(max_actions=2, max_pages=1))
    assert v.validate(_click(), actions_taken=500).allowed is True


def test_d_backend_does_not_stop_on_page_count():
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=50, max_pages=1))
    memory.visited_urls.update([f"http://x/{i}" for i in range(25)])
    stop, reason = memory.should_stop()
    assert stop is False
    assert reason is None

    v = ActionValidator(_policy(max_actions=2, max_pages=1))
    assert v.validate(_click(), pages_visited=200).allowed is True


def test_e_action_counters_still_work_and_are_not_decremented_as_a_budget():
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=2, max_pages=1))
    remaining_before = memory.remaining_action_budget
    _remember(memory, 7)
    assert len(memory.actions) == 7
    assert memory.remaining_action_budget == remaining_before
    cov = memory.coverage()
    assert cov.action_budget_used == 7


def test_f_page_counters_still_work():
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.bootstrap_budgets(RunConfiguration(max_pages=1))
    memory.remember_page(
        PageState(page_id="p1", url="https://example.com/", title="Home", state_fingerprint="fp1")
    )
    memory.remember_page(
        PageState(
            page_id="p2",
            url="https://example.com/add",
            title="Add",
            state_fingerprint="fp2",
        )
    )
    assert len(memory.visited_urls) >= 2
    cov = memory.coverage()
    assert cov.pages_discovered >= 2


@pytest.mark.asyncio
async def test_g_h_i_pause_continue_and_end_run_still_work():
    controller = _bare_controller()
    controller.request_pause()
    assert controller.is_pause_requested()

    wait = asyncio.create_task(controller._wait_if_paused())
    await asyncio.sleep(0.05)
    assert not wait.done()

    controller.request_resume()
    await asyncio.wait_for(wait, timeout=1)
    assert not controller.is_pause_requested()

    controller.request_pause()
    wait2 = asyncio.create_task(controller._wait_if_paused())
    await asyncio.sleep(0.05)
    controller.request_cancel()
    await asyncio.wait_for(wait2, timeout=1)
    assert controller._cancel.is_set()


def test_j_safety_checks_still_block_high_risk():
    v = ActionValidator(_policy())
    blocked = v.validate(
        BrowserAction(
            action=ActionType.CLICK,
            reason="wipe production",
            risk=RiskLevel.CRITICAL,
        )
    )
    assert blocked.allowed is False
    assert "Risk level" in (blocked.reason or "")


def test_k_fatal_fail_path_still_stops():
    src = (BACKEND / "app" / "agent" / "controller.py").read_text(encoding="utf-8")
    assert 'await self.emit("run_failed"' in src
    assert "status=RunStatusEnum.FAILED" in src
    assert "Maximum runtime exceeded" not in src
    assert "request_cancel" in src


def test_l_execution_speed_controls_still_work():
    p = ExecutionPacing()
    for speed in SPEED_VALUES:
        assert p.update(execution_speed=speed)["execution_speed"] == speed
    live = (FRONTEND / "pages" / "LiveRunPage.tsx").read_text(encoding="utf-8")
    assert "ExecutionPacingControls" in live
    assert 'label="Actions"' in live
    assert 'label="Pages"' in live


def test_m_action_pause_still_works():
    p = ExecutionPacing()
    for pause in PAUSE_VALUES:
        assert p.update(action_pause=pause)["action_pause"] == pause
    live = (FRONTEND / "pages" / "LiveRunPage.tsx").read_text(encoding="utf-8")
    assert ">Pause<" in live or "Pause" in live
    assert "Continue" in live
    assert "End run" in live


def test_planner_and_validator_have_no_action_page_budget_stops():
    planner = (BACKEND / "app" / "agent" / "planner.py").read_text(encoding="utf-8")
    validator = (BACKEND / "app" / "agent" / "memory.py").read_text(encoding="utf-8")
    safety = (BACKEND / "app" / "safety" / "validator.py").read_text(encoding="utf-8")
    assert "remaining_action_budget <= 0" not in planner
    assert "remaining_action_budget <= 1" not in planner
    assert "Action budget exhausted" not in safety
    assert "Page budget exhausted" not in safety
    assert "return True, \"action_budget_exhausted\"" not in validator
    assert "pages_visited >= self.policy.max_pages" not in safety
    assert "actions_taken >= self.policy.max_actions" not in safety
    assert "Maximum runtime exceeded" not in safety
    assert "runtime_seconds >= self.policy.max_runtime_seconds" not in safety

"""No user-facing maximum run runtime. Gemma request timeout stays 60s."""

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
from app.config import get_settings  # noqa: E402
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
        max_runtime_seconds=900,
    )
    base.update(kwargs)
    return SafetyPolicy(**base)


def _click() -> BrowserAction:
    return BrowserAction(action=ActionType.CLICK, reason="explore", risk=RiskLevel.LOW)


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


def test_a_no_900_second_runtime_stop_in_controller_or_validator():
    controller = (BACKEND / "app" / "agent" / "controller.py").read_text(encoding="utf-8")
    validator = (BACKEND / "app" / "safety" / "validator.py").read_text(encoding="utf-8")
    assert "Maximum runtime exceeded" not in controller
    assert "Maximum runtime exceeded" not in validator
    assert "runtime_s >= self.policy.max_runtime_seconds" not in controller
    assert "runtime_seconds >= self.policy.max_runtime_seconds" not in validator
    new_run = (FRONTEND / "pages" / "NewRunPage.tsx").read_text(encoding="utf-8")
    assert "Maximum Runtime" not in new_run
    assert "Maximum runtime" not in new_run
    live = (FRONTEND / "pages" / "LiveRunPage.tsx").read_text(encoding="utf-8")
    assert "15-minute" not in live


def test_b_elapsed_time_over_900_seconds_does_not_stop():
    v = ActionValidator(_policy(max_runtime_seconds=900))
    assert v.validate(_click(), runtime_seconds=901).allowed is True
    assert v.validate(_click(), runtime_seconds=10_000).allowed is True
    # Old stored configs that still carry 900 must not become a stop.
    cfg = RunConfiguration(max_runtime_seconds=900)
    assert cfg.max_runtime_seconds == 900
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(cfg)
    stop, reason = memory.should_stop()
    assert stop is False
    assert reason is None


def test_c_e_end_run_pause_continue_and_cancel_still_work():
    controller = _bare_controller()
    controller.request_pause()
    assert controller.is_pause_requested()
    controller.request_cancel()
    assert controller._cancel.is_set()


@pytest.mark.asyncio
async def test_d_e_f_pause_continue_and_cancel_still_work():
    controller = _bare_controller()
    controller.request_pause()
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


def test_g_fatal_fail_path_still_present():
    src = (BACKEND / "app" / "agent" / "controller.py").read_text(encoding="utf-8")
    assert 'await self.emit("run_failed"' in src
    assert "status=RunStatusEnum.FAILED" in src


def test_h_safety_checks_still_block_critical_risk():
    blocked = ActionValidator(_policy()).validate(
        BrowserAction(action=ActionType.CLICK, reason="wipe", risk=RiskLevel.CRITICAL)
    )
    assert blocked.allowed is False


def test_i_gemma_timeout_remains_sixty_seconds():
    from app.config import Settings

    assert "max_runtime_seconds" not in Settings.model_fields
    settings = get_settings()
    assert settings.gemma_timeout_seconds == 60.0
    example = (BACKEND / ".env.example").read_text(encoding="utf-8")
    assert "MAX_RUNTIME_SECONDS" not in example
    root_example = BACKEND.parents[0] / ".env.example"
    root = root_example.read_text(encoding="utf-8")
    assert "GEMMA_TIMEOUT_SECONDS=60" in root
    assert "MAX_RUNTIME_SECONDS" not in root


def test_j_k_execution_speed_and_action_pause_unchanged():
    p = ExecutionPacing()
    for speed in SPEED_VALUES:
        assert p.update(execution_speed=speed)["execution_speed"] == speed
    for pause in PAUSE_VALUES:
        assert p.update(action_pause=pause)["action_pause"] == pause
    live = (FRONTEND / "pages" / "LiveRunPage.tsx").read_text(encoding="utf-8")
    assert "ExecutionPacingControls" in live


def test_l_m_action_and_page_counters_still_work():
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.bootstrap_budgets(RunConfiguration())
    result = ActionResult(
        action_id=new_id(),
        run_id=memory.run_id,
        action=_click(),
        success=True,
        before_url="https://example.com/",
        after_url="https://example.com/add",
    )
    memory.remember_action(result, before_fingerprint="fp", made_progress=True)
    memory.remember_page(
        PageState(
            page_id="p1",
            url="https://example.com/",
            title="Home",
            state_fingerprint="fp1",
        )
    )
    assert len(memory.actions) == 1
    assert len(memory.visited_urls) >= 1


def test_n_action_page_budgets_remain_removed():
    v = ActionValidator(_policy(max_actions=2, max_pages=1, max_runtime_seconds=900))
    assert v.validate(_click(), actions_taken=500, pages_visited=200).allowed is True
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=2, max_pages=1, max_runtime_seconds=900))
    memory.remaining_action_budget = 0
    memory.visited_urls.update([f"http://x/{i}" for i in range(10)])
    stop, reason = memory.should_stop()
    assert stop is False
    assert reason is None
    new_run = (FRONTEND / "pages" / "NewRunPage.tsx").read_text(encoding="utf-8")
    assert "Maximum Actions" not in new_run
    assert "Maximum Pages" not in new_run


def test_legacy_max_runtime_field_still_parses_but_defaults_to_none():
    assert RunConfiguration().max_runtime_seconds is None
    assert RunConfiguration(max_runtime_seconds=900).max_runtime_seconds == 900

"""Live execution speed / action-pause — operator pacing only.

Gemma inference, timeouts, safety, and action *choice* stay out of this module.
At 1x + 0s the waits are no-ops so Playwright actions keep today's timing.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
import sys

sys.path.insert(0, str(BACKEND))

from app.browser.pacing import (  # noqa: E402
    PAUSE_VALUES,
    SPEED_VALUES,
    ExecutionPacing,
    highlight_ms_for,
    pacing_activity_line,
    watch_ms_for,
)
from app.config import get_settings  # noqa: E402
from app.reporting.activity_log import summarize  # noqa: E402

CONTROLLER = BACKEND / "app" / "agent" / "controller.py"
PACING = BACKEND / "app" / "browser" / "pacing.py"
EXECUTOR = BACKEND / "app" / "browser" / "executor.py"
OPENAI = BACKEND / "app" / "gemma" / "openai_compatible.py"


def test_default_speed_is_1x():
    assert ExecutionPacing().speed == 1.0
    assert watch_ms_for(1.0) == 0


def test_default_action_pause_is_0s():
    assert ExecutionPacing().pause_s == 0.0


def test_all_speed_values_are_accepted():
    assert SPEED_VALUES == (0.25, 0.5, 1.0, 1.5, 2.0, 4.0)
    p = ExecutionPacing()
    for speed in SPEED_VALUES:
        assert p.update(execution_speed=speed)["execution_speed"] == speed


def test_all_pause_values_are_accepted():
    assert PAUSE_VALUES == (0.0, 0.5, 1.0, 2.0, 5.0, 10.0)
    p = ExecutionPacing()
    for pause in PAUSE_VALUES:
        assert p.update(action_pause=pause)["action_pause"] == pause


def test_unknown_speed_or_pause_is_rejected():
    p = ExecutionPacing()
    with pytest.raises(ValueError):
        p.update(execution_speed=3.0)
    with pytest.raises(ValueError):
        p.update(action_pause=3.0)
    assert p.speed == 1.0
    assert p.pause_s == 0.0


def test_speed_changes_execution_pacing():
    assert watch_ms_for(0.25) > watch_ms_for(0.5) > watch_ms_for(1.0)
    assert watch_ms_for(1.0) == watch_ms_for(1.5) == watch_ms_for(4.0) == 0
    assert highlight_ms_for(4.0) < highlight_ms_for(1.0) < highlight_ms_for(0.25)


@pytest.mark.asyncio
async def test_action_pause_occurs_between_actions():
    p = ExecutionPacing()
    p.update(action_pause=0.5)
    started = time.perf_counter()
    assert await p.after_action() is True
    assert await p.after_action() is True
    elapsed = time.perf_counter() - started
    assert elapsed >= 1.0


@pytest.mark.asyncio
async def test_default_pacing_does_not_delay_actions():
    p = ExecutionPacing()
    started = time.perf_counter()
    assert await p.wait_for_highlight() is True
    assert await p.after_action() is True
    assert time.perf_counter() - started < 0.15


def test_speed_does_not_modify_gemma_timeout():
    src = PACING.read_text(encoding="utf-8")
    assert "gemma_timeout" not in src
    assert "GEMMA_TIMEOUT" not in src
    settings = get_settings()
    assert settings.gemma_timeout_seconds == 60.0
    openai = OPENAI.read_text(encoding="utf-8")
    assert "settings.gemma_timeout_seconds" in openai


def test_speed_and_pause_do_not_modify_gemma_inference():
    src = PACING.read_text(encoding="utf-8")
    assert "app.gemma" not in src
    assert "generate(" not in src
    assert "planner" not in src.lower()
    controller = CONTROLLER.read_text(encoding="utf-8")
    # Pacing is attached to the executor; Gemma still goes through Planner.
    assert "self.planner = Planner(self.gemma)" in controller
    assert "executor.pacing = self.pacing" in controller


def test_changing_speed_during_a_session_affects_subsequent_actions():
    p = ExecutionPacing()
    assert p.watch_ms() == 0
    p.update(execution_speed=0.25)
    assert p.watch_ms() > 0
    p.update(execution_speed=1.0)
    assert p.watch_ms() == 0


def test_changing_pause_during_a_session_affects_subsequent_actions():
    p = ExecutionPacing()
    p.update(action_pause=5.0)
    assert p.pause_s == 5.0
    p.update(action_pause=0.0)
    assert p.pause_s == 0.0


def test_existing_pause_continue_still_work():
    src = CONTROLLER.read_text(encoding="utf-8")
    assert "def request_pause(self)" in src
    assert "def request_resume(self)" in src
    assert "async def _wait_if_paused" in src
    assert "self._run_gate.clear()" in src
    assert "self._run_gate.set()" in src
    pacing = PACING.read_text(encoding="utf-8")
    assert "wait_if_paused" in pacing


def test_existing_stop_still_works():
    src = CONTROLLER.read_text(encoding="utf-8")
    assert "def request_cancel(self)" in src
    assert "self._cancel.set()" in src


@pytest.mark.asyncio
async def test_stop_cancels_pending_execution_delay():
    p = ExecutionPacing()
    cancel = asyncio.Event()
    p.bind(is_cancelled=cancel.is_set, wait_if_paused=None)
    p.update(action_pause=10.0)
    task = asyncio.create_task(p.after_action())
    await asyncio.sleep(0.15)
    cancel.set()
    started = time.perf_counter()
    assert await task is False
    assert time.perf_counter() - started < 0.5


def test_actions_at_1x_and_0s_keep_the_existing_click_path():
    executor = EXECUTOR.read_text(encoding="utf-8")
    assert "click_element" in executor
    assert "if self.pacing is None" in executor
    assert "settle_ms" in executor
    # Speed must not rewrite the settle that submit classification depends on.
    assert "self.settle_ms /" not in executor
    assert "self.settle_ms *" not in executor


def test_safety_checks_are_unchanged():
    src = CONTROLLER.read_text(encoding="utf-8")
    validate_at = src.index("validation = self.validator.validate(")
    execute_at = src.index("result = await executor.execute(")
    assert validate_at < execute_at


def test_activity_log_still_records_pacing_and_existing_events(tmp_path: Path):
    from app.reporting.activity_log import ActivityLog

    log = ActivityLog("run_pace", root=tmp_path)
    first = log.record("page_observed", {"title": "Contact List", "elements": 3})
    second = log.record("execution_pacing", {"execution_speed": 0.5, "action_pause": 2})
    assert "Contact List" in first["summary"]
    assert second["summary"] == "Speed: 0.5x | Action pause: 2s"
    assert summarize("execution_pacing", {"execution_speed": 0.5, "action_pause": 2}) == (
        "Speed: 0.5x | Action pause: 2s"
    )
    assert pacing_activity_line(1, 0) == "Speed: 1x | Action pause: 0s"


@pytest.mark.asyncio
async def test_pause_gate_is_honoured_during_a_delay():
    """Operator Pause during Action Pause must freeze, then Continue resumes."""
    p = ExecutionPacing()
    cancel = asyncio.Event()
    gate = asyncio.Event()
    gate.set()
    paused = {"n": 0}

    async def wait_if_paused() -> None:
        if gate.is_set():
            return
        paused["n"] += 1
        await gate.wait()

    p.bind(is_cancelled=cancel.is_set, wait_if_paused=wait_if_paused)
    p.update(action_pause=0.5)
    task = asyncio.create_task(p.after_action())
    await asyncio.sleep(0.15)
    gate.clear()
    await asyncio.sleep(0.2)
    gate.set()
    assert await task is True
    assert paused["n"] >= 1

"""Operator pause / continue keeps the browser and freezes the time budget."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.controller import AgentController, _utc_now  # noqa: E402
from app.schemas import RunStatusEnum  # noqa: E402


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
    return controller


def test_runtime_excludes_accumulated_pause_time() -> None:
    controller = _bare_controller()
    controller._started_at = _utc_now() - timedelta(seconds=30)
    controller._paused_total_s = 20.0
    elapsed = controller._runtime_seconds()
    assert 8.0 <= elapsed <= 12.0


def test_runtime_excludes_in_progress_pause() -> None:
    controller = _bare_controller()
    controller._started_at = _utc_now() - timedelta(seconds=40)
    controller._pause_started_at = _utc_now() - timedelta(seconds=25)
    elapsed = controller._runtime_seconds()
    assert 13.0 <= elapsed <= 17.0


@pytest.mark.asyncio
async def test_wait_if_paused_blocks_until_resume() -> None:
    controller = _bare_controller()
    controller.request_pause()
    assert controller.is_pause_requested()

    task = asyncio.create_task(controller._wait_if_paused())
    await asyncio.sleep(0.05)
    assert not task.done()
    controller._update_run.assert_awaited()
    controller.emit.assert_awaited()

    controller.request_resume()
    await asyncio.wait_for(task, timeout=1)
    assert controller._paused_total_s > 0
    assert controller._pause_started_at is None
    events = [call.args[0] for call in controller.emit.await_args_list]
    assert "run_paused" in events
    assert "run_resumed" in events


@pytest.mark.asyncio
async def test_end_unblocks_a_paused_wait() -> None:
    controller = _bare_controller()
    controller.request_pause()
    task = asyncio.create_task(controller._wait_if_paused())
    await asyncio.sleep(0.05)
    controller.request_cancel()
    await asyncio.wait_for(task, timeout=1)
    events = [call.args[0] for call in controller.emit.await_args_list]
    assert "run_resumed" not in events

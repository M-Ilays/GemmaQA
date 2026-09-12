"""Operator execution pacing — speed and inter-action pause.

Observational timing only. This module never calls Gemma, never changes
timeouts or retries, and never decides which action to run. It scales the
live-action highlight and, when asked, waits *after* Playwright returns.

At speed=1x and action_pause=0s every wait is a no-op, so browser actions
keep today's timing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

# Matches the existing live-indicator duration at 1x.
BASE_HIGHLIGHT_MS = 1600
MIN_HIGHLIGHT_MS = 200

SPEED_VALUES: tuple[float, ...] = (0.25, 0.5, 1.0, 1.5, 2.0, 4.0)
PAUSE_VALUES: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 5.0, 10.0)

_CHUNK_S = 0.1


def highlight_ms_for(speed: float) -> int:
    """How long the in-page ring stays visible. Faster speed → shorter ring."""
    if speed <= 0:
        speed = 1.0
    return max(MIN_HIGHLIGHT_MS, int(round(BASE_HIGHLIGHT_MS / speed)))


def watch_ms_for(speed: float) -> int:
    """Python wait after Playwright so a slow highlight can be seen.

    1x and faster: 0 — same as before this feature (highlight is fire-and-forget).
    Slower than 1x: wait the scaled highlight duration before the after-screenshot
    strips the overlay.
    """
    if speed >= 1.0:
        return 0
    return highlight_ms_for(speed)


class ExecutionPacing:
    """Latest operator speed/pause. Safe to update while a run is in flight."""

    def __init__(self) -> None:
        self._speed = 1.0
        self._pause_s = 0.0
        self._is_cancelled: Callable[[], bool] = lambda: False
        self._wait_if_paused: Callable[[], Awaitable[None]] | None = None

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def pause_s(self) -> float:
        return self._pause_s

    def bind(
        self,
        *,
        is_cancelled: Callable[[], bool],
        wait_if_paused: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._is_cancelled = is_cancelled
        self._wait_if_paused = wait_if_paused

    def update(
        self,
        *,
        execution_speed: float | None = None,
        action_pause: float | None = None,
    ) -> dict[str, float]:
        if execution_speed is not None:
            speed = float(execution_speed)
            if speed not in SPEED_VALUES:
                raise ValueError(f"execution_speed must be one of {SPEED_VALUES}")
            self._speed = speed
        if action_pause is not None:
            pause = float(action_pause)
            if pause not in PAUSE_VALUES:
                raise ValueError(f"action_pause must be one of {PAUSE_VALUES}")
            self._pause_s = pause
        return self.snapshot()

    def snapshot(self) -> dict[str, float]:
        return {
            "execution_speed": self._speed,
            "action_pause": self._pause_s,
        }

    def highlight_ms(self) -> int:
        return highlight_ms_for(self._speed)

    def watch_ms(self) -> int:
        return watch_ms_for(self._speed)

    async def wait_for_highlight(self) -> bool:
        """After the click/fill, before evidence capture. False if Stop fired."""
        return await self._interruptible_sleep(self.watch_ms() / 1000.0)

    async def after_action(self, *, skip: bool = False) -> bool:
        """Fixed Action Pause between completed actions. False if Stop fired."""
        if skip:
            return True
        return await self._interruptible_sleep(self._pause_s)

    async def _interruptible_sleep(self, seconds: float) -> bool:
        if seconds <= 0:
            return True
        remaining = seconds
        while remaining > 0:
            if self._is_cancelled():
                return False
            if self._wait_if_paused is not None:
                await self._wait_if_paused()
                if self._is_cancelled():
                    return False
            chunk = min(_CHUNK_S, remaining)
            started = time.monotonic()
            try:
                await asyncio.sleep(chunk)
            except asyncio.CancelledError:
                return False
            remaining -= time.monotonic() - started
        return not self._is_cancelled()


def pacing_activity_line(speed: float, pause_s: float) -> str:
    return f"Speed: {speed:g}x | Action pause: {pause_s:g}s"

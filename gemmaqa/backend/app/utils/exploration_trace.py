"""Opt-in structured per-iteration exploration trace.

Enabled with GEMMAQA_EXPLORATION_TRACE=true. Distinct from GEMMAQA_AUTH_TRACE (which
covers only the authentication planning path) — this covers the full exploration
decision loop: current page/state, active goal, gaps, generated candidates and their
scores, rejected candidates and why, the selected candidate, progress evaluation,
state transitions, goal lifecycle changes, and stop-policy decisions.

Never logs credentials, passwords, or raw field values — only structural/semantic
data (ids, counts, statuses, reasons). Field values are redacted by key name
regardless of nesting depth.
"""

from __future__ import annotations

import os
from typing import Any

from app.utils.logging import get_logger

logger = get_logger("exploration.trace")

_SECRET_KEYS = {
    "value",
    "password",
    "username",
    "email",
    "secret",
    "token",
    "current_value",
    "planned_values",
}


def enabled() -> bool:
    return os.environ.get("GEMMAQA_EXPLORATION_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}


def _sanitize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***redacted***" if str(k).lower() in _SECRET_KEYS else _sanitize(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_sanitize(x) for x in obj]
    return obj


def record(event: str, **fields: Any) -> None:
    """Emit one structured trace record if tracing is enabled; a no-op otherwise so
    call sites never pay for building the payload when tracing is off."""
    if not enabled():
        return
    logger.info("EXPLORATION_TRACE %s %s", event, _sanitize(fields))

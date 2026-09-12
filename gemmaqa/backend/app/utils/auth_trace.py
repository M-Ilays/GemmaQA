"""Opt-in structured tracing for the authentication planning path.

Enabled with GEMMAQA_AUTH_TRACE=true. Never logs field values or secrets —
only semantic types, element ids, counts, and decisions.
"""

from __future__ import annotations

import os
from typing import Any

from app.utils.logging import get_logger

logger = get_logger("auth.trace")

_SECRET_KEYS = {"value", "password", "username", "email", "secret", "token"}


def enabled() -> bool:
    return os.environ.get("GEMMAQA_AUTH_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}


def trace(event: str, **fields: Any) -> None:
    if not enabled():
        return
    safe = {k: v for k, v in fields.items() if k.lower() not in _SECRET_KEYS}
    parts = " ".join(f"{k}={safe[k]!r}" for k in safe)
    logger.info("AUTH_TRACE %s %s", event, parts)

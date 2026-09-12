"""Per-invocation request bag. Credentials never enter the Strands prompt."""

from __future__ import annotations

from contextvars import ContextVar

from app.schemas import CreateRunRequest

active_run_request: ContextVar[CreateRunRequest | None] = ContextVar(
    "strands_active_run_request",
    default=None,
)
last_launched_run_id: ContextVar[str | None] = ContextVar(
    "strands_last_launched_run_id",
    default=None,
)

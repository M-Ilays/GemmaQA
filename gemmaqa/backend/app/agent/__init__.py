"""Agent package."""

from __future__ import annotations

from typing import Any

__all__ = [
    "AgentController",
    "ACTIVE_RUNS",
    "create_and_start_run",
    "RunMemory",
    "action_signature",
    "AgentPhase",
    "StateMachine",
    "identifiable_name",
    "values_for_field",
]


def __getattr__(name: str) -> Any:
    if name in {"AgentController", "ACTIVE_RUNS", "create_and_start_run"}:
        from app.agent import controller as _controller

        return getattr(_controller, name)
    if name in {"RunMemory", "action_signature"}:
        from app.agent import memory as _memory

        return getattr(_memory, name)
    if name in {"AgentPhase", "StateMachine"}:
        from app.agent import state_machine as _sm

        return getattr(_sm, name)
    if name in {"identifiable_name", "values_for_field"}:
        from app.agent import test_data as _td

        return getattr(_td, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Deterministic execution state machine for one investigation.

Mirrors `app/agent/state_machine.py`'s shape (a `phase` + `history` tracker)
but for the Autonomous Investigation Engine's own 16 states, which are
entirely distinct from the run-level `AgentPhase` enum -- an investigation
can be `Executing` while the run-level phase is simply `EXECUTING` for
whatever concrete action that step produced.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import EXECUTION_STATES, TERMINAL_STATES

# Explicit allow-list of transitions. A transition not listed here is
# rejected by `InvestigationStateMachine.transition` (returns False, never
# raises -- callers decide how to react, exactly like every other
# non-fatal engine hook in this codebase).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "idle": frozenset({"preparing"}),
    "preparing": frozenset({"ready", "blocked", "failed", "cancelled"}),
    "ready": frozenset({"executing", "paused", "cancelled", "blocked"}),
    "executing": frozenset({"waiting", "recovery", "cancelled"}),
    "waiting": frozenset({"observing", "recovery", "cancelled"}),
    "observing": frozenset({"collecting_evidence", "recovery", "cancelled"}),
    "collecting_evidence": frozenset({"verifying", "cancelled"}),
    "verifying": frozenset({"planning_next", "cancelled"}),
    "planning_next": frozenset({"ready", "updating_knowledge", "cancelled"}),
    "updating_knowledge": frozenset({"completed", "blocked", "failed", "cancelled"}),
    "recovery": frozenset({"ready", "failed", "blocked", "cancelled"}),
    "paused": frozenset({"ready", "cancelled"}),
    "completed": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class InvestigationStateMachine:
    def __init__(self, initial: str = "idle") -> None:
        self.state = initial
        self.history: list[str] = [initial]

    def can_transition(self, target: str) -> bool:
        return target in ALLOWED_TRANSITIONS.get(self.state, frozenset())

    def transition(self, target: str) -> bool:
        if target not in EXECUTION_STATES:
            return False
        if not self.can_transition(target):
            return False
        self.state = target
        self.history.append(target)
        return True

    def force(self, target: str) -> None:
        """Escape hatch for `cancelled` (user cancellation must always be
        honoured regardless of current state) -- the only transition every
        non-terminal state implicitly allows."""
        self.state = target
        self.history.append(target)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_paused(self) -> bool:
        return self.state == "paused"

"""Agent run state machine with live-run statuses."""

from __future__ import annotations

from enum import Enum

from app.schemas import RunStatusEnum


class AgentPhase(str, Enum):
    CREATED = "created"
    INITIALIZING = "initializing"
    OPENING_BROWSER = "opening_browser"
    NAVIGATING = "navigating"
    AUTHENTICATING = "authenticating"
    OBSERVING = "observing"
    PLANNING = "planning"
    EXECUTING = "executing"
    ANALYZING = "analyzing"
    DOCUMENTING = "documenting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


PHASE_TO_STATUS: dict[AgentPhase, RunStatusEnum] = {
    AgentPhase.CREATED: RunStatusEnum.CREATED,
    AgentPhase.INITIALIZING: RunStatusEnum.INITIALIZING,
    AgentPhase.OPENING_BROWSER: RunStatusEnum.OPENING_BROWSER,
    AgentPhase.NAVIGATING: RunStatusEnum.NAVIGATING,
    AgentPhase.AUTHENTICATING: RunStatusEnum.AUTHENTICATING,
    AgentPhase.OBSERVING: RunStatusEnum.OBSERVING,
    AgentPhase.PLANNING: RunStatusEnum.PLANNING,
    AgentPhase.EXECUTING: RunStatusEnum.EXECUTING,
    AgentPhase.ANALYZING: RunStatusEnum.ANALYZING,
    AgentPhase.DOCUMENTING: RunStatusEnum.DOCUMENTING,
    AgentPhase.COMPLETED: RunStatusEnum.COMPLETED,
    AgentPhase.FAILED: RunStatusEnum.FAILED,
    AgentPhase.CANCELLED: RunStatusEnum.CANCELLED,
}


class StateMachine:
    """Tracks phase transitions for an exploratory run."""

    def __init__(self) -> None:
        self.phase = AgentPhase.CREATED
        self.history: list[AgentPhase] = [AgentPhase.CREATED]

    def transition(self, next_phase: AgentPhase) -> AgentPhase:
        self.phase = next_phase
        self.history.append(next_phase)
        return self.phase

    @property
    def status(self) -> RunStatusEnum:
        return PHASE_TO_STATUS.get(self.phase, RunStatusEnum.CREATED)

    @property
    def is_terminal(self) -> bool:
        return self.phase in {
            AgentPhase.COMPLETED,
            AgentPhase.FAILED,
            AgentPhase.CANCELLED,
        }

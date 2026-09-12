"""Autonomous Investigation Engine schemas — application-neutral by
construction, same closed-vocabulary discipline as every other reasoning
engine in this lineage.

Determinism discipline (carried forward from Goal Generation -> Scenario
Planning -> QA Strategy): no field here defaults to a random id. Every id
is either required or derived deterministically from the investigation/
candidate/scenario/step id it wraps.

This package is the ONLY layer allowed to actually drive execution — but it
does so exclusively by calling the EXISTING runtime Planner, which itself
calls BrowserAdapter/ActionExecutor exactly as it always has. Nothing here
ever imports Playwright, BrowserAdapter, or ActionExecutor directly — see
`__init__.py` for the full scope statement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

EXECUTION_STATES = frozenset(
    {
        "idle", "preparing", "ready", "executing", "waiting", "observing", "collecting_evidence",
        "verifying", "updating_knowledge", "planning_next", "completed", "blocked", "failed",
        "cancelled", "paused", "recovery",
    }
)
TERMINAL_STATES = frozenset({"completed", "blocked", "failed", "cancelled"})

ASSERTION_OUTCOMES = frozenset({"supported", "contradicted", "inconclusive"})

FAILURE_CLASSES = frozenset(
    {
        "navigation_failure", "timeout", "missing_element", "unexpected_dialog", "page_refresh",
        "session_expiry", "api_failure", "network_interruption", "stale_dom", "capability_unavailable", "unknown",
    }
)

RECOVERY_ACTIONS = frozenset({"retry", "retry_with_backoff", "skip_step", "abort_scenario", "reobserve", "none"})

STOP_REASONS = frozenset(
    {
        "no_executable_scenarios", "coverage_target_reached", "safety_violation", "repeated_failures",
        "budget_exceeded", "time_exceeded", "max_actions_reached", "user_cancellation", "none",
        # Added for richer, more specific stop-reason reporting: distinguishes
        # "nothing was ever executable" (no_executable_scenarios) from "we
        # finished everything we could" vs. "everything left is blocked" vs.
        # a hard infrastructure failure, so operators get an actionable
        # answer instead of one catch-all reason.
        "frontier_exhausted", "all_executable_scenarios_completed", "all_remaining_scenarios_blocked",
        "no_safe_action_available", "authentication_lost", "planner_unavailable", "provider_unavailable",
        "fatal_browser_error",
    }
)

INVESTIGATION_OUTCOMES = frozenset({"completed", "blocked", "failed", "cancelled", "paused"})


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _validator_for(allowed: frozenset, label: str):
    def _validate(cls, value: str) -> str:
        if value and value not in allowed:
            raise ValueError(f"Invalid {label}: {value!r} (expected one of {sorted(allowed)})")
        return value

    return _validate


# ---------------------------------------------------------------------------
# Precondition / safety checks
# ---------------------------------------------------------------------------


class PreconditionCheckResult(BaseModel):
    satisfied: bool = False
    deferred: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    deferred_reasons: list[str] = Field(default_factory=list)
    checked_requirement_types: list[str] = Field(default_factory=list)


class SafetyGateResult(BaseModel):
    allowed: bool = False
    reason: str = ""
    risk_class: str = "unknown"


# ---------------------------------------------------------------------------
# Execution trace / evidence / verification
# ---------------------------------------------------------------------------


class ExecutionTrace(BaseModel):
    trace_id: str
    investigation_id: str
    scenario_id: str = ""
    step_id: str = ""
    sequence_index: int = 0
    state: str = "idle"
    action_type: str = ""
    action_summary: str = ""
    success: bool = False
    duration_ms: int = 0
    evidence_ids: list[str] = Field(default_factory=list)
    error: str = ""
    recovery_action: str = "none"
    started_at: datetime = Field(default_factory=datetime.utcnow)

    _validate_state = field_validator("state")(classmethod(_validator_for(EXECUTION_STATES, "execution_state")))
    _validate_recovery = field_validator("recovery_action")(classmethod(_validator_for(RECOVERY_ACTIONS, "recovery_action")))


class AssertionResult(BaseModel):
    assertion_result_id: str
    investigation_id: str
    source_assertion_id: str = ""
    subject_id: str = ""
    operator: str = "unknown"
    outcome: str = "inconclusive"
    observed_value: str = ""
    expected_value: str = ""
    confidence: float = 0.0
    explanation: str = ""

    _validate_outcome = field_validator("outcome")(classmethod(_validator_for(ASSERTION_OUTCOMES, "assertion_outcome")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class VerificationResult(BaseModel):
    verification_id: str
    investigation_id: str
    scenario_id: str = ""
    assertion_results: list[AssertionResult] = Field(default_factory=list)
    supported_count: int = 0
    contradicted_count: int = 0
    inconclusive_count: int = 0
    overall_outcome: str = "inconclusive"

    _validate_outcome = field_validator("overall_outcome")(classmethod(_validator_for(ASSERTION_OUTCOMES | {"mixed"}, "overall_outcome")))


class EvidenceBundle(BaseModel):
    """References only -- never a copy of a raw evidence payload. Every id
    here is one already produced by the existing EvidenceCollector via
    ActionExecutor; this bundle just groups them by investigation."""

    bundle_id: str
    investigation_id: str
    scenario_id: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    screenshot_paths: list[str] = Field(default_factory=list)
    urls_visited: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    network_errors: list[str] = Field(default_factory=list)
    total_duration_ms: int = 0


# ---------------------------------------------------------------------------
# Knowledge / coverage / confidence / follow-up goals
# ---------------------------------------------------------------------------


class KnowledgeUpdates(BaseModel):
    update_id: str
    investigation_id: str
    graph_version_before: int = 0
    graph_version_after: int = 0
    node_count_before: int = 0
    node_count_after: int = 0
    edge_count_before: int = 0
    edge_count_after: int = 0
    resolved_gap_count: int = 0
    new_gap_count: int = 0
    new_contradiction_count: int = 0
    explanation: str = ""


class CoverageUpdates(BaseModel):
    update_id: str
    investigation_id: str
    gap_count_before: int = 0
    gap_count_after: int = 0
    feasible_scenario_count_before: int = 0
    feasible_scenario_count_after: int = 0
    explanation: str = ""


class ConfidenceUpdates(BaseModel):
    update_id: str
    investigation_id: str
    consistency_issue_count_before: int = 0
    consistency_issue_count_after: int = 0
    average_scenario_confidence_before: float = 0.0
    average_scenario_confidence_after: float = 0.0
    explanation: str = ""


class NextGoals(BaseModel):
    next_goals_id: str
    investigation_id: str
    new_goal_ids: list[str] = Field(default_factory=list)
    triggered_by: str = ""


# ---------------------------------------------------------------------------
# Investigation result / summary
# ---------------------------------------------------------------------------


class InvestigationResult(BaseModel):
    investigation_id: str
    candidate_id: str = ""
    scenario_id: str = ""
    goal_id: str = ""
    outcome: str = "paused"
    state: str = "idle"
    steps_total: int = 0
    steps_executed: int = 0
    execution_trace: list[ExecutionTrace] = Field(default_factory=list)
    verification: Optional[VerificationResult] = None
    evidence_bundle: Optional[EvidenceBundle] = None
    knowledge_updates: Optional[KnowledgeUpdates] = None
    coverage_updates: Optional[CoverageUpdates] = None
    confidence_updates: Optional[ConfidenceUpdates] = None
    next_goals: Optional[NextGoals] = None
    blockers: list[str] = Field(default_factory=list)
    failure_reason: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    duration_ms: int = 0

    _validate_outcome = field_validator("outcome")(classmethod(_validator_for(INVESTIGATION_OUTCOMES, "investigation_outcome")))
    _validate_state = field_validator("state")(classmethod(_validator_for(EXECUTION_STATES, "execution_state")))


class InvestigationSummary(BaseModel):
    summary_id: str
    total_investigations: int = 0
    completed_count: int = 0
    blocked_count: int = 0
    failed_count: int = 0
    cancelled_count: int = 0
    paused_count: int = 0
    narrative: str = ""


class StopReport(BaseModel):
    """The full answer to "why did autonomous investigation stop, and what
    should happen next" — richer than the bare `stop_reason` string alone,
    per the Autonomous-Execution follow-up's explicit reporting requirement."""

    stop_reason: str = "none"
    active_scenario_id: Optional[str] = None
    last_completed_investigation_id: Optional[str] = None
    last_completed_step_id: Optional[str] = None
    blocked_candidate_counts: dict[str, int] = Field(default_factory=dict)
    unexecuted_scenario_count: int = 0
    recommended_next_action: str = ""

    _validate_stop_reason = field_validator("stop_reason")(classmethod(_validator_for(STOP_REASONS, "stop_reason")))


class InvestigationStatistics(BaseModel):
    total_investigations: int = 0
    investigations_by_outcome: dict[str, int] = Field(default_factory=dict)
    total_steps_executed: int = 0
    total_assertions_evaluated: int = 0
    supported_assertion_count: int = 0
    contradicted_assertion_count: int = 0
    inconclusive_assertion_count: int = 0
    total_recovery_attempts: int = 0
    stop_reason: str = "none"

    _validate_stop_reason = field_validator("stop_reason")(classmethod(_validator_for(STOP_REASONS, "stop_reason")))


# ---------------------------------------------------------------------------
# Active (in-progress) investigation state -- the ONE mutable working
# record; everything else in this module is an immutable result record.
# ---------------------------------------------------------------------------


class ActiveInvestigation(BaseModel):
    investigation_id: str
    candidate_id: str
    scenario_id: str
    goal_id: str = ""
    attempt: int = 1
    state: str = "idle"
    current_step_index: int = 0
    total_steps: int = 0
    retry_counts: dict[str, int] = Field(default_factory=dict)
    execution_trace: list[ExecutionTrace] = Field(default_factory=list)
    assertion_results: list[AssertionResult] = Field(default_factory=list)
    assertions_evaluated: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    consecutive_step_failures: int = 0
    pending_step_id: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    graph_version_at_start: int = 0

    _validate_state = field_validator("state")(classmethod(_validator_for(EXECUTION_STATES, "execution_state")))

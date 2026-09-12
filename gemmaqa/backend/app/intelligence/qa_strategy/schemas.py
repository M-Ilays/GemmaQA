"""QA Strategy Engine schemas — application-neutral by construction.

No field here may name a business role, entity, workflow, KPI, or module.
`queue_type`/`recommended_action`/`policy_id`/etc. are small, CLOSED,
application-neutral vocabularies — the same shape choice made in
`goal_generation/schemas.py` and `scenario_planning/schemas.py`.

Determinism discipline (carried forward from Scenario Planning, itself a
fix for a real bug found in Goal Generation — see docs/GOAL_GENERATION_
ENGINE.md and docs/SCENARIO_PLANNING_ENGINE.md): NO field here defaults to
a random id. Every id is either required or defaults to "" for the
constructing code to fill in deterministically from the scenario/goal id
it derives from.

This package only DECIDES which scenarios should execute, in what order,
and why. It never executes a browser action, never calls BrowserAdapter/
ActionExecutor, never switches actors, and never mutates application
state — see `__init__.py` for the full scope statement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

QUEUE_TYPES = frozenset(
    {"immediate", "deferred", "blocked", "read_only", "mutation", "cross_actor", "cleanup", "exploration", "regression", "unknown"}
)

RECOMMENDED_ACTIONS = frozenset({"execute", "defer", "block", "skip"})

BATCH_TYPES = frozenset({"actor_workflow", "actor_entity", "actor_output", "actor", "ungrouped"})

EXECUTION_DEPENDENCY_TYPES = frozenset(
    {"scenario_prerequisite", "goal_prerequisite", "cleanup_ordering", "workflow_ordering", "state_ordering", "actor_ordering"}
)

EXECUTION_CONFLICT_TYPES = frozenset(
    {"actor_scope_conflict", "batch_scope_conflict", "resource_contention", "duplicate_candidate", "ordering_contradiction", "unknown"}
)
CONFLICT_STATUSES = frozenset({"open", "acknowledged", "resolved"})

FORECAST_TYPES = frozenset({"coverage", "confidence", "risk"})

POLICY_TYPES = frozenset(
    {
        "fastest_first", "highest_value_first", "lowest_risk_first", "read_only_first", "coverage_first",
        "confidence_first", "business_critical_first", "dependency_first", "balanced", "custom_weighted",
    }
)

RISK_CLASSES = frozenset({"read_only", "low", "moderate", "high", "prohibited", "unknown"})
COMPLEXITY_CLASSES = frozenset({"trivial", "simple", "moderate", "complex", "very_complex", "unknown"})
FEASIBILITY_STATUSES = frozenset({"feasible", "conditionally_feasible", "blocked", "incomplete", "unknown"})


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _validator_for(allowed: frozenset, label: str):
    def _validate(cls, value: str) -> str:
        if value and value not in allowed:
            raise ValueError(f"Invalid {label}: {value!r} (expected one of {sorted(allowed)})")
        return value

    return _validate


# ---------------------------------------------------------------------------
# Candidate
# ---------------------------------------------------------------------------


class ExecutionCandidate(BaseModel):
    candidate_id: str
    scenario_id: str
    goal_id: str
    scenario_type: str = ""
    priority_score: float = 0.0
    business_value: float = 0.0
    risk_score: float = 0.0
    coverage_gain: float = 0.0
    confidence_gain: float = 0.0
    knowledge_gain: float = 0.0
    risk_reduction_value: float = 0.0
    workflow_centrality: float = 0.0
    blocking_impact: float = 0.0
    feasibility_status: str = "unknown"
    risk_class: str = "unknown"
    recommended_action: str = "defer"
    queue_type: str = "unknown"
    batch_id: Optional[str] = None
    depends_on_candidate_ids: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    primary_actor: str = ""
    primary_workflow: str = ""
    primary_entity: str = ""
    primary_output: str = ""
    required_actors: list[str] = Field(default_factory=list)
    required_cleanup: bool = False
    required_data: bool = False
    estimated_duration_class: str = "unknown"
    explanation: str = ""
    priority_breakdown: dict[str, float] = Field(default_factory=dict)
    applied_penalties: list[str] = Field(default_factory=list)
    graph_version: int = 0
    goal_version: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    observation_count: int = 1

    _validate_feasibility = field_validator("feasibility_status")(classmethod(_validator_for(FEASIBILITY_STATUSES, "feasibility_status")))
    _validate_risk_class = field_validator("risk_class")(classmethod(_validator_for(RISK_CLASSES, "risk_class")))
    _validate_action = field_validator("recommended_action")(classmethod(_validator_for(RECOMMENDED_ACTIONS, "recommended_action")))
    _validate_queue = field_validator("queue_type")(classmethod(_validator_for(QUEUE_TYPES, "queue_type")))
    _validate_duration = field_validator("estimated_duration_class")(classmethod(_validator_for(COMPLEXITY_CLASSES, "complexity_class")))

    @field_validator("priority_score", "business_value", "risk_score", "coverage_gain", "confidence_gain", "knowledge_gain", "risk_reduction_value", "workflow_centrality", "blocking_impact")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Queues / batches
# ---------------------------------------------------------------------------


class ExecutionQueue(BaseModel):
    queue_id: str
    queue_type: str
    candidate_ids: list[str] = Field(default_factory=list)
    description: str = ""

    _validate_queue_type = field_validator("queue_type")(classmethod(_validator_for(QUEUE_TYPES, "queue_type")))


class ExecutionBatch(BaseModel):
    batch_id: str
    batch_type: str
    batch_key: str = ""
    candidate_ids: list[str] = Field(default_factory=list)
    primary_actor: str = ""
    estimated_actor_switches: int = 0

    _validate_batch_type = field_validator("batch_type")(classmethod(_validator_for(BATCH_TYPES, "batch_type")))


# ---------------------------------------------------------------------------
# Dependencies / conflicts
# ---------------------------------------------------------------------------


class ExecutionDependency(BaseModel):
    dependency_id: str
    candidate_id: str
    required_candidate_id: str
    dependency_type: str = "unknown"
    reason: str = ""
    blocking: bool = True
    confidence: float = 0.0

    _validate_type = field_validator("dependency_type")(classmethod(_validator_for(EXECUTION_DEPENDENCY_TYPES, "dependency_type")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ExecutionConflict(BaseModel):
    conflict_id: str
    candidate_ids: list[str] = Field(default_factory=list)
    conflict_type: str = "unknown"
    description: str = ""
    severity: str = "low"
    status: str = "open"

    _validate_type = field_validator("conflict_type")(classmethod(_validator_for(EXECUTION_CONFLICT_TYPES, "conflict_type")))
    _validate_status = field_validator("status")(classmethod(_validator_for(CONFLICT_STATUSES, "conflict status")))


# ---------------------------------------------------------------------------
# Recommendations / forecasts / ordering
# ---------------------------------------------------------------------------


class ExecutionRecommendation(BaseModel):
    recommendation_id: str
    candidate_id: str
    decision: str = "defer"
    reasons: list[str] = Field(default_factory=list)
    expected_gain: float = 0.0
    expected_coverage: float = 0.0
    expected_confidence: float = 0.0
    expected_risk: float = 0.0
    estimated_duration_class: str = "unknown"
    required_actors: list[str] = Field(default_factory=list)
    required_cleanup: bool = False
    required_data: bool = False

    _validate_decision = field_validator("decision")(classmethod(_validator_for(RECOMMENDED_ACTIONS, "recommended_action")))
    _validate_duration = field_validator("estimated_duration_class")(classmethod(_validator_for(COMPLEXITY_CLASSES, "complexity_class")))

    @field_validator("expected_gain", "expected_coverage", "expected_confidence", "expected_risk")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return _clamp(value)


class ExecutionForecast(BaseModel):
    forecast_id: str
    forecast_type: str
    baseline_value: float = 0.0
    projected_value: float = 0.0
    projected_delta: float = 0.0
    basis: str = ""
    confidence_in_forecast: float = 0.3
    explanation: str = ""

    _validate_type = field_validator("forecast_type")(classmethod(_validator_for(FORECAST_TYPES, "forecast_type")))

    @field_validator("baseline_value", "projected_value", "confidence_in_forecast")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return _clamp(value)


class ExecutionOrdering(BaseModel):
    """Also serves as the "Execution Timeline" the task's OUTPUT section
    names -- an ordered sequence IS a timeline; a separate near-duplicate
    schema would add nothing but bookkeeping."""

    ordering_id: str
    scope: str = "global"  # "global" | "queue:{queue_type}" | "batch:{batch_id}"
    ordered_candidate_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


# ---------------------------------------------------------------------------
# Policy / strategy / statistics / summary / result
# ---------------------------------------------------------------------------


class ExecutionPolicy(BaseModel):
    policy_id: str
    description: str = ""
    weight_overrides: dict[str, float] = Field(default_factory=dict)

    _validate_policy_id = field_validator("policy_id")(classmethod(_validator_for(POLICY_TYPES, "policy_id")))


class ExecutionStrategy(BaseModel):
    """The applied configuration for ONE strategy-generation pass: which
    policy was used and pointers to the queues/batches/ordering it
    produced -- distinct from `StrategyResult`, which embeds the full
    expanded content."""

    strategy_id: str
    policy: ExecutionPolicy
    queue_ids: list[str] = Field(default_factory=list)
    batch_ids: list[str] = Field(default_factory=list)
    ordering_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ExecutionStatistics(BaseModel):
    total_candidates: int = 0
    candidates_by_queue: dict[str, int] = Field(default_factory=dict)
    candidates_by_action: dict[str, int] = Field(default_factory=dict)
    candidates_by_batch_type: dict[str, int] = Field(default_factory=dict)
    batch_count: int = 0
    dependency_count: int = 0
    conflict_count: int = 0
    average_priority: float = 0.0
    average_risk: float = 0.0
    policy_used: str = ""
    graph_version: int = 0
    strategy_version: int = 0


class StrategySummary(BaseModel):
    summary_id: str
    top_candidate_ids: list[str] = Field(default_factory=list)
    total_candidates: int = 0
    narrative: str = ""
    policy_used: str = ""


class StrategyResult(BaseModel):
    result_id: str = ""
    strategy: Optional[ExecutionStrategy] = None
    candidates: list[ExecutionCandidate] = Field(default_factory=list)
    queues: list[ExecutionQueue] = Field(default_factory=list)
    batches: list[ExecutionBatch] = Field(default_factory=list)
    dependencies: list[ExecutionDependency] = Field(default_factory=list)
    conflicts: list[ExecutionConflict] = Field(default_factory=list)
    recommendations: list[ExecutionRecommendation] = Field(default_factory=list)
    forecasts: list[ExecutionForecast] = Field(default_factory=list)
    ordering: Optional[ExecutionOrdering] = None
    statistics: ExecutionStatistics = Field(default_factory=ExecutionStatistics)
    summary: Optional[StrategySummary] = None
    strategy_version: int = 0
    graph_version: int = 0
    generated_at: datetime = Field(default_factory=datetime.utcnow)

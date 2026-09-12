"""Goal Generation Engine schemas — application-neutral by construction.

No field here may name a business role, entity, workflow, KPI, or module.
`goal_type` values are a small, CLOSED, application-neutral vocabulary
(unlike the Knowledge Graph's deliberately open node/edge types) — the
commissioning task explicitly forbids inventing application-specific goal
categories, so a fixed enum is the right shape here, not an open string.

This package only DECIDES WHAT to investigate next. It never executes a
browser action, never plans a scenario, never selects a QA strategy, and
never mutates application state — see `__init__.py` for the full scope
statement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.ids import new_id

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

GOAL_TYPES = frozenset(
    {
        "verify_workflow",
        "verify_dependency",
        "verify_kpi",
        "verify_permission",
        "verify_actor_capability",
        "verify_state_transition",
        "verify_entity_lifecycle",
        "verify_business_rule",
        "resolve_contradiction",
        "resolve_graph_gap",
        "resolve_unresolved_reference",
        "increase_confidence",
        "validate_inferred_relationship",
        "validate_workflow_branch",
        "validate_prerequisite",
        "validate_dashboard_output",
        "validate_report",
        "validate_notification",
        "validate_aggregation_rule",
        "validate_actor_hand_off",
        "validate_ownership",
        "validate_scope",
    }
)

GOAL_STATUSES = frozenset({"pending", "blocked", "completed", "superseded", "dismissed"})

GOAL_GROUP_TYPES = frozenset(
    {"entity", "workflow", "actor", "business_process", "output", "module", "graph_region", "contradiction", "gap"}
)

GOAL_EVIDENCE_TYPES = frozenset(
    {
        "gap", "contradiction", "consistency_issue", "low_confidence_edge", "low_confidence_node",
        "inferred_edge", "unresolved_reference", "unverified_node",
    }
)

ESTIMATED_COMPLEXITY_LEVELS = frozenset({"low", "medium", "high"})


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class GoalEvidence(BaseModel):
    """A pointer to ONE piece of graph-level evidence motivating a goal --
    never a copy of the underlying raw evidence payload (that already lives,
    referenced the same way, on the Knowledge Graph's own nodes/edges).

    `evidence_id` defaults to a DETERMINISTIC key derived from
    (evidence_type, source_id) rather than a random id -- goal generation
    must be idempotent (task: "the same graph should always produce the
    same goals"), and a random id embedded inside every goal's
    `supporting_evidence` would make byte-for-byte content comparison
    across passes impossible."""

    evidence_id: str = ""
    evidence_type: str
    source_id: str = ""
    description: str = ""
    confidence: float = 0.0

    @model_validator(mode="after")
    def _default_evidence_id(self) -> "GoalEvidence":
        if not self.evidence_id:
            self.evidence_id = f"evidence:{self.evidence_type}:{self.source_id}"
        return self

    @field_validator("evidence_type")
    @classmethod
    def _validate_evidence_type(cls, value: str) -> str:
        if value not in GOAL_EVIDENCE_TYPES:
            raise ValueError(f"Invalid evidence_type: {value!r} (expected one of {sorted(GOAL_EVIDENCE_TYPES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Priority / explanation
# ---------------------------------------------------------------------------


class GoalPriority(BaseModel):
    """The full, explainable breakdown behind one goal's `priority_score`.

    Every component is stored, not just the final number -- "explain every
    score" (task section on the priority model) means a caller can always
    answer "why did this goal rank where it did" without re-deriving
    anything."""

    goal_id: str = ""
    business_value: float = 0.0
    risk: float = 0.0
    knowledge_gain: float = 0.0
    coverage_improvement: float = 0.0
    dependency_impact: float = 0.0
    graph_centrality: float = 0.0
    blocking_severity: float = 0.0
    confidence_gap: float = 0.0
    weighted_score: float = 0.0
    penalty_multiplier: float = 1.0
    applied_penalties: list[str] = Field(default_factory=list)
    final_score: float = 0.0
    explanation: str = ""

    @field_validator(
        "business_value", "risk", "knowledge_gain", "coverage_improvement", "dependency_impact",
        "graph_centrality", "blocking_severity", "confidence_gap", "weighted_score", "final_score",
    )
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return _clamp(value)


class GoalExplanation(BaseModel):
    """A human-readable explanation trail for one goal -- the "why" a future
    Scenario Planning Engine (or a human reviewer) can read without
    re-deriving it from the raw graph."""

    explanation_id: str = Field(default_factory=new_id)
    goal_id: str = ""
    summary: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    recommended_investigation: str = ""

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Candidate (pre-dedup, pre-priority draft produced by the candidate builder)
# ---------------------------------------------------------------------------


class GoalCandidate(BaseModel):
    """A draft goal, before deduplication/priority/explanation/dependency/
    grouping are applied. `subject_key` is the deterministic identity a
    candidate is merged on -- two candidates with the same `subject_key`
    represent the SAME investigation and must be merged, never both kept."""

    subject_key: str
    goal_type: str
    title: str
    description: str = ""
    required_entities: list[str] = Field(default_factory=list)
    required_actors: list[str] = Field(default_factory=list)
    required_workflows: list[str] = Field(default_factory=list)
    required_outputs: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    required_states: list[str] = Field(default_factory=list)
    required_context: list[str] = Field(default_factory=list)
    blocking_gaps: list[str] = Field(default_factory=list)
    supporting_evidence: list[GoalEvidence] = Field(default_factory=list)
    supporting_graph_nodes: list[str] = Field(default_factory=list)
    supporting_graph_edges: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    source_gap_ids: list[str] = Field(default_factory=list)
    source_contradiction_ids: list[str] = Field(default_factory=list)
    source_consistency_issue_ids: list[str] = Field(default_factory=list)
    source_inference_rule_ids: list[str] = Field(default_factory=list)
    source_reference_ids: list[str] = Field(default_factory=list)
    source_confidence: float = 0.0
    already_verified: bool = False
    estimated_workflow_depth: int = 0
    estimated_actor_count: int = 0
    estimated_browser_actions: int = 1

    @field_validator("goal_type")
    @classmethod
    def _validate_goal_type(cls, value: str) -> str:
        if value not in GOAL_TYPES:
            raise ValueError(f"Invalid goal_type: {value!r} (expected one of {sorted(GOAL_TYPES)})")
        return value

    @field_validator("source_confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Investigation goal (the durable, queryable record)
# ---------------------------------------------------------------------------


class InvestigationGoal(BaseModel):
    goal_id: str
    goal_type: str
    title: str
    description: str = ""
    priority_score: float = 0.0
    business_value: float = 0.0
    risk_score: float = 0.0
    coverage_value: float = 0.0
    confidence: float = 0.0
    required_entities: list[str] = Field(default_factory=list)
    required_actors: list[str] = Field(default_factory=list)
    required_workflows: list[str] = Field(default_factory=list)
    required_outputs: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    required_states: list[str] = Field(default_factory=list)
    required_context: list[str] = Field(default_factory=list)
    blocking_gaps: list[str] = Field(default_factory=list)
    supporting_evidence: list[GoalEvidence] = Field(default_factory=list)
    supporting_graph_nodes: list[str] = Field(default_factory=list)
    supporting_graph_edges: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    # Which OTHER goal_type should be tackled first when this goal is
    # blocked by a dependency (e.g. a verify_kpi goal recommends
    # verify_workflow when its workflow dependency isn't yet satisfied).
    # Equal to `goal_type` itself when nothing blocks direct investigation.
    recommended_goal_type: str = ""
    estimated_complexity: str = "low"
    estimated_actor_count: int = 0
    estimated_workflow_depth: int = 0
    estimated_browser_actions: int = 1
    goal_status: str = "pending"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    observation_count: int = 1
    graph_version: int = 0
    group_id: Optional[str] = None
    depends_on_goal_ids: list[str] = Field(default_factory=list)
    priority: Optional[GoalPriority] = None
    explanation: Optional[GoalExplanation] = None
    # bookkeeping mirrored from the candidate that produced this goal --
    # needed so a later pass can re-detect "this is still the same
    # investigation" without re-deriving it from scratch.
    source_gap_ids: list[str] = Field(default_factory=list)
    source_contradiction_ids: list[str] = Field(default_factory=list)
    source_consistency_issue_ids: list[str] = Field(default_factory=list)
    source_inference_rule_ids: list[str] = Field(default_factory=list)
    source_reference_ids: list[str] = Field(default_factory=list)

    @field_validator("goal_type", "recommended_goal_type")
    @classmethod
    def _validate_goal_type(cls, value: str) -> str:
        if value and value not in GOAL_TYPES:
            raise ValueError(f"Invalid goal_type: {value!r} (expected one of {sorted(GOAL_TYPES)})")
        return value

    @field_validator("goal_status")
    @classmethod
    def _validate_goal_status(cls, value: str) -> str:
        if value not in GOAL_STATUSES:
            raise ValueError(f"Invalid goal_status: {value!r} (expected one of {sorted(GOAL_STATUSES)})")
        return value

    @field_validator("estimated_complexity")
    @classmethod
    def _validate_complexity(cls, value: str) -> str:
        if value not in ESTIMATED_COMPLEXITY_LEVELS:
            raise ValueError(f"Invalid estimated_complexity: {value!r} (expected one of {sorted(ESTIMATED_COMPLEXITY_LEVELS)})")
        return value

    @field_validator("priority_score", "business_value", "risk_score", "coverage_value", "confidence")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Dependencies / conflicts / grouping
# ---------------------------------------------------------------------------


class GoalDependency(BaseModel):
    """A represented (never executed) prerequisite relationship between two
    goals -- e.g. "verify_kpi depends on verify_workflow"."""

    dependency_id: str = Field(default_factory=new_id)
    goal_id: str
    depends_on_goal_id: str
    reason: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class GoalConflict(BaseModel):
    conflict_id: str = Field(default_factory=new_id)
    goal_ids: list[str] = Field(default_factory=list)
    conflict_type: str = "duplicate"
    reason: str = ""
    resolution: str = "merged"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class GoalGroup(BaseModel):
    group_id: str
    group_type: str
    group_key: str = ""
    title: str = ""
    goal_ids: list[str] = Field(default_factory=list)
    importance: float = 0.0
    coverage: float = 0.0
    risk: float = 0.0
    goal_count: int = 0

    @field_validator("group_type")
    @classmethod
    def _validate_group_type(cls, value: str) -> str:
        if value not in GOAL_GROUP_TYPES:
            raise ValueError(f"Invalid group_type: {value!r} (expected one of {sorted(GOAL_GROUP_TYPES)})")
        return value

    @field_validator("importance", "coverage", "risk")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Statistics / result
# ---------------------------------------------------------------------------


class GoalStatistics(BaseModel):
    total_goals: int = 0
    goals_by_type: dict[str, int] = Field(default_factory=dict)
    goals_by_status: dict[str, int] = Field(default_factory=dict)
    average_priority: float = 0.0
    high_priority_count: int = 0
    blocked_count: int = 0
    completed_count: int = 0
    dismissed_count: int = 0
    group_count: int = 0
    dependency_count: int = 0
    conflict_count: int = 0
    graph_version: int = 0


class GoalGenerationResult(BaseModel):
    result_id: str = Field(default_factory=new_id)
    goals: list[InvestigationGoal] = Field(default_factory=list)
    groups: list[GoalGroup] = Field(default_factory=list)
    dependencies: list[GoalDependency] = Field(default_factory=list)
    conflicts: list[GoalConflict] = Field(default_factory=list)
    statistics: GoalStatistics = Field(default_factory=GoalStatistics)
    stop_conditions: list[str] = Field(default_factory=list)
    graph_version: int = 0
    generation_count: int = 0
    generated_at: datetime = Field(default_factory=datetime.utcnow)

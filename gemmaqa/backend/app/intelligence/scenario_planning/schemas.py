"""Scenario Planning Engine schemas — application-neutral by construction.

No field here may name a business role, entity, workflow, KPI, or module.
`scenario_type`/`step_type`/etc. are small, CLOSED, application-neutral
vocabularies (the same shape choice made in `goal_generation/schemas.py`
and for the same reason: this engine must never invent an
application-specific category).

IMPORTANT — determinism: unlike the Knowledge Graph's evidence objects,
NO field in this package defaults to a random id (`new_id()`). A random
id embedded in a nested object silently breaks idempotent regeneration
(discovered as a real bug in the Goal Generation milestone — see
`docs/GOAL_GENERATION_ENGINE.md`'s "Live verification finding"). Every id
here is either required (the caller must supply a deterministic string)
or defaults to "" for the constructing code to fill in deterministically.

This package only decides HOW a goal COULD be investigated. It never
executes a browser action, never invokes BrowserAdapter/ActionExecutor,
never switches actors, and never mutates application state — see
`__init__.py` for the full scope statement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

SCENARIO_TYPES = frozenset(
    {
        "workflow_verification",
        "dependency_verification",
        "output_verification",
        "metric_verification",
        "permission_positive_verification",
        "permission_negative_verification",
        "actor_capability_verification",
        "state_transition_verification",
        "entity_lifecycle_verification",
        # Not in the task's illustrative list verbatim, but required to give
        # `verify_business_rule` goals (Goal Generation's own closed
        # vocabulary) a non-forced home rather than lumping them into
        # `entity_lifecycle_verification` or `unknown`.
        "business_rule_verification",
        "prerequisite_verification",
        "branch_verification",
        "actor_handoff_verification",
        "ownership_verification",
        "scope_verification",
        "aggregation_verification",
        "notification_verification",
        "report_verification",
        "contradiction_resolution",
        "graph_gap_resolution",
        "inferred_relationship_validation",
        "confidence_increase",
        "unresolved_reference_resolution",
        "stale_knowledge_refresh",
        "exploratory_observation",
        "unknown",
    }
)

# Statuses THIS engine may itself assign are ENGINE_PRODUCIBLE_STATUSES,
# below. `selected`/`rejected`/`completed`/`failed`/`inconclusive` plus the
# newer execution-lifecycle values are assigned by
# `app.intelligence.autonomous_investigation.AutonomousInvestigationEngine`
# once a scenario is actually driven through the runtime Planner — this
# package never sets them itself, but they now ARE actively written
# (in-place, on the same `InvestigationScenario` object this engine's own
# `ScenarioMemory` holds) rather than sitting unused as placeholders.
SCENARIO_STATUSES = frozenset(
    {
        "draft", "feasible", "conditionally_feasible", "blocked", "incomplete",
        "superseded", "selected", "rejected", "completed", "failed", "inconclusive", "stale",
        # Execution-lifecycle values: never set by THIS engine (see
        # ENGINE_PRODUCIBLE_STATUSES) — owned by
        # app.intelligence.autonomous_investigation.AutonomousInvestigationEngine,
        # the only package authorised to actually drive a scenario. Extends
        # (never duplicates) the vocabulary above: "selected"/"completed"/
        # "failed"/"inconclusive"/"stale" already existed as reserved
        # placeholders; this adds the remaining execution states a scenario
        # can be tracked through end to end.
        "queued", "validating", "executing", "verifying", "passed", "contradicted",
        "skipped", "cleanup_pending", "cleaned_up",
    }
)
# Statuses THIS engine (Scenario Planning) is allowed to assign directly.
# Everything else in SCENARIO_STATUSES — selected/rejected/completed/failed/
# inconclusive/queued/validating/executing/verifying/passed/contradicted/
# skipped/cleanup_pending/cleaned_up — is written exclusively by
# AutonomousInvestigationEngine (via a direct, in-place `scenario.status =`
# mutation on the SAME object this engine's own registry holds — see
# `autonomous_investigation_engine.py::_finalize`), never re-derived here.
ENGINE_PRODUCIBLE_STATUSES = frozenset({"draft", "feasible", "conditionally_feasible", "blocked", "incomplete", "superseded", "stale"})

STEP_TYPES = frozenset(
    {
        "establish_session", "authenticate_actor", "navigate", "locate_subject", "establish_baseline",
        "observe", "prepare_data", "perform_operation", "perform_workflow_step", "trigger_transition",
        "verify_state", "verify_permission", "verify_denial", "observe_output", "compare",
        "capture_evidence", "switch_actor", "restore_context", "cleanup", "rollback",
        "wait_for_effect", "resolve_scope", "unknown",
    }
)

REQUIREMENT_STATUSES = frozenset({"satisfied", "satisfiable", "unresolved", "contradicted", "blocked", "unavailable", "stale", "unknown"})

RISK_CLASSES = frozenset({"read_only", "low", "moderate", "high", "prohibited", "unknown"})

COMPLEXITY_CLASSES = frozenset({"trivial", "simple", "moderate", "complex", "very_complex", "unknown"})

FEASIBILITY_STATUSES = frozenset({"feasible", "conditionally_feasible", "blocked", "incomplete", "unknown"})

CHECKPOINT_TYPES = frozenset(
    {
        "initial_context", "authentication_confirmed", "baseline_captured", "prerequisite_confirmed",
        "entity_located", "pre_transition_state", "post_transition_state", "output_before", "output_after",
        "comparison_ready", "evidence_complete", "cleanup_complete",
    }
)

COMPARISON_OPERATORS = frozenset(
    {
        "equals", "not_equals", "increases", "decreases", "contains", "excludes", "appears",
        "disappears", "changes_to", "remains_unchanged", "permission_allowed", "permission_denied", "unknown",
    }
)

SCENARIO_EVIDENCE_TYPES = frozenset(
    {
        "page_state", "visible_text", "form_state", "table_row", "metric_value", "counter_value",
        "badge_value", "chart_value", "report_row", "queue_membership", "notification", "api_status",
        "api_response_summary", "url_transition", "actor_identity", "permission_denial", "entity_state",
        "timestamp", "screenshot_reference", "network_request_summary", "workflow_trace",
    }
)

EVIDENCE_ROLES = frozenset({"sufficient", "supporting", "optional", "contradictory", "inconclusive_if_missing"})

SCENARIO_GAP_TYPES = frozenset(
    {
        "missing_actor", "missing_actor_session", "missing_permission", "permission_contradiction",
        "missing_entity", "missing_entity_state", "missing_workflow", "incomplete_workflow",
        "missing_workflow_step", "missing_transition", "missing_prerequisite", "unsatisfied_prerequisite",
        "missing_output", "missing_output_location", "missing_scope", "incompatible_scope",
        "missing_test_data", "unknown_data_constraints", "missing_baseline_method", "missing_observation_method",
        "missing_comparison_rule", "missing_evidence_requirement", "missing_cleanup_path", "irreversible_mutation",
        "unresolved_actor_handoff", "unresolved_reference", "stale_graph_context", "contradictory_graph_context",
        "unsafe_scenario", "unsupported_goal_type", "insufficient_information", "unknown",
    }
)

BRANCH_TYPES = frozenset(
    {
        "approval_rejection", "success_failure", "permission_allow_deny", "immediate_vs_delayed",
        "entity_found_vs_missing", "actor_session_availability", "prerequisite_satisfied_vs_blocked", "unknown",
    }
)

MUTATION_TYPES = frozenset({"none", "create", "update", "delete", "transition", "unknown"})
REVERSIBILITY_LEVELS = frozenset({"reversible", "conditionally_reversible", "irreversible", "unknown"})

CONFLICT_TYPES = frozenset(
    {
        "permission_contradiction", "state_conflict", "incompatible_scope", "cleanup_permission_unavailable",
        "mixed_mutation_readonly", "branch_contradicts_workflow", "scope_mismatch", "stale_reference",
        "actor_result_incompatibility", "delay_assumption_conflict", "transition_contradicted",
        "duplicate_alternative", "unknown",
    }
)
CONFLICT_STATUSES = frozenset({"open", "acknowledged", "resolved"})

DEPENDENCY_TYPES = frozenset({"scenario_prerequisite", "goal_dependency_projection", "actor_session_dependency", "data_preparation_dependency", "unknown"})

PRECONDITION_TYPES = frozenset(
    {
        "actor_session", "entity_state", "permission", "module_access", "prerequisite_workflow",
        "feature_availability", "scope", "test_data", "output_baseline", "environment_stability",
    }
)
POSTCONDITION_TYPES = frozenset(
    {"entity_state", "output_state", "workflow_outcome", "notification", "permission_result", "evidence_captured", "cleanup_state"}
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _validator_for(allowed: frozenset, label: str):
    def _validate(cls, value: str) -> str:
        if value and value not in allowed:
            raise ValueError(f"Invalid {label}: {value!r} (expected one of {sorted(allowed)})")
        return value

    return _validate


# ---------------------------------------------------------------------------
# Provenance / evidence references
# ---------------------------------------------------------------------------


class ScenarioProvenance(BaseModel):
    source_registry: str = ""
    source_record_id: str = ""
    synchronized_at_iteration: int = 0
    synchronized_at: datetime = Field(default_factory=datetime.utcnow)


class ScenarioEvidenceReference(BaseModel):
    """A pointer to ONE piece of upstream evidence (a graph node/edge, a
    goal's own evidence, a gap, a contradiction) -- never a copy of a raw
    evidence payload."""

    reference_id: str = ""
    reference_type: str = ""
    source_id: str = ""
    description: str = ""
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------


class _RequirementBase(BaseModel):
    requirement_id: str
    requirement_type: str
    referenced_object_id: str = ""
    status: str = "unknown"
    confidence: float = 0.0
    mandatory: bool = True
    resolved: bool = False
    resolution_source: str = ""
    missing_reason: str = ""
    alternatives: list[str] = Field(default_factory=list)
    supporting_graph_nodes: list[str] = Field(default_factory=list)
    supporting_graph_edges: list[str] = Field(default_factory=list)

    _validate_status = field_validator("status")(classmethod(_validator_for(REQUIREMENT_STATUSES, "requirement status")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ScenarioActorRequirement(_RequirementBase):
    requirement_type: str = "actor"
    actor_id: str = ""
    canonical_name: str = ""
    required_workflow_steps: list[str] = Field(default_factory=list)
    required_operations: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    session_required: bool = True
    authentication_method: Optional[str] = None
    role_switch_required: bool = False
    current_availability: str = "unknown"
    positive_permission_evidence: list[ScenarioEvidenceReference] = Field(default_factory=list)
    negative_permission_evidence: list[ScenarioEvidenceReference] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)


class ScenarioPermissionRequirement(_RequirementBase):
    requirement_type: str = "permission"
    permission_id: str = ""
    operation_id: str = ""
    denial_expected: bool = False
    positive_evidence: list[ScenarioEvidenceReference] = Field(default_factory=list)
    negative_evidence: list[ScenarioEvidenceReference] = Field(default_factory=list)


class ScenarioEntityRequirement(_RequirementBase):
    requirement_type: str = "entity"
    entity_id: str = ""
    canonical_name: str = ""
    required_relationships: list[str] = Field(default_factory=list)


class ScenarioStateRequirement(_RequirementBase):
    requirement_type: str = "state"
    entity_id: str = ""
    state_label: str = ""
    role: str = "source"  # source | target
    transition_id: str = ""


class ScenarioWorkflowRequirement(_RequirementBase):
    requirement_type: str = "workflow"
    workflow_id: str = ""
    canonical_name: str = ""
    required_steps: list[str] = Field(default_factory=list)
    required_outcome: str = ""


class ScenarioOutputRequirement(_RequirementBase):
    requirement_type: str = "output"
    output_id: str = ""
    output_type: str = ""
    scope: str = ""


class ScenarioDataRequirement(_RequirementBase):
    requirement_type: str = "data"
    entity_type: str = ""
    field_purpose: str = ""
    required_state: str = ""
    required_uniqueness: bool = False
    required_scope: str = ""
    ownership_requirement: str = ""
    actor_relationship: str = ""
    source_options: list[str] = Field(default_factory=list)
    mutation_requirement: bool = False
    cleanup_requirement: bool = False
    sensitivity: str = "none"  # none | test_only | sensitive
    constraints: list[str] = Field(default_factory=list)
    known_valid_source: Optional[str] = None
    known_reusable_fixture: Optional[str] = None
    generation_policy: str = "unknown"  # reuse_existing | synthetic_test_data | unknown


# ---------------------------------------------------------------------------
# Pre/postconditions, checkpoints, observations, assertions, comparisons
# ---------------------------------------------------------------------------


class ScenarioPrecondition(BaseModel):
    precondition_id: str
    precondition_type: str
    description: str = ""
    referenced_object_id: str = ""
    status: str = "unknown"
    mandatory: bool = True
    confidence: float = 0.0

    _validate_type = field_validator("precondition_type")(classmethod(_validator_for(PRECONDITION_TYPES, "precondition_type")))
    _validate_status = field_validator("status")(classmethod(_validator_for(REQUIREMENT_STATUSES, "requirement status")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ScenarioPostcondition(BaseModel):
    postcondition_id: str
    postcondition_type: str
    description: str = ""
    referenced_object_id: str = ""
    expected_value: str = ""
    confidence: float = 0.0

    _validate_type = field_validator("postcondition_type")(classmethod(_validator_for(POSTCONDITION_TYPES, "postcondition_type")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ScenarioCheckpoint(BaseModel):
    checkpoint_id: str
    scenario_id: str = ""
    sequence_index: int = 0
    checkpoint_type: str = "initial_context"
    title: str = ""
    required_observations: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    pass_criteria: str = ""
    failure_criteria: str = ""
    inconclusive_criteria: str = ""
    actor_context: str = ""
    scope_context: str = ""
    entity_context: str = ""

    _validate_type = field_validator("checkpoint_type")(classmethod(_validator_for(CHECKPOINT_TYPES, "checkpoint_type")))


class ScenarioObservation(BaseModel):
    observation_id: str
    checkpoint_id: str = ""
    target_object_id: str = ""
    observation_type: str = ""
    expected_condition: str = ""
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ScenarioAssertion(BaseModel):
    assertion_id: str
    subject_id: str = ""
    assertion_type: str = ""
    expected_value: str = ""
    operator: str = "unknown"
    confidence: float = 0.0
    evidence_requirement_ids: list[str] = Field(default_factory=list)

    _validate_operator = field_validator("operator")(classmethod(_validator_for(COMPARISON_OPERATORS, "comparison operator")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ScenarioComparison(BaseModel):
    comparison_id: str
    subject_type: str = ""
    subject_id: str = ""
    baseline_checkpoint_id: str = ""
    final_checkpoint_id: str = ""
    comparison_operator: str = "unknown"
    expected_direction: str = ""
    expected_delta: Optional[str] = None
    tolerance: Optional[str] = None
    scope_requirements: list[str] = Field(default_factory=list)
    temporal_requirements: str = ""
    inconclusive_conditions: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)

    _validate_operator = field_validator("comparison_operator")(classmethod(_validator_for(COMPARISON_OPERATORS, "comparison operator")))


class ScenarioEvidenceRequirement(BaseModel):
    evidence_requirement_id: str
    evidence_type: str
    target_object_id: str = ""
    checkpoint_id: str = ""
    mandatory: bool = True
    expected_condition: str = ""
    scope: str = ""
    freshness_requirement: str = ""
    acceptable_alternatives: list[str] = Field(default_factory=list)
    sufficiency_weight: float = 0.5
    source_graph_relationship: str = ""
    evidence_role: str = "supporting"

    _validate_evidence_type = field_validator("evidence_type")(classmethod(_validator_for(SCENARIO_EVIDENCE_TYPES, "evidence_type")))
    _validate_role = field_validator("evidence_role")(classmethod(_validator_for(EVIDENCE_ROLES, "evidence_role")))

    @field_validator("sufficiency_weight")
    @classmethod
    def _clamp_weight(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Steps, branches
# ---------------------------------------------------------------------------


class ScenarioStep(BaseModel):
    step_id: str
    scenario_id: str = ""
    sequence_index: int = 0
    step_type: str = "unknown"
    title: str = ""
    description: str = ""
    semantic_action: str = ""
    actor_id: str = ""
    actor_requirement_id: str = ""
    entity_ids: list[str] = Field(default_factory=list)
    workflow_id: str = ""
    workflow_step_id: str = ""
    operation_id: str = ""
    permission_id: str = ""
    source_state_id: str = ""
    target_state_id: str = ""
    input_requirements: list[str] = Field(default_factory=list)
    expected_observations: list[str] = Field(default_factory=list)
    expected_state_change: str = ""
    expected_output_change: str = ""
    evidence_requirements: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    safety_class: str = "unknown"
    mutation_type: str = "none"
    reversibility: str = "unknown"
    optional: bool = False
    blocking: bool = True
    branch_id: Optional[str] = None
    depends_on_step_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    source_graph_nodes: list[str] = Field(default_factory=list)
    source_graph_edges: list[str] = Field(default_factory=list)
    provenance: list[ScenarioProvenance] = Field(default_factory=list)

    _validate_step_type = field_validator("step_type")(classmethod(_validator_for(STEP_TYPES, "step_type")))
    _validate_safety_class = field_validator("safety_class")(classmethod(_validator_for(RISK_CLASSES, "safety_class")))
    _validate_mutation_type = field_validator("mutation_type")(classmethod(_validator_for(MUTATION_TYPES, "mutation_type")))
    _validate_reversibility = field_validator("reversibility")(classmethod(_validator_for(REVERSIBILITY_LEVELS, "reversibility")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ScenarioBranch(BaseModel):
    branch_id: str
    source_step_id: str = ""
    condition: str = ""
    condition_source: str = ""
    confidence: float = 0.0
    branch_type: str = "unknown"
    target_step_ids: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    evidence_requirements: list[str] = Field(default_factory=list)
    terminal: bool = False
    fallback: bool = False
    unresolved: bool = False

    _validate_branch_type = field_validator("branch_type")(classmethod(_validator_for(BRANCH_TYPES, "branch_type")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Cleanup / rollback / risk / feasibility
# ---------------------------------------------------------------------------


class ScenarioCleanupPlan(BaseModel):
    cleanup_required: bool = False
    cleanup_steps: list[str] = Field(default_factory=list)
    cleanup_actor: str = ""
    cleanup_permissions: list[str] = Field(default_factory=list)
    cleanup_state: str = ""
    cleanup_evidence: list[str] = Field(default_factory=list)
    cleanup_feasibility: str = "unknown"
    unresolved_cleanup_gaps: list[str] = Field(default_factory=list)

    _validate_feasibility = field_validator("cleanup_feasibility")(classmethod(_validator_for(FEASIBILITY_STATUSES, "feasibility_status")))


class ScenarioRollbackPlan(BaseModel):
    rollback_possible: bool = False
    rollback_transition: str = ""
    rollback_actor: str = ""
    rollback_permissions: list[str] = Field(default_factory=list)
    rollback_risk: str = "unknown"
    irreversible_reason: str = ""
    fallback_containment: str = ""

    _validate_risk = field_validator("rollback_risk")(classmethod(_validator_for(RISK_CLASSES, "risk_class")))


class ScenarioRiskAssessment(BaseModel):
    risk_id: str = ""
    scenario_id: str = ""
    risk_class: str = "unknown"
    final_risk_score: float = 0.0
    # Named, explainable components -- see `scenario_risk_analyzer.py` for
    # the full dimension list (destructive mutation, irreversibility,
    # cross-actor, cleanup uncertainty, etc); a dict keeps this extensible
    # without a schema change per new risk dimension, while every component
    # remains individually inspectable (never collapsed silently).
    components: dict[str, float] = Field(default_factory=dict)
    applied_flags: list[str] = Field(default_factory=list)
    explanation: str = ""

    _validate_risk_class = field_validator("risk_class")(classmethod(_validator_for(RISK_CLASSES, "risk_class")))

    @field_validator("final_risk_score")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return _clamp(value)


class ScenarioFeasibilityAssessment(BaseModel):
    feasibility_id: str = ""
    scenario_id: str = ""
    feasibility_status: str = "unknown"
    explanation: str = ""
    blocking_reasons: list[str] = Field(default_factory=list)
    conditional_reasons: list[str] = Field(default_factory=list)
    satisfied_requirements: list[str] = Field(default_factory=list)
    unresolved_requirements: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    _validate_status = field_validator("feasibility_status")(classmethod(_validator_for(FEASIBILITY_STATUSES, "feasibility_status")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Dependencies / conflicts / gaps / alternatives
# ---------------------------------------------------------------------------


class ScenarioDependency(BaseModel):
    dependency_id: str
    scenario_id: str
    required_scenario_id: str
    dependency_type: str = "unknown"
    reason: str = ""
    blocking: bool = True
    confidence: float = 0.0
    source_goal_dependency: str = ""
    source_graph_nodes: list[str] = Field(default_factory=list)
    source_graph_edges: list[str] = Field(default_factory=list)

    _validate_type = field_validator("dependency_type")(classmethod(_validator_for(DEPENDENCY_TYPES, "dependency_type")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ScenarioConflict(BaseModel):
    conflict_id: str
    scenario_ids: list[str] = Field(default_factory=list)
    conflict_type: str = "unknown"
    description: str = ""
    severity: str = "low"
    resolution: str = ""
    status: str = "open"

    _validate_type = field_validator("conflict_type")(classmethod(_validator_for(CONFLICT_TYPES, "conflict_type")))
    _validate_status = field_validator("status")(classmethod(_validator_for(CONFLICT_STATUSES, "conflict status")))


class ScenarioGap(BaseModel):
    gap_id: str
    scenario_id: str
    gap_type: str
    description: str = ""
    blocking: bool = False
    severity: str = "low"
    confidence: float = 0.3
    affected_steps: list[str] = Field(default_factory=list)
    source_goal_id: str = ""
    source_graph_nodes: list[str] = Field(default_factory=list)
    source_graph_edges: list[str] = Field(default_factory=list)
    recommended_resolution_goal_type: str = ""
    status: str = "open"

    _validate_gap_type = field_validator("gap_type")(classmethod(_validator_for(SCENARIO_GAP_TYPES, "gap_type")))
    _validate_status = field_validator("status")(classmethod(_validator_for(CONFLICT_STATUSES, "gap status")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ScenarioAlternative(BaseModel):
    alternative_id: str
    scenario_id: str
    alternative_scenario_id: str
    differentiation: list[str] = Field(default_factory=list)
    rationale: str = ""


# ---------------------------------------------------------------------------
# Candidate (pre-assembly draft)
# ---------------------------------------------------------------------------


class ScenarioCandidate(BaseModel):
    candidate_id: str
    goal_id: str
    scenario_type: str
    variant: str = "primary"
    title: str = ""
    objective: str = ""
    template_id: str = ""
    mutation_level: str = "read_only"  # read_only | mutating
    actor_count_estimate: int = 1
    rationale: str = ""

    _validate_scenario_type = field_validator("scenario_type")(classmethod(_validator_for(SCENARIO_TYPES, "scenario_type")))


# ---------------------------------------------------------------------------
# Investigation scenario (the durable, queryable record)
# ---------------------------------------------------------------------------


class InvestigationScenario(BaseModel):
    scenario_id: str
    goal_id: str
    scenario_type: str
    title: str = ""
    description: str = ""
    objective: str = ""
    status: str = "draft"
    feasibility_status: str = "unknown"
    confidence: float = 0.0
    priority_hint: float = 0.0
    graph_version: int = 0
    goal_version: int = 0
    source_goal_type: str = ""
    source_goal_priority: float = 0.0

    actor_requirements: list[ScenarioActorRequirement] = Field(default_factory=list)
    permission_requirements: list[ScenarioPermissionRequirement] = Field(default_factory=list)
    entity_requirements: list[ScenarioEntityRequirement] = Field(default_factory=list)
    state_requirements: list[ScenarioStateRequirement] = Field(default_factory=list)
    workflow_requirements: list[ScenarioWorkflowRequirement] = Field(default_factory=list)
    output_requirements: list[ScenarioOutputRequirement] = Field(default_factory=list)
    data_requirements: list[ScenarioDataRequirement] = Field(default_factory=list)

    preconditions: list[ScenarioPrecondition] = Field(default_factory=list)
    steps: list[ScenarioStep] = Field(default_factory=list)
    branches: list[ScenarioBranch] = Field(default_factory=list)
    checkpoints: list[ScenarioCheckpoint] = Field(default_factory=list)
    observations: list[ScenarioObservation] = Field(default_factory=list)
    assertions: list[ScenarioAssertion] = Field(default_factory=list)
    comparisons: list[ScenarioComparison] = Field(default_factory=list)
    evidence_requirements: list[ScenarioEvidenceRequirement] = Field(default_factory=list)
    postconditions: list[ScenarioPostcondition] = Field(default_factory=list)

    cleanup_plan: Optional[ScenarioCleanupPlan] = None
    rollback_plan: Optional[ScenarioRollbackPlan] = None
    risk_assessment: Optional[ScenarioRiskAssessment] = None
    feasibility_assessment: Optional[ScenarioFeasibilityAssessment] = None

    dependencies: list[str] = Field(default_factory=list)  # ScenarioDependency ids
    conflicts: list[str] = Field(default_factory=list)  # ScenarioConflict ids
    gaps: list[str] = Field(default_factory=list)  # ScenarioGap ids
    alternatives: list[str] = Field(default_factory=list)  # sibling scenario ids

    estimated_action_count: int = 0
    estimated_actor_switches: int = 0
    estimated_navigation_count: int = 0
    estimated_state_mutations: int = 0
    estimated_runtime_class: str = "unknown"
    complexity_score: float = 0.0
    risk_score: float = 0.0
    information_gain_score: float = 0.0
    confidence_gain_estimate: float = 0.0
    reversibility_score: float = 0.0
    determinism_score: float = 0.0

    source_graph_nodes: list[str] = Field(default_factory=list)
    source_graph_edges: list[str] = Field(default_factory=list)
    source_evidence_references: list[ScenarioEvidenceReference] = Field(default_factory=list)
    provenance: list[ScenarioProvenance] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    observation_count: int = 1
    active: bool = True
    stale: bool = False

    _validate_scenario_type = field_validator("scenario_type")(classmethod(_validator_for(SCENARIO_TYPES, "scenario_type")))
    _validate_status = field_validator("status")(classmethod(_validator_for(SCENARIO_STATUSES, "scenario status")))
    _validate_feasibility = field_validator("feasibility_status")(classmethod(_validator_for(FEASIBILITY_STATUSES, "feasibility_status")))
    _validate_runtime_class = field_validator("estimated_runtime_class")(classmethod(_validator_for(COMPLEXITY_CLASSES, "complexity_class")))

    @field_validator("confidence", "priority_hint", "complexity_score", "risk_score", "information_gain_score", "confidence_gain_estimate", "reversibility_score", "determinism_score", "source_goal_priority")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Plan / result / statistics / versioning
# ---------------------------------------------------------------------------


class ScenarioPlan(BaseModel):
    """The bundle of scenarios produced for ONE goal (primary + alternatives)."""

    plan_id: str
    goal_id: str
    primary_scenario_id: str = ""
    alternative_scenario_ids: list[str] = Field(default_factory=list)
    scenario_ids: list[str] = Field(default_factory=list)
    status: str = "draft"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    _validate_status = field_validator("status")(classmethod(_validator_for(SCENARIO_STATUSES, "scenario status")))


class ScenarioStatistics(BaseModel):
    total_scenarios: int = 0
    scenarios_by_type: dict[str, int] = Field(default_factory=dict)
    scenarios_by_status: dict[str, int] = Field(default_factory=dict)
    scenarios_by_feasibility: dict[str, int] = Field(default_factory=dict)
    scenarios_by_risk: dict[str, int] = Field(default_factory=dict)
    read_only_count: int = 0
    mutating_count: int = 0
    cross_actor_count: int = 0
    high_risk_count: int = 0
    average_complexity_score: float = 0.0
    average_confidence_gain: float = 0.0
    dependency_count: int = 0
    conflict_count: int = 0
    gap_count: int = 0
    alternative_count: int = 0
    graph_version: int = 0
    scenario_plan_version: int = 0


class ScenarioVersion(BaseModel):
    scenario_plan_version: int = 0
    source_graph_version: int = 0
    source_goal_generation_count: int = 0
    synchronized_at: datetime = Field(default_factory=datetime.utcnow)
    change_summary: str = ""
    added_scenario_ids: list[str] = Field(default_factory=list)
    updated_scenario_ids: list[str] = Field(default_factory=list)
    stale_scenario_ids: list[str] = Field(default_factory=list)
    added_gap_ids: list[str] = Field(default_factory=list)
    resolved_gap_ids: list[str] = Field(default_factory=list)
    added_conflict_ids: list[str] = Field(default_factory=list)
    resolved_conflict_ids: list[str] = Field(default_factory=list)


class ScenarioPlanningResult(BaseModel):
    result_id: str = ""
    scenarios: list[InvestigationScenario] = Field(default_factory=list)
    plans: list[ScenarioPlan] = Field(default_factory=list)
    dependencies: list[ScenarioDependency] = Field(default_factory=list)
    conflicts: list[ScenarioConflict] = Field(default_factory=list)
    gaps: list[ScenarioGap] = Field(default_factory=list)
    alternatives: list[ScenarioAlternative] = Field(default_factory=list)
    statistics: ScenarioStatistics = Field(default_factory=ScenarioStatistics)
    graph_version: int = 0
    scenario_plan_version: int = 0
    generated_at: datetime = Field(default_factory=datetime.utcnow)

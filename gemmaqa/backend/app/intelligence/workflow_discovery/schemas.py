"""Workflow Discovery schemas — application-neutral by construction.

No field here may name a business process. `canonical_name` and every
`*_verb`/`*_state` string are data extracted at runtime from the observed
application's own text (a button label, a status badge, a URL segment) —
never vocabulary shipped in this code.

These are DELIBERATELY distinct from `app.schemas.Workflow`/`WorkflowStep`
(a narrative log of one run's recorded action sequence, built by
`RunMemory.finalize_workflow()`). This package reconstructs the underlying
BUSINESS PROCESS a workflow candidate belongs to — a different concept, a
different shape, and a different (much larger, cross-page/cross-actor/
cross-session) lifecycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.ids import new_id

# ---------------------------------------------------------------------------
# Controlled, generic vocabularies
# ---------------------------------------------------------------------------

WORKFLOW_EVIDENCE_SOURCE_KINDS = frozenset(
    {
        "create_form",
        "edit_form",
        "multi_step_form",
        "status_control",
        "status_badge",
        "assignment_control",
        "approval_control",
        "rejection_control",
        "confirmation_dialog",
        "progression_button",
        "table_row_transition",
        "list_to_detail_navigation",
        "entity_cross_reference",
        "actor_queue",
        "notification",
        "alert",
        "empty_state",
        "page_heading",
        "breadcrumb",
        "network_mutation",
        "network_response",
        "before_after_page_state",
        "before_after_entity_state",
        "application_memory",
        "entity_operation",
        "actor_permission",
        "access_denied",
        "navigation_difference",
        "actor_handoff",
        "prerequisite_check",
        "branch_control",
    }
)

# WorkflowDescriptor lifecycle. Mirrors the candidate/incomplete/confirmed
# vocabulary already established by entity/actor discovery, plus
# "contradicted" (evidence conflicts) and "stale" (not re-observed recently
# — reserved, not yet actively computed this milestone, same as actor
# discovery's own unused "stale").
WORKFLOW_STATUSES = frozenset({"candidate", "partial", "confirmed", "contradicted", "stale"})

# Per the task's explicit requirement: never conflate inferred with observed.
STEP_STATUSES = frozenset(
    {"observed", "partially_observed", "inferred", "blocked", "unverified", "contradicted", "completed"}
)

TRIGGER_TYPES = frozenset(
    {"navigation", "form_submission", "button_click", "entity_creation", "scheduled", "external", "unknown"}
)

OUTCOME_TYPES = frozenset(
    {"state_change", "notification", "dashboard_update", "new_entity", "report_change", "access_change", "unknown"}
)

PREREQUISITE_TYPES = frozenset(
    {
        "authentication",
        "required_actor",
        "required_permission",
        "required_entity",
        "required_related_entity",
        "required_state",
        "required_configuration",
        "required_assignment",
        "required_prior_workflow",
        "required_data_availability",
    }
)

BRANCH_TYPES = frozenset(
    {
        "approve_vs_reject",
        "save_vs_submit",
        "success_vs_failure",
        "complete_vs_cancel",
        "assigned_vs_unassigned",
        "active_vs_archived",
        "retry_vs_abandon",
        "unclassified",
    }
)

GAP_TYPES = frozenset(
    {
        "unknown_initiating_actor",
        "missing_creation_path",
        "missing_completion_path",
        "unknown_state_transition",
        "unresolved_actor_handoff",
        "unknown_prerequisite",
        "outcome_without_producer",
        "mutation_without_observed_result",
        "dashboard_without_source_workflow",
        "status_without_changing_action",
        "permission_without_workflow",
        "relationship_without_workflow",
    }
)

WORKFLOW_ROLES = frozenset({"initiator", "approver", "reviewer", "assignee", "observer", "system"})


class WorkflowEvidence(BaseModel):
    """One observation supporting a workflow claim — WHERE it was seen, never
    WHAT it means."""

    evidence_id: str = Field(default_factory=new_id)
    source_kind: str
    observed_text: str = ""
    page_url: str = ""
    state_fingerprint: str = ""
    element_id: Optional[str] = None
    observed_at_iteration: int = 0

    @field_validator("source_kind")
    @classmethod
    def _validate_source_kind(cls, value: str) -> str:
        if value not in WORKFLOW_EVIDENCE_SOURCE_KINDS:
            raise ValueError(
                f"Invalid workflow evidence source_kind: {value!r} "
                f"(expected one of {sorted(WORKFLOW_EVIDENCE_SOURCE_KINDS)})"
            )
        return value


class WorkflowState(BaseModel):
    """A named or structural state a workflow's subject entity can occupy.
    `is_explicit` distinguishes a real observed label ("Submitted", a status
    badge/dropdown value) from a structural placeholder synthesized when no
    explicit label exists ("state before clicking Approve") — the latter
    always carries lower confidence."""

    state_id: str = Field(default_factory=new_id)
    label: str
    is_explicit: bool = False
    source_kind: str = "structural"
    confidence: float = 0.3
    evidence: list[WorkflowEvidence] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowTransition(BaseModel):
    """EntityState A -> action -> EntityState B. `source_state`/`target_state`
    are state labels (see WorkflowState) — explicit when the application
    showed one, structural otherwise."""

    transition_id: str = Field(default_factory=new_id)
    entity_id: Optional[str] = None
    source_state: str
    target_state: str
    action_verb: str = ""
    is_explicit: bool = False
    page_url: str = ""
    state_fingerprint_before: str = ""
    state_fingerprint_after: str = ""
    confidence: float = 0.3
    evidence: list[WorkflowEvidence] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowTrigger(BaseModel):
    trigger_id: str = Field(default_factory=new_id)
    trigger_type: str = "unknown"
    description: str = ""
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    confidence: float = 0.3

    @field_validator("trigger_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in TRIGGER_TYPES:
            raise ValueError(f"Invalid trigger_type: {value!r} (expected one of {sorted(TRIGGER_TYPES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowOutcome(BaseModel):
    outcome_id: str = Field(default_factory=new_id)
    description: str = ""
    outcome_type: str = "unknown"
    produced_by_step_id: Optional[str] = None
    visible_to_actor_ids: list[str] = Field(default_factory=list)
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    confidence: float = 0.3

    @field_validator("outcome_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in OUTCOME_TYPES:
            raise ValueError(f"Invalid outcome_type: {value!r} (expected one of {sorted(OUTCOME_TYPES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowPrerequisite(BaseModel):
    prerequisite_id: str = Field(default_factory=new_id)
    type: str
    target: str = ""
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    confidence: float = 0.3
    satisfied: Optional[bool] = None
    blocking_reason: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in PREREQUISITE_TYPES:
            raise ValueError(f"Invalid prerequisite type: {value!r} (expected one of {sorted(PREREQUISITE_TYPES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowBranch(BaseModel):
    """A decision point with 2+ mutually exclusive options — never merged
    into a single linear workflow."""

    branch_id: str = Field(default_factory=new_id)
    description: str = ""
    branch_type: str = "unclassified"
    decision_point_step_id: Optional[str] = None
    option_labels: list[str] = Field(default_factory=list)
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    confidence: float = 0.3

    @field_validator("branch_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in BRANCH_TYPES:
            raise ValueError(f"Invalid branch_type: {value!r} (expected one of {sorted(BRANCH_TYPES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowStep(BaseModel):
    """One action within a reconstructed workflow. `status` must never
    collapse `inferred` into `observed` — see STEP_STATUSES."""

    step_id: str = Field(default_factory=new_id)
    sequence_hint: int = 0
    action_verb: str = ""
    semantic_action: str = ""
    actor_id: Optional[str] = None
    entity_id: Optional[str] = None
    source_entity_id: Optional[str] = None
    target_entity_id: Optional[str] = None
    source_state: Optional[str] = None
    target_state: Optional[str] = None
    page_id: Optional[str] = None
    page_state_fingerprint: Optional[str] = None
    control_id: Optional[str] = None
    form_id: Optional[str] = None
    dialog_id: Optional[str] = None
    endpoint_evidence: Optional[str] = None
    prerequisite_ids: list[str] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)
    observed_outcomes: list[str] = Field(default_factory=list)
    confidence: float = 0.3
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    status: str = "inferred"

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in STEP_STATUSES:
            raise ValueError(f"Invalid step status: {value!r} (expected one of {sorted(STEP_STATUSES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowActorParticipation(BaseModel):
    actor_id: str
    role_in_workflow: str = "observer"
    step_ids: list[str] = Field(default_factory=list)
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    confidence: float = 0.3

    @field_validator("role_in_workflow")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        if value not in WORKFLOW_ROLES:
            raise ValueError(f"Invalid role_in_workflow: {value!r} (expected one of {sorted(WORKFLOW_ROLES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowEntityParticipation(BaseModel):
    entity_id: str
    role_in_workflow: str = "primary_subject"  # primary_subject | related
    step_ids: list[str] = Field(default_factory=list)
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    confidence: float = 0.3

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowGap(BaseModel):
    gap_id: str = Field(default_factory=new_id)
    workflow_id: Optional[str] = None
    gap_type: str
    description: str = ""
    related_actor_ids: list[str] = Field(default_factory=list)
    related_entity_ids: list[str] = Field(default_factory=list)
    related_step_ids: list[str] = Field(default_factory=list)
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    confidence: float = 0.3
    exploration_value: float = 0.5
    recommended_investigation_goal: str = ""

    @field_validator("gap_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in GAP_TYPES:
            raise ValueError(f"Invalid gap_type: {value!r} (expected one of {sorted(GAP_TYPES)})")
        return value

    @field_validator("confidence", "exploration_value")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class WorkflowExecutionObservation(BaseModel):
    """One concrete instance of a step/transition actually happening —
    feeds confidence's "repeated evidence" / "successful replay" factors
    without letting duplicate re-observation inflate confidence unboundedly
    (see workflow_confidence.py)."""

    observation_id: str = Field(default_factory=new_id)
    step_id: Optional[str] = None
    transition_id: Optional[str] = None
    page_url: str = ""
    state_fingerprint_before: str = ""
    state_fingerprint_after: str = ""
    success: bool = True
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    observed_at_iteration: int = 0


class WorkflowCandidate(BaseModel):
    """A raw, per-observation workflow signal BEFORE reconstruction — many
    candidates merge into existing WorkflowDescriptors rather than becoming
    new ones."""

    candidate_type: str
    entity_id: Optional[str] = None
    actor_id: Optional[str] = None
    action_verb: str = ""
    evidence: WorkflowEvidence
    page_url: str = ""


class WorkflowDescriptor(BaseModel):
    """A reconstructed business workflow."""

    workflow_id: str = Field(default_factory=new_id)
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    status: str = "candidate"
    confidence: float = 0.0
    supporting_evidence: list[WorkflowEvidence] = Field(default_factory=list)

    source_pages: list[str] = Field(default_factory=list)
    source_states: list[str] = Field(default_factory=list)
    source_elements: list[str] = Field(default_factory=list)
    source_network_evidence: list[str] = Field(default_factory=list)

    actors: list[WorkflowActorParticipation] = Field(default_factory=list)
    entities: list[WorkflowEntityParticipation] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(default_factory=list)
    triggers: list[WorkflowTrigger] = Field(default_factory=list)
    prerequisites: list[WorkflowPrerequisite] = Field(default_factory=list)
    transitions: list[WorkflowTransition] = Field(default_factory=list)
    branches: list[WorkflowBranch] = Field(default_factory=list)
    outcomes: list[WorkflowOutcome] = Field(default_factory=list)
    states: list[WorkflowState] = Field(default_factory=list)
    executions: list[WorkflowExecutionObservation] = Field(default_factory=list)

    known_entry_points: list[str] = Field(default_factory=list)
    known_exit_points: list[str] = Field(default_factory=list)
    known_failure_paths: list[str] = Field(default_factory=list)
    known_success_paths: list[str] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)

    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    observation_count: int = 0
    version: int = 1

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in WORKFLOW_STATUSES:
            raise ValueError(f"Invalid workflow status: {value!r} (expected one of {sorted(WORKFLOW_STATUSES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))

    def observed_step_count(self) -> int:
        return sum(1 for s in self.steps if s.status in {"observed", "completed"})

    def inferred_step_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "inferred")

    def is_cross_role(self) -> bool:
        return len({p.actor_id for p in self.actors}) >= 2

    def requires_another_actor(self) -> bool:
        """True when an actor hand-off was detected but the OTHER actor
        could not be named yet (an unresolved `required_actor` prerequisite,
        `satisfied=False`) — per this milestone's explicit "do not
        auto-switch, mark unresolved instead" requirement. A hand-off to a
        NAMED, already-known actor resolves immediately (that actor is
        attached as a participant, see WorkflowReconstructor) and is not a
        gap; only the "we know someone else must act but can't say who"
        case counts as still requiring another actor."""
        return any(p.type == "required_actor" and p.satisfied is False for p in self.prerequisites)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "actor_ids": [p.actor_id for p in self.actors],
            "entity_ids": [p.entity_id for p in self.entities],
            "step_count": len(self.steps),
            "observed_step_count": self.observed_step_count(),
            "inferred_step_count": self.inferred_step_count(),
            "transition_count": len(self.transitions),
            "branch_count": len(self.branches),
            "prerequisite_count": len(self.prerequisites),
            "outcome_count": len(self.outcomes),
            "unresolved_gap_count": len(self.unresolved_gaps),
            "is_cross_role": self.is_cross_role(),
            "requires_another_actor": self.requires_another_actor(),
            "known_entry_points": list(self.known_entry_points),
            "known_exit_points": list(self.known_exit_points),
            "observation_count": self.observation_count,
            "version": self.version,
        }


class WorkflowRegistrySnapshot(BaseModel):
    """A serializable snapshot of the registry's current state — the
    typed counterpart of `WorkflowRegistry.summary()`."""

    workflow_count: int = 0
    known: list[str] = Field(default_factory=list)
    incomplete: list[str] = Field(default_factory=list)
    high_confidence: list[str] = Field(default_factory=list)
    low_confidence: list[str] = Field(default_factory=list)
    cross_role: list[str] = Field(default_factory=list)
    requiring_another_actor: list[str] = Field(default_factory=list)
    gap_count: int = 0
    workflows: list[dict[str, Any]] = Field(default_factory=list)

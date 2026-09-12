"""Adaptive Application Understanding Engine schemas.

Application-neutral AND framework-neutral by construction. No field, value,
or vocabulary member here may name:

  - a business role, entity, workflow, KPI, or module (the same
    application-neutrality rule every other intelligence package follows), OR
  - a UI framework, library, or rendering strategy (React/Vue/Angular/
    Svelte/htmx/...). This package exists precisely because GemmaQA must
    reason about *observable behaviour*, never about which framework
    produced it.

Every readiness signal in `READINESS_SIGNAL_KINDS` is therefore expressed as
something a human tester could see or a standards-based accessibility
attribute exposes -- "nothing is interactive yet", "the page is still
changing", "a progress indicator is present" -- never "Vue has not mounted".

Determinism discipline (carried forward from the Goal Generation ->
Scenario Planning -> QA Strategy -> Autonomous Investigation lineage): no
field here defaults to a random id. Every id is derived deterministically
from the content it describes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

# Observable readiness signals. Each is a question a human could answer by
# looking at the screen, or a W3C-standard ARIA attribute -- never a
# framework internal.
READINESS_SIGNAL_KINDS = frozenset(
    {
        "no_interactive_elements",      # nothing is clickable/fillable yet
        "sparse_interactive_elements",  # implausibly few controls for a real page
        "no_content",                   # no headings/text blocks rendered
        "sparse_content",               # very little text rendered
        "busy_region_present",          # aria-busy="true" (W3C standard)
        "progress_indicator_present",   # role="progressbar" (W3C standard)
        "repeated_empty_containers",    # placeholder-shaped repeated structure
        "pending_network_activity",     # requests still in flight
        "failed_network_activity",      # requests failed (may explain emptiness)
        "console_errors_present",       # scripting failed; render may be incomplete
        "dom_still_changing",           # consecutive observations differ
        "dom_settled",                  # consecutive observations agree
        "content_growing",              # observation N+1 has strictly more content
        "interactive_surface_present",  # controls exist -- positive readiness
        "content_present",              # real content exists -- positive readiness
        "empty_document",               # no body content at all
        "unknown",
    }
)

# Signals whose presence ARGUES FOR readiness. Everything else in
# READINESS_SIGNAL_KINDS argues against it (or is neutral evidence).
POSITIVE_READINESS_SIGNALS = frozenset(
    {"interactive_surface_present", "content_present", "dom_settled"}
)

# Application states, inferred from structural evidence only.
APPLICATION_STATES = frozenset(
    {
        "initializing",
        "loading",
        "interactive",
        "partially_interactive",
        "authentication",
        "dashboard",
        "collection",
        "detail",
        "create",
        "edit",
        "wizard",
        "modal",
        "confirmation",
        "validation_failure",
        "permission_denied",
        "backend_failure",
        "frontend_failure",
        "empty_state",
        "no_data",
        "feature_disabled",
        "coming_soon",
        "placeholder",
        "unknown",
    }
)

# States in which NOT seeing something is no evidence that it is absent: the
# application has not finished presenting itself, or failed to present itself
# at all. Concluding "the record is gone" from one of these is concluding from
# a page that was never rendered.
#
# A live cleanup pass navigated back to a record's list page, observed 2
# elements while the client-side app was still booting, was told by this engine
# that the state was `frontend_failure` — and then reported the record as
# unreachable and left it on the application under test.
UNTRUSTWORTHY_ABSENCE_STATES = frozenset(
    {
        "initializing",
        "loading",
        "partially_interactive",
        "frontend_failure",
        "backend_failure",
        "unknown",
    }
)

# What the engine advises the caller to do with this observation.
OBSERVATION_DECISIONS = frozenset(
    {
        "accept",            # confidence is sufficient; reason from this observation
        "reobserve",         # evidence is weak; sample again before reasoning
        "accept_degraded",   # re-observation budget spent; proceed but flag low confidence
    }
)

# Contradiction kinds this engine can raise. Deliberately mirrors the SHAPE
# of `GraphContradiction`/`GraphConsistencyIssue` (deterministic id, node
# refs, evidence, confidence) rather than inventing a parallel mechanism --
# see `understanding_contradictions.py`.
UNDERSTANDING_CONTRADICTION_TYPES = frozenset(
    {
        "behaviour_contradiction",   # same action, materially different outcome
        "navigation_contradiction",  # a route/target that previously worked no longer does
        "validation_contradiction",  # validation appeared/disappeared for the same input shape
        "permission_contradiction",  # access granted/denied differs from prior observation
        "state_contradiction",       # same fingerprint classified as a different state
        "unknown",
    }
)

CONTRADICTION_STATUSES = frozenset({"open", "acknowledged", "resolved"})

# Why an observation's confidence is what it is.
CONFIDENCE_BASES = frozenset(
    {"direct_observation", "heuristic", "inferred", "insufficient_evidence"}
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
# Readiness
# ---------------------------------------------------------------------------


class ReadinessSignal(BaseModel):
    """One observable fact bearing on "is this application ready to reason
    about?". `supports_ready` records the DIRECTION of the evidence so a
    reader never has to know which kinds are positive."""

    signal_id: str = ""
    kind: str = "unknown"
    supports_ready: bool = False
    weight: float = 0.0
    observed_value: str = ""
    description: str = ""

    _validate_kind = field_validator("kind")(classmethod(_validator_for(READINESS_SIGNAL_KINDS, "readiness signal kind")))

    @field_validator("weight")
    @classmethod
    def _clamp_weight(cls, value: float) -> float:
        return _clamp(value)


class ReadinessAssessment(BaseModel):
    """Whether the evidence suggests the application is ready for reasoning.
    Never a boolean alone -- always a score plus the signals behind it."""

    assessment_id: str = ""
    readiness_score: float = 0.0
    signals: list[ReadinessSignal] = Field(default_factory=list)
    positive_signal_kinds: list[str] = Field(default_factory=list)
    blocking_signal_kinds: list[str] = Field(default_factory=list)
    explanation: str = ""

    @field_validator("readiness_score")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return _clamp(value)


class StabilityAssessment(BaseModel):
    """Whether the application stopped changing between two consecutive
    observations. This is how DOM mutation is detected without injecting a
    MutationObserver or knowing anything about the rendering technology:
    sample twice, compare what a tester would see."""

    stable: bool = False
    compared: bool = False
    fingerprint_changed: bool = False
    interactive_delta: int = 0
    content_delta: int = 0
    collection_row_delta: int = 0
    explanation: str = ""


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


class StateHypothesis(BaseModel):
    """One candidate answer to "what am I looking at?", with the evidence
    both for and against it. Multiple hypotheses may coexist -- the engine
    ranks rather than forces a single answer."""

    hypothesis_id: str = ""
    state: str = "unknown"
    confidence: float = 0.0
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)

    _validate_state = field_validator("state")(classmethod(_validator_for(APPLICATION_STATES, "application state")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ApplicationStateAssessment(BaseModel):
    assessment_id: str = ""
    primary_state: str = "unknown"
    confidence: float = 0.0
    hypotheses: list[StateHypothesis] = Field(default_factory=list)
    unknown_reason: str = ""

    _validate_state = field_validator("primary_state")(classmethod(_validator_for(APPLICATION_STATES, "application state")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Observation confidence
# ---------------------------------------------------------------------------


class ObservationConfidence(BaseModel):
    """How much this observation can be trusted as a basis for planning.
    Carries what was missing and what to do next, so a low score is
    actionable rather than merely discouraging."""

    value: float = 0.0
    basis: str = "insufficient_evidence"
    supporting_evidence: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    recommended_next_observation: str = ""

    _validate_basis = field_validator("basis")(classmethod(_validator_for(CONFIDENCE_BASES, "confidence basis")))

    @field_validator("value")
    @classmethod
    def _clamp_value(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Contradictions and transitions
# ---------------------------------------------------------------------------


class UnderstandingContradiction(BaseModel):
    """Observed behaviour that disagrees with what this run previously
    recorded. Deliberately shaped like `GraphContradiction` (deterministic
    id, description, evidence, confidence, status) so downstream consumers
    treat it the same way."""

    contradiction_id: str = ""
    contradiction_type: str = "unknown"
    description: str = ""
    previous_observation: str = ""
    current_observation: str = ""
    supporting_evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.3
    status: str = "open"
    first_seen: datetime = Field(default_factory=datetime.utcnow)

    _validate_type = field_validator("contradiction_type")(classmethod(_validator_for(UNDERSTANDING_CONTRADICTION_TYPES, "contradiction type")))
    _validate_status = field_validator("status")(classmethod(_validator_for(CONTRADICTION_STATUSES, "contradiction status")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ObservedStateTransition(BaseModel):
    """`state A --action--> state B`, in APPLICATION-STATE terms rather than
    URL terms. Complements `workflow_discovery.WorkflowTransition` (which
    models ENTITY state) -- this one models what kind of screen the user is
    now looking at, which is a different and equally real axis."""

    transition_id: str = ""
    from_state: str = "unknown"
    to_state: str = "unknown"
    action_type: str = ""
    action_label: str = ""
    from_fingerprint: str = ""
    to_fingerprint: str = ""
    url_changed: bool = False
    observation_count: int = 1
    confidence: float = 0.0

    _validate_from = field_validator("from_state")(classmethod(_validator_for(APPLICATION_STATES, "application state")))
    _validate_to = field_validator("to_state")(classmethod(_validator_for(APPLICATION_STATES, "application state")))

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Top-level assessment + statistics
# ---------------------------------------------------------------------------


class UnderstandingAssessment(BaseModel):
    """The engine's complete answer for ONE observation: is it ready, what
    is it, how much do we trust that, and should the caller look again."""

    assessment_id: str = ""
    url: str = ""
    fingerprint: str = ""
    observation_pass: int = 1
    readiness: ReadinessAssessment = Field(default_factory=ReadinessAssessment)
    stability: StabilityAssessment = Field(default_factory=StabilityAssessment)
    state: ApplicationStateAssessment = Field(default_factory=ApplicationStateAssessment)
    observation_confidence: ObservationConfidence = Field(default_factory=ObservationConfidence)
    decision: str = "accept"
    decision_reason: str = ""
    unknowns: list[str] = Field(default_factory=list)
    assessed_at: datetime = Field(default_factory=datetime.utcnow)

    _validate_decision = field_validator("decision")(classmethod(_validator_for(OBSERVATION_DECISIONS, "observation decision")))

    @property
    def is_ready(self) -> bool:
        return self.decision != "reobserve"


class UnderstandingStatistics(BaseModel):
    total_assessments: int = 0
    total_reobservations: int = 0
    degraded_acceptances: int = 0
    assessments_by_state: dict[str, int] = Field(default_factory=dict)
    assessments_by_decision: dict[str, int] = Field(default_factory=dict)
    average_readiness_score: float = 0.0
    average_observation_confidence: float = 0.0
    contradiction_count: int = 0
    contradictions_by_type: dict[str, int] = Field(default_factory=dict)
    transition_count: int = 0
    unknown_state_count: int = 0
    understanding_version: int = 0

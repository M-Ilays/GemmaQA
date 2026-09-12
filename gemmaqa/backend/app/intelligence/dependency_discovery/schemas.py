"""Business Dependency Discovery schemas — application-neutral by
construction.

No field here may name a business KPI, entity, actor, or workflow. Every
`canonical_name`/`label`/`*_term` string is data extracted at runtime from
the observed application's own text (a dashboard card's heading, a table
header, a chart legend) — never vocabulary shipped in this code.

`entity_id`/`actor_id`/`workflow_id` fields on these schemas are the same
canonical-name TERM strings already used by
`app.intelligence.entity_discovery`/`actor_discovery`/`workflow_discovery`
(e.g. `"job"`, `"current session"`) — never a second internal id scheme —
so a dependency can be cross-referenced against those registries directly
by string equality, exactly like `WorkflowStep.entity_id`/`actor_id` already
do.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.ids import new_id

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

OUTPUT_TYPES = frozenset(
    {
        "kpi_card", "counter", "badge", "chart_total", "chart_series",
        "chart_segment", "table_total", "queue_count", "notification_count",
        "alert_count", "report_summary", "status_board", "progress_indicator",
        "percentage", "ratio", "monetary_total", "duration_summary",
        "average", "min_max", "trend_indicator", "period_summary",
        "empty_state_count", "pagination_total", "row_count_label",
        "derived_form_field", "calculated_field", "unknown",
    }
)

# What MetricSemanticAnalyzer classifies an output's underlying nature as.
METRIC_SEMANTIC_TYPES = frozenset(
    {
        "count", "total", "sum", "average", "percentage", "ratio",
        "duration", "currency", "trend", "status_distribution", "backlog",
        "capacity", "utilisation", "completion", "failure", "warning",
        "alert", "activity", "revenue_like", "age_based", "unknown",
    }
)

DEPENDENCY_STATUSES = frozenset(
    {"observed", "partially_observed", "inferred", "candidate", "verified", "contradicted", "blocked", "stale"}
)

DEPENDENCY_TYPES = frozenset(
    {"entity_dependency", "workflow_dependency", "actor_dependency", "state_dependency", "aggregate_dependency", "unknown"}
)

RELATIONSHIP_TYPES = frozenset(
    {
        "counts", "sums", "averages", "groups_by", "filters_by_state",
        "filters_by_actor", "filters_by_time", "filters_by_tenant",
        "includes", "excludes", "increments", "decrements", "recalculates",
        "creates_notification", "creates_alert", "populates_queue",
        "removes_from_queue", "contributes_to_chart", "contributes_to_report",
        "controls_visibility", "enables_action", "disables_action",
        "affects_status_board", "unknown_effect",
    }
)

EFFECT_DIRECTIONS = frozenset(
    {
        "increase", "decrease", "add", "remove", "replace", "recalculate",
        "redistribute", "move_between_categories", "create", "clear",
        "activate", "deactivate", "enable", "disable", "unknown",
    }
)

AGGREGATION_RULE_TYPES = frozenset(
    {
        "count_all", "count_in_state", "count_by_actor", "count_in_time_range",
        "sum_field", "average_field", "group_by_state", "percentage_of_total",
        "ratio_between_categories", "latest_value", "cumulative_total",
        "distinct_count", "unknown",
    }
)

SCOPE_DIMENSION_TYPES = frozenset(
    {
        "actor", "actor_role", "tenant", "location", "department", "team",
        "assigned_user", "date_range", "timezone", "selected_status",
        "selected_tab", "filter", "search_query", "pagination", "ownership",
        "account", "environment", "organisation", "workspace", "unknown",
    }
)

CORRELATION_RESULTS = frozenset(
    {"supported", "unsupported", "contradicted", "delayed", "scope_incompatible", "inconclusive"}
)

GAP_TYPES = frozenset(
    {
        "kpi_without_source_entity", "output_without_producing_workflow",
        "workflow_outcome_without_consumer", "alert_without_known_trigger",
        "queue_without_entry_workflow", "queue_without_exit_workflow",
        "chart_category_without_transition", "report_total_without_aggregation_rule",
        "output_changed_source_unknown", "source_changed_output_unchanged",
        "unknown_actor_consumer", "unresolved_role_switch",
        "ambiguous_scope", "unknown_time_range", "unknown_tenant_boundary",
        "output_api_disagreement", "competing_dependency_explanations",
    }
)

VERIFICATION_STEP_TYPES = frozenset(
    {
        "login_as_actor", "create_source_record", "perform_transition",
        "return_as_actor", "compare_output", "wait_for_refresh", "unknown",
    }
)

EVIDENCE_SOURCE_KINDS = frozenset(
    {
        "heading", "text_block", "table_header", "table_total", "chart_image",
        "chart_legend", "alert", "dialog", "interactive_element",
        "network_endpoint", "network_aggregate_field", "entity_registry",
        "actor_registry", "workflow_registry", "workflow_transition",
        "before_after_observation", "application_memory", "label_match",
        "terminology_match",
    }
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class DependencyEvidence(BaseModel):
    """One observation supporting a dependency claim — WHERE it was seen,
    never WHAT it means."""

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
        if value not in EVIDENCE_SOURCE_KINDS:
            raise ValueError(f"Invalid evidence source_kind: {value!r} (expected one of {sorted(EVIDENCE_SOURCE_KINDS)})")
        return value


# ---------------------------------------------------------------------------
# Derived output family
# ---------------------------------------------------------------------------


class DerivedOutputDescriptor(BaseModel):
    """A single visible, potentially-data-derived value observed on a page —
    a KPI card, a badge, a chart total, a queue count, ... `raw_value` is the
    literal displayed text; `parsed_value` is only populated when it could be
    safely parsed as a number (never guessed)."""

    output_id: str = Field(default_factory=new_id)
    canonical_label: str
    aliases: list[str] = Field(default_factory=list)
    output_type: str = "unknown"
    raw_value: str = ""
    parsed_value: Optional[float] = None
    unit: Optional[str] = None
    format: Optional[str] = None
    region_id: Optional[str] = None
    page_url: str = ""
    state_fingerprint: str = ""
    nearby_context: list[str] = Field(default_factory=list)
    scope_dimension_ids: list[str] = Field(default_factory=list)
    current_actor_term: Optional[str] = None
    tenant_term: Optional[str] = None
    is_aggregate: bool = False
    is_static: bool = False
    is_derived_field: bool = False
    confidence: float = 0.3
    evidence: list[DependencyEvidence] = Field(default_factory=list)
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    observation_count: int = 1

    @field_validator("output_type")
    @classmethod
    def _validate_output_type(cls, value: str) -> str:
        if value not in OUTPUT_TYPES:
            raise ValueError(f"Invalid output_type: {value!r} (expected one of {sorted(OUTPUT_TYPES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class MetricDescriptor(BaseModel):
    """Semantic classification of a `DerivedOutputDescriptor` — its inferred
    generic nature (count/sum/average/...), never a business meaning."""

    metric_id: str = Field(default_factory=new_id)
    output_id: str
    metric_semantic_type: str = "unknown"
    candidate_entity_terms: list[str] = Field(default_factory=list)
    candidate_state_terms: list[str] = Field(default_factory=list)
    candidate_workflow_terms: list[str] = Field(default_factory=list)
    confidence: float = 0.3
    evidence: list[DependencyEvidence] = Field(default_factory=list)

    @field_validator("metric_semantic_type")
    @classmethod
    def _validate_metric_semantic_type(cls, value: str) -> str:
        if value not in METRIC_SEMANTIC_TYPES:
            raise ValueError(f"Invalid metric_semantic_type: {value!r} (expected one of {sorted(METRIC_SEMANTIC_TYPES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class CounterDescriptor(DerivedOutputDescriptor):
    """A simple incrementing/decrementing count-style output. A thin,
    intention-revealing alias over `DerivedOutputDescriptor` — carries no
    extra fields, used where a builder wants to be explicit about output
    shape at construction time."""


class BadgeDescriptor(DerivedOutputDescriptor):
    """A small, usually queue/notification-attached count badge."""


class ChartDescriptor(DerivedOutputDescriptor):
    """A chart total or one labeled segment/series within a chart."""

    legend_label: Optional[str] = None
    series_terms: list[str] = Field(default_factory=list)


class ReportDescriptor(DerivedOutputDescriptor):
    """A report-page summary figure."""


class QueueDescriptor(DerivedOutputDescriptor):
    """A named queue's current count (e.g. an inbox/backlog count)."""


class SummaryDescriptor(DerivedOutputDescriptor):
    """A period ("today"/"this week") or status-board summary figure."""


# ---------------------------------------------------------------------------
# Aggregation / scope rules
# ---------------------------------------------------------------------------


class AggregationRule(BaseModel):
    """One CANDIDATE explanation for how an output's value is computed.
    Multiple competing `AggregationRule`s may exist for the same output when
    the evidence is ambiguous — never collapsed into a single guess."""

    rule_id: str = Field(default_factory=new_id)
    rule_type: str = "unknown"
    field_term: Optional[str] = None
    group_by_term: Optional[str] = None
    filter_terms: list[str] = Field(default_factory=list)
    confidence: float = 0.2
    evidence: list[DependencyEvidence] = Field(default_factory=list)

    @field_validator("rule_type")
    @classmethod
    def _validate_rule_type(cls, value: str) -> str:
        if value not in AGGREGATION_RULE_TYPES:
            raise ValueError(f"Invalid rule_type: {value!r} (expected one of {sorted(AGGREGATION_RULE_TYPES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class StateInclusionRule(BaseModel):
    """A state/category this output's aggregation is believed to INCLUDE."""

    state_label: str
    confidence: float = 0.3
    evidence: list[DependencyEvidence] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class StateExclusionRule(BaseModel):
    """A state/category this output's aggregation is believed to EXCLUDE."""

    state_label: str
    confidence: float = 0.3
    evidence: list[DependencyEvidence] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class ScopeDimension(BaseModel):
    """One active scope constraint that qualifies an output's value —
    without matching scope, two observations must never be compared."""

    dimension_id: str = Field(default_factory=new_id)
    dimension_type: str = "unknown"
    value: str = ""
    evidence: list[DependencyEvidence] = Field(default_factory=list)

    @field_validator("dimension_type")
    @classmethod
    def _validate_dimension_type(cls, value: str) -> str:
        if value not in SCOPE_DIMENSION_TYPES:
            raise ValueError(f"Invalid dimension_type: {value!r} (expected one of {sorted(SCOPE_DIMENSION_TYPES)})")
        return value


class TemporalScope(BaseModel):
    label: str = ""
    is_relative: bool = False  # "today"/"this week" vs. an explicit fixed range
    evidence: list[DependencyEvidence] = Field(default_factory=list)


class ActorScope(BaseModel):
    actor_term: Optional[str] = None
    role_term: Optional[str] = None
    evidence: list[DependencyEvidence] = Field(default_factory=list)


class TenantScope(BaseModel):
    tenant_term: Optional[str] = None
    evidence: list[DependencyEvidence] = Field(default_factory=list)


class FilterScope(BaseModel):
    filter_label: str = ""
    filter_value: str = ""
    evidence: list[DependencyEvidence] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Dependency family
# ---------------------------------------------------------------------------


class DependencySource(BaseModel):
    """WHERE a candidate dependency's producing side comes from — an entity,
    a workflow, an actor action, or a state transition. At least one of
    `entity_id`/`workflow_id`/`actor_id`/`transition_id` is expected to be
    set; none are mutually exclusive (a transition always has an entity)."""

    entity_id: Optional[str] = None
    workflow_id: Optional[str] = None
    actor_id: Optional[str] = None
    transition_id: Optional[str] = None
    state_label: Optional[str] = None


class DependencyTarget(BaseModel):
    """WHERE a candidate dependency's consuming side is — a derived output,
    optionally scoped to one chart segment/series."""

    output_id: str
    segment_label: Optional[str] = None


class DependencyEffect(BaseModel):
    """ONE candidate structural effect rule (see task section 7) — e.g.
    "entity creation -> candidate increase in total count." Always a
    candidate until a before/after observation confirms it."""

    effect_id: str = Field(default_factory=new_id)
    direction: str = "unknown"
    trigger_semantic_action: str = ""
    magnitude_hint: Optional[str] = None
    confidence: float = 0.2
    evidence: list[DependencyEvidence] = Field(default_factory=list)

    @field_validator("direction")
    @classmethod
    def _validate_direction(cls, value: str) -> str:
        if value not in EFFECT_DIRECTIONS:
            raise ValueError(f"Invalid effect direction: {value!r} (expected one of {sorted(EFFECT_DIRECTIONS)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class DependencyContradiction(BaseModel):
    """Evidence that conflicts with an otherwise-supported dependency claim
    — retained, never silently dropped."""

    contradiction_id: str = Field(default_factory=new_id)
    description: str = ""
    evidence: list[DependencyEvidence] = Field(default_factory=list)
    confidence: float = 0.3

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class VerificationRequirement(BaseModel):
    """One PLANNED step toward verifying a dependency — never executed by
    this milestone. An ordered list of these forms a verification plan
    (e.g. login as A -> create -> login as B -> transition -> compare)."""

    requirement_id: str = Field(default_factory=new_id)
    step_type: str = "unknown"
    description: str = ""
    actor_term: Optional[str] = None
    entity_term: Optional[str] = None
    ordinal: int = 0

    @field_validator("step_type")
    @classmethod
    def _validate_step_type(cls, value: str) -> str:
        if value not in VERIFICATION_STEP_TYPES:
            raise ValueError(f"Invalid step_type: {value!r} (expected one of {sorted(VERIFICATION_STEP_TYPES)})")
        return value


class DependencyGap(BaseModel):
    gap_id: str = Field(default_factory=new_id)
    dependency_id: Optional[str] = None
    gap_type: str = "unknown"
    description: str = ""
    related_output_ids: list[str] = Field(default_factory=list)
    related_actor_ids: list[str] = Field(default_factory=list)
    related_entity_ids: list[str] = Field(default_factory=list)
    related_workflow_ids: list[str] = Field(default_factory=list)
    related_transition_ids: list[str] = Field(default_factory=list)
    evidence: list[DependencyEvidence] = Field(default_factory=list)
    confidence: float = 0.3
    exploration_value: float = 0.3
    risk: str = "low"
    recommended_investigation_goal: str = ""

    @field_validator("gap_type")
    @classmethod
    def _validate_gap_type(cls, value: str) -> str:
        if value not in GAP_TYPES:
            raise ValueError(f"Invalid gap_type: {value!r} (expected one of {sorted(GAP_TYPES)})")
        return value

    @field_validator("confidence", "exploration_value")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class DependencyCandidate(BaseModel):
    """A single raw, per-observation dependency hypothesis, BEFORE
    reconstruction/merging into a persistent `DependencyDescriptor` — mirrors
    `WorkflowCandidate`'s role in the workflow-discovery pipeline."""

    candidate_id: str = Field(default_factory=new_id)
    dependency_type: str = "unknown"
    relationship_type: str = "unknown_effect"
    source: DependencySource = Field(default_factory=DependencySource)
    target: DependencyTarget
    effect: DependencyEffect = Field(default_factory=DependencyEffect)
    page_url: str = ""
    evidence: DependencyEvidence

    @field_validator("dependency_type")
    @classmethod
    def _validate_dependency_type(cls, value: str) -> str:
        if value not in DEPENDENCY_TYPES:
            raise ValueError(f"Invalid dependency_type: {value!r} (expected one of {sorted(DEPENDENCY_TYPES)})")
        return value

    @field_validator("relationship_type")
    @classmethod
    def _validate_relationship_type(cls, value: str) -> str:
        if value not in RELATIONSHIP_TYPES:
            raise ValueError(f"Invalid relationship_type: {value!r} (expected one of {sorted(RELATIONSHIP_TYPES)})")
        return value


class DependencyRule(BaseModel):
    """A merged, named rule attached to a `DependencyDescriptor` — pairs an
    `AggregationRule` with its inclusion/exclusion state rules, so the full
    "what this output counts" story travels together."""

    rule_id: str = Field(default_factory=new_id)
    aggregation: AggregationRule = Field(default_factory=AggregationRule)
    inclusion_rules: list[StateInclusionRule] = Field(default_factory=list)
    exclusion_rules: list[StateExclusionRule] = Field(default_factory=list)


class DependencyDescriptor(BaseModel):
    """The persistent, merged record of one discovered (candidate or
    verified) dependency between a producing side (entity/workflow/actor/
    transition) and a consuming side (a derived output)."""

    dependency_id: str = Field(default_factory=new_id)
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    dependency_type: str = "unknown"
    status: str = "candidate"
    confidence: float = 0.0
    supporting_evidence: list[DependencyEvidence] = Field(default_factory=list)

    source_actor_ids: list[str] = Field(default_factory=list)
    source_entity_ids: list[str] = Field(default_factory=list)
    source_workflow_ids: list[str] = Field(default_factory=list)
    source_step_ids: list[str] = Field(default_factory=list)
    source_transition_ids: list[str] = Field(default_factory=list)
    target_output_ids: list[str] = Field(default_factory=list)

    relationship_type: str = "unknown_effect"
    effect_direction: str = "unknown"
    aggregation_rule: Optional[AggregationRule] = None
    competing_aggregation_rules: list[AggregationRule] = Field(default_factory=list)
    inclusion_rules: list[StateInclusionRule] = Field(default_factory=list)
    exclusion_rules: list[StateExclusionRule] = Field(default_factory=list)
    scope_dimensions: list[ScopeDimension] = Field(default_factory=list)
    temporal_scope: Optional[TemporalScope] = None
    actor_scope: Optional[ActorScope] = None
    tenant_scope: Optional[TenantScope] = None
    filter_scope: Optional[FilterScope] = None

    prerequisites: list[str] = Field(default_factory=list)
    known_producers: list[str] = Field(default_factory=list)
    known_consumers: list[str] = Field(default_factory=list)
    contradictions: list[DependencyContradiction] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    verification_requirements: list[VerificationRequirement] = Field(default_factory=list)

    # Separate confidence sub-scores (task section 15) — the composite
    # `confidence` above is derived from these, never computed independently.
    relationship_confidence: float = 0.0
    formula_confidence: float = 0.0
    scope_confidence: float = 0.0
    effect_direction_confidence: float = 0.0
    verification_confidence: float = 0.0

    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    observation_count: int = 0
    version: int = 1

    @field_validator("dependency_type")
    @classmethod
    def _validate_dependency_type(cls, value: str) -> str:
        if value not in DEPENDENCY_TYPES:
            raise ValueError(f"Invalid dependency_type: {value!r} (expected one of {sorted(DEPENDENCY_TYPES)})")
        return value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in DEPENDENCY_STATUSES:
            raise ValueError(f"Invalid status: {value!r} (expected one of {sorted(DEPENDENCY_STATUSES)})")
        return value

    @field_validator("relationship_type")
    @classmethod
    def _validate_relationship_type(cls, value: str) -> str:
        if value not in RELATIONSHIP_TYPES:
            raise ValueError(f"Invalid relationship_type: {value!r} (expected one of {sorted(RELATIONSHIP_TYPES)})")
        return value

    @field_validator("effect_direction")
    @classmethod
    def _validate_effect_direction(cls, value: str) -> str:
        if value not in EFFECT_DIRECTIONS:
            raise ValueError(f"Invalid effect_direction: {value!r} (expected one of {sorted(EFFECT_DIRECTIONS)})")
        return value

    @field_validator(
        "confidence", "relationship_confidence", "formula_confidence",
        "scope_confidence", "effect_direction_confidence", "verification_confidence",
    )
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)

    def is_cross_role(self) -> bool:
        return len(set(self.known_producers) | set(self.known_consumers)) >= 2 and bool(
            set(self.known_producers) - set(self.known_consumers)
            or set(self.known_consumers) - set(self.known_producers)
        )

    def requires_actor_switch(self) -> bool:
        return any(p.startswith("required_actor:") for p in self.prerequisites)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "dependency_id": self.dependency_id,
            "canonical_name": self.canonical_name,
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "relationship_type": self.relationship_type,
            "effect_direction": self.effect_direction,
            "source_entity_ids": list(self.source_entity_ids),
            "source_workflow_ids": list(self.source_workflow_ids),
            "target_output_ids": list(self.target_output_ids),
            "known_producers": list(self.known_producers),
            "known_consumers": list(self.known_consumers),
            "is_cross_role": self.is_cross_role(),
        }


class DependencyExecutionObservation(BaseModel):
    """One before/after correlation attempt's raw result — never itself a
    verdict on the underlying dependency, only a data point `DependencyMemory`
    folds in."""

    observation_id: str = Field(default_factory=new_id)
    dependency_id: Optional[str] = None
    result: str = "inconclusive"
    before_value: Optional[float] = None
    after_value: Optional[float] = None
    expected_direction: str = "unknown"
    observed_direction: str = "unknown"
    scope_compatible: bool = True
    delay_iterations: int = 0
    evidence: list[DependencyEvidence] = Field(default_factory=list)
    observed_at_iteration: int = 0

    @field_validator("result")
    @classmethod
    def _validate_result(cls, value: str) -> str:
        if value not in CORRELATION_RESULTS:
            raise ValueError(f"Invalid correlation result: {value!r} (expected one of {sorted(CORRELATION_RESULTS)})")
        return value


class DependencyRegistrySnapshot(BaseModel):
    dependency_count: int = 0
    known: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    high_confidence: list[str] = Field(default_factory=list)
    low_confidence: list[str] = Field(default_factory=list)
    cross_role: list[str] = Field(default_factory=list)
    requiring_actor_switch: list[str] = Field(default_factory=list)
    gap_count: int = 0
    dependencies: list[dict[str, Any]] = Field(default_factory=list)

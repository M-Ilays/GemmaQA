"""Application Knowledge Graph schemas — application-neutral by
construction.

No field here may name a business role, entity, workflow, KPI, or module.
Every `canonical_name`/`node_type`/`edge_type` string is either a fixed
STRUCTURAL vocabulary word (shared by every application: "actor", "has_
permission", "acts_on") or data copied verbatim from an already-discovered
registry record (an entity's own canonical_name, a workflow's own step
verb) — never business vocabulary invented by this code.

This package projects — never rediscovers. Every node/edge here traces
back to an `EntityRegistry`/`ActorRegistry`/`WorkflowRegistry`/
`DependencyRegistry` record; nothing is derived from raw page observations
directly.

Node and edge TYPES are deliberately plain strings, not closed pydantic
enums — section 6/7 of the commissioning task explicitly requires both to
remain "extensible without modifying a closed enum everywhere" and able to
"preserve source terminology" for relationship kinds this milestone didn't
anticimate. The `*_TYPES` constants below are a SUGGESTED, non-exhaustive
vocabulary for documentation/tests, not a validated closed set.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.ids import new_id

# ---------------------------------------------------------------------------
# Suggested (non-exhaustive, non-enforced) vocabularies
# ---------------------------------------------------------------------------

SUGGESTED_NODE_TYPES = frozenset(
    {
        "application", "module", "page", "page_state", "actor", "permission",
        "operation", "entity", "entity_state", "entity_attribute", "workflow",
        "workflow_step", "workflow_branch", "prerequisite", "trigger",
        "outcome", "transition", "derived_output", "metric", "counter",
        "badge", "chart", "report", "queue", "notification", "alert",
        "scope", "temporal_scope", "tenant_scope", "actor_scope",
        "filter_scope", "navigation_region", "form", "table", "api_resource",
        "api_operation", "evidence", "unknown",
    }
)

SUGGESTED_EDGE_TYPES = frozenset(
    {
        # structure
        "contains", "belongs_to", "appears_on", "navigates_to", "exposes", "represents",
        # actor
        "has_permission", "lacks_permission", "can_perform", "cannot_perform",
        "owns", "creates", "updates", "assigns", "approves", "rejects",
        "completes", "consumes", "views", "manages", "participates_in",
        # entity
        "relates_to", "contains", "references", "parent_of", "child_of",
        "assigned_to", "owned_by", "produced_by", "consumed_by", "has_state",
        "has_attribute",
        # workflow
        "has_step", "precedes", "follows", "branches_to", "triggered_by",
        "requires", "blocked_by", "produces", "acts_on", "performed_by",
        "transitions", "hands_off_to", "starts_with", "ends_with",
        # dependency
        "depends_on", "contributes_to", "counts", "sums", "averages",
        "groups_by", "filters_by", "includes_state", "excludes_state",
        "increments", "decrements", "recalculates", "populates",
        "removes_from", "affects", "visible_to", "generated_by", "verified_by",
        # evidence/uncertainty
        "supported_by", "contradicted_by", "inferred_from", "derived_from",
        "equivalent_to", "alias_of", "conflicts_with", "supersedes",
        # CRUD surface discovery (list <-> form <-> record relationships)
        "lists_entity", "creates_entity", "edits_entity", "deletes_entity",
        "opens_create_form", "returns_to_list", "verifies_in_collection",
    }
)

GRAPH_STATUSES = frozenset(
    {"observed", "partially_observed", "inferred", "verified", "candidate", "contradicted", "blocked", "stale", "unknown"}
)

CONSISTENCY_ISSUE_STATUSES = frozenset({"open", "acknowledged", "resolved", "false_positive", "stale"})

GAP_TYPES = frozenset(
    {
        "entity_without_actor", "entity_without_workflow", "entity_state_without_transition",
        "operation_without_actor", "permission_without_operation", "actor_without_confirmed_permission",
        "actor_without_workflow", "workflow_without_actor", "workflow_without_entity",
        "workflow_without_trigger", "workflow_without_outcome", "workflow_with_unresolved_step",
        "unresolved_actor_hand_off", "prerequisite_without_satisfaction_path", "output_without_source",
        "output_without_consumer", "dependency_without_workflow", "dependency_without_entity",
        "dependency_without_verification_path", "isolated_node", "unresolved_identity",
        "unresolved_reference", "contradictory_relationship", "stale_subgraph",
        "low_confidence_bridge", "disconnected_module", "unexplained_derived_output",
    }
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Evidence / provenance
# ---------------------------------------------------------------------------


class GraphEvidenceReference(BaseModel):
    """A pointer to ONE piece of source-registry evidence — never a copy of
    a full evidence payload (screenshots, network bodies, secrets never
    travel through this class)."""

    reference_id: str = Field(default_factory=new_id)
    source_kind: str = ""
    observed_text: str = ""
    page_url: str = ""
    element_id: Optional[str] = None


class GraphProvenance(BaseModel):
    """WHICH source registry record created or last updated this node/edge
    — the answer to "which registry records produced this?" that
    observability must always be able to give."""

    source_registry: str
    source_record_id: str
    source_record_version: int = 0
    synchronized_at_iteration: int = 0
    synchronized_at: datetime = Field(default_factory=datetime.utcnow)


class GraphNodeReference(BaseModel):
    node_id: str
    node_type: str = ""


class GraphEdgeReference(BaseModel):
    edge_id: str
    edge_type: str = ""


# ---------------------------------------------------------------------------
# Scope (copied onto edges, never collapsed)
# ---------------------------------------------------------------------------


class GraphScope(BaseModel):
    temporal_scope: Optional[str] = None
    actor_scope: Optional[str] = None
    tenant_scope: Optional[str] = None
    filter_scope: Optional[str] = None

    def compatible_with(self, other: "GraphScope") -> bool:
        for field_name in ("temporal_scope", "actor_scope", "tenant_scope", "filter_scope"):
            a, b = getattr(self, field_name), getattr(other, field_name)
            if a is not None and b is not None and a != b:
                return False
        return True


# ---------------------------------------------------------------------------
# Node / edge
# ---------------------------------------------------------------------------


class KnowledgeNode(BaseModel):
    node_id: str
    node_type: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    status: str = "observed"
    confidence: float = 0.0
    source_registry: str = ""
    source_record_id: str = ""
    source_record_version: int = 0
    evidence_references: list[GraphEvidenceReference] = Field(default_factory=list)
    provenance: list[GraphProvenance] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    observation_count: int = 1
    graph_version: int = 0
    active: bool = True
    stale: bool = False

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in GRAPH_STATUSES:
            raise ValueError(f"Invalid graph status: {value!r} (expected one of {sorted(GRAPH_STATUSES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class KnowledgeEdge(BaseModel):
    edge_id: str
    edge_type: str
    source_node_id: str
    target_node_id: str
    status: str = "observed"
    confidence: float = 0.0
    source_registry: str = ""
    source_record_ids: list[str] = Field(default_factory=list)
    evidence_references: list[GraphEvidenceReference] = Field(default_factory=list)
    provenance: list[GraphProvenance] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    qualifiers: dict[str, str] = Field(default_factory=dict)
    temporal_scope: Optional[str] = None
    actor_scope: Optional[str] = None
    tenant_scope: Optional[str] = None
    filter_scope: Optional[str] = None
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    observation_count: int = 1
    graph_version: int = 0
    active: bool = True
    stale: bool = False
    contradiction_ids: list[str] = Field(default_factory=list)

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in GRAPH_STATUSES:
            raise ValueError(f"Invalid graph status: {value!r} (expected one of {sorted(GRAPH_STATUSES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)

    def scope(self) -> GraphScope:
        return GraphScope(
            temporal_scope=self.temporal_scope, actor_scope=self.actor_scope,
            tenant_scope=self.tenant_scope, filter_scope=self.filter_scope,
        )


# ---------------------------------------------------------------------------
# Inference / contradictions / gaps
# ---------------------------------------------------------------------------


class GraphInference(BaseModel):
    """Full explanation for ONE inferred edge — the rule that produced it,
    what it was derived from, and why its confidence is what it is. Every
    inferred `KnowledgeEdge` has exactly one of these, keyed by `edge_id`."""

    inference_id: str = Field(default_factory=new_id)
    edge_id: str
    rule_id: str
    input_node_ids: list[str] = Field(default_factory=list)
    input_edge_ids: list[str] = Field(default_factory=list)
    evidence_references: list[GraphEvidenceReference] = Field(default_factory=list)
    confidence_derivation: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    graph_version: int = 0
    invalidated: bool = False
    invalidated_reason: Optional[str] = None


class GraphContradiction(BaseModel):
    """`contradiction_id` defaults to a DETERMINISTIC key derived from the
    exact same (description, node_ids, edge_ids) content `add_contradiction`
    already dedups on -- see `PendingReference`'s docstring above for why a
    random default here would break cross-construction determinism for any
    downstream consumer (Goal Generation's `resolve_contradiction` goals)
    that keys its own identity off this id."""

    contradiction_id: str = ""
    description: str = ""
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    evidence_references: list[GraphEvidenceReference] = Field(default_factory=list)
    confidence: float = 0.3
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)

    @model_validator(mode="after")
    def _default_contradiction_id(self) -> "GraphContradiction":
        if not self.contradiction_id:
            self.contradiction_id = f"contradiction:{self.description}:{sorted(self.node_ids)}:{sorted(self.edge_ids)}"
        return self

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


class GraphConsistencyIssue(BaseModel):
    """`issue_id` defaults to a deterministic key -- see `GraphContradiction`."""

    issue_id: str = ""
    issue_type: str = "unknown"
    severity: str = "low"
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    evidence_references: list[GraphEvidenceReference] = Field(default_factory=list)
    compatible_scope: bool = True
    explanation: str = ""
    status: str = "open"
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    resolution: Optional[str] = None

    @model_validator(mode="after")
    def _default_issue_id(self) -> "GraphConsistencyIssue":
        if not self.issue_id:
            self.issue_id = f"issue:{self.issue_type}:{sorted(self.node_ids)}:{sorted(self.edge_ids)}"
        return self

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in CONSISTENCY_ISSUE_STATUSES:
            raise ValueError(f"Invalid issue status: {value!r} (expected one of {sorted(CONSISTENCY_ISSUE_STATUSES)})")
        return value


class GraphGap(BaseModel):
    """`gap_id` defaults to a deterministic key -- see `GraphContradiction`."""

    gap_id: str = ""
    gap_type: str = "unknown"
    description: str = ""
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    related_registry_records: list[str] = Field(default_factory=list)
    evidence_references: list[GraphEvidenceReference] = Field(default_factory=list)
    confidence: float = 0.3
    risk: str = "low"
    exploration_value: float = 0.3
    blocking: bool = False
    recommended_future_goal_type: str = ""
    status: str = "open"
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)

    @model_validator(mode="after")
    def _default_gap_id(self) -> "GraphGap":
        if not self.gap_id:
            self.gap_id = f"gap:{self.gap_type}:{sorted(self.node_ids)}"
        return self

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


# ---------------------------------------------------------------------------
# Query / traversal / paths / subgraphs
# ---------------------------------------------------------------------------


class GraphTraversalConstraint(BaseModel):
    max_depth: int = 3
    allowed_node_types: Optional[list[str]] = None
    allowed_edge_types: Optional[list[str]] = None
    min_confidence: float = 0.0
    allowed_statuses: Optional[list[str]] = None
    required_scope: Optional[GraphScope] = None
    include_stale: bool = False
    include_inferred: bool = True
    max_results: int = 200


class GraphQuery(BaseModel):
    query_type: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    constraint: GraphTraversalConstraint = Field(default_factory=GraphTraversalConstraint)


class GraphQueryResult(BaseModel):
    query_type: str
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    total_before_truncation: int = 0


class KnowledgePath(BaseModel):
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    length: int = 0


class KnowledgeSubgraph(BaseModel):
    focus_node_id: Optional[str] = None
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)


class GraphNeighbourhood(BaseModel):
    center_node_id: str
    depth: int
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    truncated: bool = False


# ---------------------------------------------------------------------------
# Statistics / snapshot / version
# ---------------------------------------------------------------------------


class GraphStatistics(BaseModel):
    total_nodes: int = 0
    total_edges: int = 0
    node_counts_by_type: dict[str, int] = Field(default_factory=dict)
    edge_counts_by_type: dict[str, int] = Field(default_factory=dict)
    observed_edge_count: int = 0
    inferred_edge_count: int = 0
    contradicted_edge_count: int = 0
    stale_edge_count: int = 0
    unresolved_reference_count: int = 0
    consistency_issue_count: int = 0
    gap_count: int = 0
    connected_component_count: int = 0
    isolated_node_count: int = 0
    graph_version: int = 0


class GraphVersion(BaseModel):
    graph_version: int = 0
    source_registry_versions: dict[str, int] = Field(default_factory=dict)
    synchronized_at: datetime = Field(default_factory=datetime.utcnow)
    change_summary: str = ""
    added_node_ids: list[str] = Field(default_factory=list)
    updated_node_ids: list[str] = Field(default_factory=list)
    stale_node_ids: list[str] = Field(default_factory=list)
    added_edge_ids: list[str] = Field(default_factory=list)
    updated_edge_ids: list[str] = Field(default_factory=list)
    stale_edge_ids: list[str] = Field(default_factory=list)
    resolved_reference_ids: list[str] = Field(default_factory=list)
    new_contradiction_ids: list[str] = Field(default_factory=list)
    resolved_contradiction_ids: list[str] = Field(default_factory=list)
    new_gap_ids: list[str] = Field(default_factory=list)
    resolved_gap_ids: list[str] = Field(default_factory=list)


class KnowledgeGraphSnapshot(BaseModel):
    graph_version: int = 0
    statistics: GraphStatistics = Field(default_factory=GraphStatistics)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    consistency_issues: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Unresolved references (pending relationship storage)
# ---------------------------------------------------------------------------


class PendingReference(BaseModel):
    """A relationship whose TARGET node doesn't exist yet — e.g. a workflow
    step names an actor before the Actor Registry has confirmed it. Resolved
    later without duplicating the edge once the target becomes known.

    `reference_id` defaults to a DETERMINISTIC key derived from
    (source_node_id, edge_type, target_hint) rather than a random id --
    the exact same tuple `add_pending_reference` already dedups on. A
    random default here means two structurally-identical graphs (e.g. the
    same fixture built twice) would mint different reference ids, which a
    downstream consumer (Goal Generation's `resolve_unresolved_reference`
    goals key their own identity off this id) would then see as different
    goal ids for the same investigation -- breaking determinism one layer
    up. Found via Scenario Planning's stricter cross-construction identity
    test; see docs/SCENARIO_PLANNING_ENGINE.md."""

    reference_id: str = ""
    source_node_id: str
    edge_type: str
    target_hint: str
    target_node_type: str = ""
    reason: str = ""
    evidence_references: list[GraphEvidenceReference] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    resolved: bool = False
    resolved_edge_id: Optional[str] = None
    resolved_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _default_reference_id(self) -> "PendingReference":
        if not self.reference_id:
            self.reference_id = f"pending:{self.source_node_id}:{self.edge_type}:{self.target_hint}"
        return self

"""Canonical application structure models (single source of truth)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ExplorationStatus(str, Enum):
    DISCOVERED = "discovered"
    EXPLORED = "explored"
    BLOCKED = "blocked"


class ScenarioGenerationStatus(str, Enum):
    GENERATED = "generated"
    SCHEDULED = "scheduled"


class ScenarioExecutionStatus(str, Enum):
    NOT_RUN = "not_run"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    # Additive: a verified-but-not-conclusively-supported-or-contradicted
    # result from AutonomousInvestigationEngine's assertion verification
    # (see intelligence.autonomous_investigation.assertion_verifier), and a
    # result actively contradicted by observed evidence — distinct from a
    # plain FAILED (a step/action error) per app.agent.form_lifecycle's same
    # observed/inferred-never-conflated discipline.
    INCONCLUSIVE = "inconclusive"
    CONTRADICTED = "contradicted"


class AppModule(BaseModel):
    id: str
    run_id: str
    canonical_key: str
    name: str
    purpose: str = ""
    parent_module_id: Optional[str] = None
    source: str = "route"  # nav | breadcrumb | route | heading | gemma
    confidence: float = 0.5
    status: str = "active"
    page_ids: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class AppPage(BaseModel):
    id: str
    run_id: str
    module_id: Optional[str] = None
    canonical_url: str
    normalized_path: str = "/"
    title: str = ""
    heading: str = ""
    page_type: str = "unknown"
    fingerprint: str = ""
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    visit_count: int = 1
    exploration_status: ExplorationStatus = ExplorationStatus.DISCOVERED
    screenshot_evidence_id: Optional[str] = None


class AppNavigationEdge(BaseModel):
    id: str
    run_id: str
    from_page_id: str
    to_page_id: str
    action_type: str = "click"
    action_label: str = ""
    element_id: Optional[str] = None
    reason: str = ""
    occurrence_count: int = 1
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)


class AppFormField(BaseModel):
    id: str
    form_id: str
    stable_field_key: str
    label: str = ""
    input_type: str = "text"
    required: bool = False
    constraints: dict[str, Any] = Field(default_factory=dict)


class AppForm(BaseModel):
    id: str
    run_id: str
    page_id: str
    fingerprint: str
    form_name: str = ""
    method: str = ""
    action: str = ""
    inspected: bool = False
    tested: bool = False
    lifecycle_state: str = "discovered"
    # CRUD surface discovery — see app.perception.form_intent_classifier /
    # app.agent.form_lifecycle.FormLifecycle. `intent` is one of
    # FORM_INTENTS ("search"/"filter"/"create"/"edit"/"delete_confirmation"/
    # "bulk_action"/"upload"/"settings"/"authentication"/"unknown"); empty
    # string means "not yet classified" (still at lifecycle DISCOVERED).
    intent: str = ""
    intent_confidence: float = 0.0
    target_entity_hypothesis: Optional[str] = None
    operation_hypothesis: Optional[str] = None
    intent_evidence: list[str] = Field(default_factory=list)
    # Rejected-but-plausible alternative intents, never silently dropped —
    # each entry is "intent:confidence:reason".
    intent_alternatives: list[str] = Field(default_factory=list)
    # Populated only when this form never reached CANDIDATE_FOR_TESTING/
    # TESTED — one of app.agent.form_lifecycle.NOT_TESTED_REASONS. None
    # while the form is still active/testable.
    not_tested_reason: Optional[str] = None
    fields: list[AppFormField] = Field(default_factory=list)


class AppTestScenario(BaseModel):
    id: str
    run_id: str
    page_id: Optional[str] = None
    form_id: Optional[str] = None
    field_id: Optional[str] = None
    scenario_key: str
    title: str
    category: str = "exploratory"
    description: str = ""
    steps: list[str] = Field(default_factory=list)
    expected_results: list[str] = Field(default_factory=list)
    generation_status: ScenarioGenerationStatus = ScenarioGenerationStatus.GENERATED
    execution_status: ScenarioExecutionStatus = ScenarioExecutionStatus.NOT_RUN
    notes: str = ""


class AppEvidence(BaseModel):
    id: str
    run_id: str
    kind: str = "screenshot"
    relative_path: str = ""
    public_url: str = ""
    description: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Internal only — never serialize to reports/APIs by default
    absolute_path: Optional[str] = Field(default=None, exclude=True)


class ExternalReference(BaseModel):
    id: str
    run_id: str
    source_page_id: Optional[str] = None
    url: str
    label: str = ""
    category: str = "external"  # docs | social | support | other


class ApplicationModel(BaseModel):
    """Single source of truth for discovered application structure."""

    run_id: str
    root_url: str
    allowed_origin: str
    application_name: str = "Unknown Application"
    inferred_domain: str = ""
    purpose: str = ""
    purpose_confidence: float = 0.0
    purpose_evidence: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    modules: list[AppModule] = Field(default_factory=list)
    pages: list[AppPage] = Field(default_factory=list)
    navigation_edges: list[AppNavigationEdge] = Field(default_factory=list)
    forms: list[AppForm] = Field(default_factory=list)
    scenarios: list[AppTestScenario] = Field(default_factory=list)
    evidence: list[AppEvidence] = Field(default_factory=list)
    external_references: list[ExternalReference] = Field(default_factory=list)
    # Same-origin URLs seen as links but not yet visited — not Page inventory rows
    candidate_urls: list[str] = Field(default_factory=list)

    def visited_pages(self) -> list[AppPage]:
        return [p for p in self.pages if p.visit_count > 0]

    def page_by_url(self, canonical_url: str) -> Optional[AppPage]:
        for p in self.pages:
            if p.canonical_url == canonical_url:
                return p
        return None

    def page_by_id(self, page_id: str) -> Optional[AppPage]:
        for p in self.pages:
            if p.id == page_id:
                return p
        return None

    def module_by_key(self, key: str) -> Optional[AppModule]:
        k = key.lower()
        for m in self.modules:
            if m.canonical_key == k or k in {a.lower() for a in m.aliases}:
                return m
        return None

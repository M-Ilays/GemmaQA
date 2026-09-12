"""Pydantic schemas for GemmaQA API and agent contracts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunStatusEnum(str, Enum):
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
    PAUSED = "paused"
    # Legacy aliases (older API / frontend clients)
    PENDING = "pending"
    LOGGING_IN = "logging_in"
    EXPLORING = "exploring"
    TESTING = "testing"
    STOPPED = "stopped"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionCategory(str, Enum):
    # Primary categories for Gemma action selection
    EXPLORATION = "exploration"
    FORM_INSPECTION = "form_inspection"
    NEGATIVE_TEST = "negative_test"
    BOUNDARY_TEST = "boundary_test"
    NAVIGATION_TEST = "navigation_test"
    EVIDENCE_CAPTURE = "evidence_capture"
    COMPLETION = "completion"
    # Legacy aliases kept for older callers / stored JSON
    INSPECTION = "inspection"
    FORM_INTERACTION = "form_interaction"
    NAVIGATION = "navigation"
    VERIFICATION = "verification"
    EVIDENCE = "evidence"
    CONTROL = "control"


class ActionType(str, Enum):
    OPEN_URL = "open_url"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    UNCHECK = "uncheck"
    PRESS = "press"
    HOVER = "hover"
    GO_BACK = "go_back"
    REFRESH = "refresh"
    WAIT = "wait"
    INSPECT_FORM = "inspect_form"
    INSPECT_TABLE = "inspect_table"
    OPEN_TAB = "open_tab"
    TAKE_SCREENSHOT = "take_screenshot"
    FINISH = "finish"


class DefectSeverity(str, Enum):
    BLOCKER = "blocker"
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    TRIVIAL = "trivial"


class DefectStatus(str, Enum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    FALSE_POSITIVE = "false_positive"
    FIXED = "fixed"


# ---------------------------------------------------------------------------
# Configuration / Run requests
# ---------------------------------------------------------------------------


class RunConfiguration(BaseModel):
    """Runtime knobs for an exploratory QA run.

    `max_actions` / `max_pages` / `max_runtime_seconds` are accepted for
    backward compatibility with stored runs and older clients. They are not
    limits: the agent does not stop because an action count, page count, or
    elapsed runtime was reached.
    """

    max_pages: Optional[int] = Field(default=None)
    max_actions: Optional[int] = Field(default=None)
    max_runtime_seconds: Optional[int] = Field(default=None)
    max_screenshots: int = Field(default=200, ge=1, le=2000)
    max_retries: int = Field(default=3, ge=0, le=20)
    safe_mode: bool = True
    allow_controlled_writes: bool = False
    allow_login: bool = True
    allow_test_account_creation: bool = True
    allow_safe_test_data_creation: bool = False
    allow_destructive_actions: bool = False
    allow_financial_actions: bool = False
    headless: bool = True
    allow_cross_domain: bool = False
    allow_subdomains: bool = False
    login_url: Optional[str] = None
    username_selector: Optional[str] = None
    password_selector: Optional[str] = None
    submit_selector: Optional[str] = None
    wait_after_login_ms: int = 2000
    enable_autonomous_investigation: bool = True
    # What the operator actually asked GemmaQA to find out, in their own words.
    # `None` means "not provided" and is deliberately distinct from `""` ("the
    # operator explicitly cleared it"): docs/MODEL_CONTEXT_AUDIT.md J-3 found
    # this value never reaching the model at all, replaced by a hardcoded
    # literal. Substituting a generic objective for a missing one is exactly the
    # failure that hid — the model cannot tell an invented instruction from a
    # real one, so absence is carried through as absence.
    testing_objective: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Operator's testing objective in their own words; null when not provided.",
    )


class CreateRunRequest(BaseModel):
    """Payload to create a new QA run (prepared; call /start to execute)."""

    url: str = Field(..., min_length=1, description="Authorized target website URL")
    username: Optional[str] = None
    password: Optional[str] = None
    configuration: RunConfiguration = Field(default_factory=RunConfiguration)
    notes: Optional[str] = None
    auto_start: bool = Field(
        default=False,
        description="If true, start the run immediately after create",
    )
    authorization_ack: bool = Field(
        default=False,
        description="Must be true: only test systems you own or are authorized to test",
    )


class ExecutionPacingUpdate(BaseModel):
    """Runtime operator pacing. Affects the next browser action only."""

    execution_speed: Optional[float] = None
    action_pause: Optional[float] = None


class RunStatus(BaseModel):
    """Current status snapshot for a QA run."""

    run_id: str
    status: RunStatusEnum
    url: str
    current_url: Optional[str] = None
    pages_visited: int = 0
    actions_taken: int = 0
    bugs_found: int = 0
    progress_pct: float = 0.0
    message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    notes: Optional[str] = None
    username: Optional[str] = None
    configuration: RunConfiguration = Field(default_factory=RunConfiguration)
    execution_speed: float = 1.0
    action_pause: float = 0.0


class RunEvent(BaseModel):
    """Sequenced run event for REST history and WebSocket streaming."""

    event_id: str
    run_id: str
    event_type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    sequence_number: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class PaginatedRuns(BaseModel):
    items: list[RunStatus]
    total: int
    page: int
    page_size: int
    total_pages: int


class DeleteRunResponse(BaseModel):
    ok: bool
    run_id: str
    message: str


# ---------------------------------------------------------------------------
# Browser / page observation
# ---------------------------------------------------------------------------


class LocatorStrategy(BaseModel):
    """How Playwright should resolve an element_id on the current page."""

    kind: str
    selector: Optional[str] = None
    role: Optional[str] = None
    name: Optional[str] = None
    text: Optional[str] = None
    nth: Optional[int] = None
    fallback_selectors: list[str] = Field(default_factory=list)


class InteractiveElement(BaseModel):
    element_id: str
    tag: str
    role: Optional[str] = None
    type: Optional[str] = None  # alias of input_type for compatibility
    name: Optional[str] = None
    id_attr: Optional[str] = None
    text: Optional[str] = None  # alias of visible_text
    aria_label: Optional[str] = None
    href: Optional[str] = None
    placeholder: Optional[str] = None
    title: Optional[str] = None
    is_visible: bool = True
    is_enabled: bool = True
    bounding_box: Optional[dict[str, float]] = None
    selector_hint: Optional[str] = None
    # Rich descriptors
    accessible_name: Optional[str] = None
    visible_text: Optional[str] = None
    label: Optional[str] = None
    input_type: Optional[str] = None
    required: bool = False
    disabled: bool = False
    checked: Optional[bool] = None
    current_value: Optional[str] = None
    available_options: list[str] = Field(default_factory=list)
    locator_strategy: Optional[LocatorStrategy] = None
    category: Optional[str] = None  # button|link|input|select|checkbox|radio|file|tab|other

    # --- Generic perception fields (docs/PAGE_PERCEPTION_AUDIT.md / Canonical Page
    # Model) — all additive and optional so existing construction/usage is
    # unaffected. See docs/CANONICAL_PAGE_MODEL.md for the full field rationale.
    stable_id: Optional[str] = None
    parent_region_id: Optional[str] = None
    source: str = "dom"  # dom | aria | heuristic | network | inferred
    accessible_description: Optional[str] = None
    attributes: dict[str, str] = Field(default_factory=dict)
    is_expanded: Optional[bool] = None  # aria-expanded
    is_selected: Optional[bool] = None  # aria-selected / aria-current
    is_external_url: Optional[bool] = None
    confidence: float = 1.0
    evidence: list[str] = Field(default_factory=list)
    status: str = "observed"  # observed | stale | inferred | unknown
    fingerprint_contribution: Optional[str] = None


class FormField(BaseModel):
    name: Optional[str] = None
    field_type: str = "text"
    label: Optional[str] = None
    required: bool = False
    placeholder: Optional[str] = None
    options: list[str] = Field(default_factory=list)
    element_id: Optional[str] = None
    disabled: bool = False
    current_value: Optional[str] = None


class FormFieldDescriptor(BaseModel):
    """Richer, generic per-field descriptor for the Canonical Page Model.

    A new, additive companion to `FormField` (kept as-is for backward
    compatibility with every existing observer/frontier code path) — this is
    what `FormDescriptor.field_descriptors` carries when populated. Not yet
    produced by the observer; see docs/CANONICAL_PAGE_MODEL.md.
    """

    stable_id: str
    element_id: Optional[str] = None
    parent_region_id: Optional[str] = None
    source: str = "dom"
    text: Optional[str] = None
    accessible_name: Optional[str] = None
    accessible_description: Optional[str] = None
    dom_tag: Optional[str] = None
    aria_role: Optional[str] = None
    attributes: dict[str, str] = Field(default_factory=dict)
    field_type: str = "text"
    label: Optional[str] = None
    placeholder: Optional[str] = None
    options: list[str] = Field(default_factory=list)
    current_value: Optional[str] = None
    is_visible: bool = True
    is_enabled: bool = True
    is_required: bool = False
    is_checked: Optional[bool] = None
    bounding_box: Optional[dict[str, float]] = None
    confidence: float = 1.0
    evidence: list[str] = Field(default_factory=list)
    status: str = "observed"
    fingerprint_contribution: Optional[str] = None
    # Richer control-shape classification (combobox/autocomplete/multi_select/
    # date_picker/radio_group/checkbox_group/file_upload/text/unknown) — see
    # app.perception.form_extractor._infer_field_kind. None means "not yet
    # classified" (a producer other than form_extractor built this field).
    field_kind: Optional[str] = None
    # Constraint-inference signals — see app.agent.field_constraint_inference.
    # None means "attribute absent on the DOM element", never "unknown".
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    step: Optional[float] = None
    # Raw string echo of the min/max attribute, always populated when the
    # attribute is present -- for date-typed inputs `min`/`max` are ISO date
    # strings that never parse as `min_value`/`max_value` (those stay None
    # for a date field); see app.agent.field_constraint_inference, which
    # reads these instead of min_value/max_value for date fields.
    min_value_raw: Optional[str] = None
    max_value_raw: Optional[str] = None


class FormDescriptor(BaseModel):
    form_id: str
    action: Optional[str] = None
    method: Optional[str] = None
    fields: list[FormField] = Field(default_factory=list)
    submit_element_id: Optional[str] = None

    # Generic perception fields — additive/optional, see InteractiveElement above.
    stable_id: Optional[str] = None
    parent_region_id: Optional[str] = None
    source: str = "dom"
    bounding_box: Optional[dict[str, float]] = None
    confidence: float = 1.0
    evidence: list[str] = Field(default_factory=list)
    status: str = "observed"
    fingerprint_contribution: Optional[str] = None
    # Richer, optional companion to `fields` — see FormFieldDescriptor. Empty by
    # default; populated only where a producer chooses to build it.
    field_descriptors: list[FormFieldDescriptor] = Field(default_factory=list)


class TableColumnDescriptor(BaseModel):
    stable_id: str
    column_index: int = 0
    header_text: Optional[str] = None
    accessible_name: Optional[str] = None
    data_type: Optional[str] = None
    confidence: float = 1.0
    evidence: list[str] = Field(default_factory=list)
    status: str = "observed"


class TableRowDescriptor(BaseModel):
    stable_id: str
    row_index: int = 0
    cell_values: list[str] = Field(default_factory=list)
    # Element ids of any per-row action controls (edit/delete/view/...) found
    # within this row — the audit's "row actions are never linked to their row"
    # gap, made representable (not yet populated by the observer).
    row_action_element_ids: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    evidence: list[str] = Field(default_factory=list)
    status: str = "observed"


class TableDescriptor(BaseModel):
    table_id: str
    headers: list[str] = Field(default_factory=list)
    row_count: int = 0
    sample_rows: list[list[str]] = Field(default_factory=list)

    # Generic perception fields — additive/optional, see InteractiveElement above.
    stable_id: Optional[str] = None
    parent_region_id: Optional[str] = None
    source: str = "dom"
    bounding_box: Optional[dict[str, float]] = None
    confidence: float = 1.0
    evidence: list[str] = Field(default_factory=list)
    status: str = "observed"
    fingerprint_contribution: Optional[str] = None
    # Richer, optional structure alongside headers/sample_rows — empty by default.
    columns: list[TableColumnDescriptor] = Field(default_factory=list)
    rows: list[TableRowDescriptor] = Field(default_factory=list)


class ConsoleEntry(BaseModel):
    level: str
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    url: Optional[str] = None
    source: Optional[str] = None


class NetworkEntry(BaseModel):
    method: str
    url: str
    status: Optional[int] = None
    resource_type: Optional[str] = None
    failed: bool = False
    failure_text: Optional[str] = None
    timing_ms: Optional[float] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class PageClassification(BaseModel):
    page_type: str = "unknown"
    confidence: float = 0.0
    purpose: Optional[str] = None
    module_guess: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class PageState(BaseModel):
    page_id: str
    url: str
    title: str = ""
    headings: list[str] = Field(default_factory=list)
    visible_text_summary: str = ""
    breadcrumbs: list[str] = Field(default_factory=list)
    navigation_items: list[str] = Field(default_factory=list)
    interactive_elements: list[InteractiveElement] = Field(default_factory=list)
    forms: list[FormDescriptor] = Field(default_factory=list)
    tables: list[TableDescriptor] = Field(default_factory=list)
    tabs: list[str] = Field(default_factory=list)
    dialogs: list[str] = Field(default_factory=list)
    modals: list[str] = Field(default_factory=list)
    toasts: list[str] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)
    pagination_controls: list[str] = Field(default_factory=list)
    search_fields: list[str] = Field(default_factory=list)
    filter_controls: list[str] = Field(default_factory=list)
    disabled_controls: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    network_failures: list[str] = Field(default_factory=list)
    console_entries: list[ConsoleEntry] = Field(default_factory=list)
    network_entries: list[NetworkEntry] = Field(default_factory=list)
    screenshot_path: Optional[str] = None
    state_fingerprint: Optional[str] = None
    classification: Optional[PageClassification] = None
    captured_at: datetime = Field(default_factory=datetime.utcnow)


class LoginResult(BaseModel):
    success: bool
    method: Literal["selectors", "auto", "skipped"] = "skipped"
    message: str = ""
    before_url: Optional[str] = None
    after_url: Optional[str] = None
    screenshot_path: Optional[str] = None
    detected_selectors: dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class BrowserAction(BaseModel):
    action: ActionType
    element_id: Optional[str] = None
    value: Optional[str] = None
    reason: str = ""
    expected_result: str = ""
    risk: RiskLevel = RiskLevel.LOW
    category: ActionCategory = ActionCategory.EXPLORATION
    url: Optional[str] = None
    key: Optional[str] = None
    wait_ms: Optional[int] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionResult(BaseModel):
    action_id: str
    run_id: str
    action: BrowserAction
    success: bool
    message: str = ""
    before_url: Optional[str] = None
    after_url: Optional[str] = None
    before_screenshot: Optional[str] = None
    after_screenshot: Optional[str] = None
    before_fingerprint: Optional[str] = None
    after_fingerprint: Optional[str] = None
    page_state_changed: bool = False
    new_console_errors: list[str] = Field(default_factory=list)
    new_network_errors: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    inspected: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Workflows / tests / defects
# ---------------------------------------------------------------------------


class WorkflowStep(BaseModel):
    step_id: str
    order: int
    action: str
    description: str
    expected: Optional[str] = None
    page_url: Optional[str] = None
    element_id: Optional[str] = None
    evidence_ids: list[str] = Field(default_factory=list)


class Workflow(BaseModel):
    workflow_id: str
    name: str
    description: str = ""
    starting_page: Optional[str] = None
    preconditions: list[str] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(default_factory=list)
    alternative_paths: list[str] = Field(default_factory=list)
    error_paths: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    mermaid: str = ""
    page_ids: list[str] = Field(default_factory=list)


class TestScenario(BaseModel):
    test_id: str
    title: str
    description: str = ""
    category: str = "exploratory"
    priority: str = "medium"
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    expected_results: list[str] = Field(default_factory=list)
    related_workflow_id: Optional[str] = None


class TestExecution(BaseModel):
    execution_id: str
    test_id: str
    run_id: str
    status: Literal["passed", "failed", "blocked", "skipped", "not_run"] = "not_run"
    notes: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    executed_at: Optional[datetime] = None


class EvidenceItem(BaseModel):
    evidence_id: str
    run_id: str
    kind: Literal["screenshot", "trace", "console", "network", "dom", "other"] = "screenshot"
    path: str
    description: str = ""
    page_id: Optional[str] = None
    action_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Defect(BaseModel):
    bug_id: str
    run_id: str
    title: str
    description: str
    severity: DefectSeverity = DefectSeverity.MAJOR
    status: DefectStatus = DefectStatus.OPEN
    steps_to_reproduce: list[str] = Field(default_factory=list)
    expected: str = ""
    actual: str = ""
    page_url: Optional[str] = None
    evidence_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    module: Optional[str] = None
    page_title: Optional[str] = None
    classification: Optional[str] = None
    priority: Optional[str] = None
    preconditions: list[str] = Field(default_factory=list)
    test_data: Optional[str] = None
    business_impact: Optional[str] = None
    possible_root_cause: Optional[str] = None
    confidence: Optional[float] = None


class BugClassification(str, Enum):
    CONFIRMED_BUG = "confirmed_bug"
    SUSPECTED_BUG = "suspected_bug"
    OBSERVATION = "observation"
    # "I looked and there is nothing to report." Added because there was no way
    # to say it: `OBSERVATION` meant both "a noteworthy non-defect" and "no
    # defect at all", and the prompt asked the model to signal the latter with a
    # LOW CONFIDENCE value. A real model does the opposite — it is highly
    # confident the page is fine — so the confidence gate read that certainty as
    # certainty that a finding was real. Live on Nova Pro: 27 of 32 recorded
    # "defects" were titled "No defects detected on <page>".
    NO_DEFECT = "no_defect"


class BugAnalysisResult(BaseModel):
    """Structured Gemma bug analysis (possible_root_cause is a hypothesis only)."""

    classification: BugClassification = BugClassification.OBSERVATION
    title: str = ""
    module: str = ""
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    priority: Literal["urgent", "high", "medium", "low"] = "medium"
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    expected_result: str = ""
    actual_result: str = ""
    business_impact: str = ""
    possible_root_cause: str = ""
    confidence: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)
    page_url: Optional[str] = None
    test_data: Optional[str] = None


class BugReportEntry(BaseModel):
    """Canonical bug report row for docs / CSV (no secrets)."""

    bug_id: str
    title: str
    module: str = ""
    page: str = ""
    url: str = ""
    classification: str = "suspected_bug"
    severity: str = "medium"
    priority: str = "medium"
    preconditions: list[str] = Field(default_factory=list)
    test_data: str = ""
    steps_to_reproduce: list[str] = Field(default_factory=list)
    expected_result: str = ""
    actual_result: str = ""
    business_impact: str = ""
    possible_root_cause_hypothesis: str = ""
    confidence: float = 0.0
    screenshot_evidence: list[str] = Field(default_factory=list)
    trace_evidence: list[str] = Field(default_factory=list)
    console_evidence: list[str] = Field(default_factory=list)
    network_evidence: list[str] = Field(default_factory=list)
    discovery_timestamp: Optional[datetime] = None
    run_id: str = ""


# ---------------------------------------------------------------------------
# Documentation models
# ---------------------------------------------------------------------------


class ProductOverview(BaseModel):
    run_id: str
    product_name: str = "Unknown Application"
    summary: str = ""
    primary_purpose: str = ""
    target_users: list[str] = Field(default_factory=list)
    key_features: list[str] = Field(default_factory=list)
    tech_observations: list[str] = Field(default_factory=list)


class ModuleRecord(BaseModel):
    module_id: str
    name: str
    description: str = ""
    entry_urls: list[str] = Field(default_factory=list)
    page_ids: list[str] = Field(default_factory=list)


class RoleObservation(BaseModel):
    role_name: str
    observed_capabilities: list[str] = Field(default_factory=list)
    restricted_areas: list[str] = Field(default_factory=list)
    notes: str = ""


class CoverageDimension(BaseModel):
    """One coverage axis (navigation, module, form_inspection, ...), reported with
    its own discovered/observed/inspected/attempted/completed/blocked/unknown
    counts — never collapsed into one misleading percentage. `unknown` counts
    same-origin regions known to exist (e.g. an unvisited candidate URL) but never
    reached; a dimension with unknown > 0 must never report pct_complete as 100."""

    name: str
    discovered: int = 0
    observed: int = 0
    inspected: int = 0
    attempted: int = 0
    completed: int = 0
    blocked: int = 0
    unknown: int = 0
    pct_complete: float = 0.0
    available: bool = True
    notes: str = ""


class CoverageRecord(BaseModel):
    run_id: str
    pages_discovered: int = 0
    pages_explored: int = 0
    navigation_items_discovered: int = 0
    navigation_items_used: int = 0
    forms_discovered: int = 0
    forms_inspected: int = 0
    forms_tested: int = 0
    tables_discovered: int = 0
    tables_inspected: int = 0
    workflows_identified: int = 0
    tests_generated: int = 0
    tests_executed: int = 0
    passed: int = 0
    failed: int = 0
    # CRUD workflow inference coverage (app.intelligence.crud_discovery) —
    # separate from tests_generated/tests_executed above, which count the
    # OLDER form/field-driven AppTestScenario generator. A hypothesis is
    # "executed" once AutonomousInvestigationEngine has actually driven a
    # corroborating action for it (status supported/executed/verified),
    # never merely because a control was observed to exist.
    crud_operations_discovered: int = 0
    crud_operations_executed: int = 0
    crud_operations_verified: int = 0
    # Per-operation CRUD breakdown -- "update" here reads from crud_discovery's
    # internal "edit" operation (see app.intelligence.crud_discovery.CRUD_OPERATIONS
    # = {create, edit, delete}; there is no distinct "read" operation there).
    # "read" coverage instead reads collections_discovered/collections_inspected
    # below: a record-collection actually being observed/reasoned about IS the
    # read/view operation for CRUD purposes on a page that lists records.
    crud_create_discovered: int = 0
    crud_create_executed: int = 0
    crud_read_discovered: int = 0
    crud_read_executed: int = 0
    crud_update_discovered: int = 0
    crud_update_executed: int = 0
    crud_delete_discovered: int = 0
    crud_delete_executed: int = 0
    # Autonomous Investigation assertion-verification coverage — distinct
    # from "tests_executed": a scenario can execute every step and still
    # yield an inconclusive/contradicted verification outcome. This IS the
    # "verification coverage" metric.
    assertions_evaluated: int = 0
    assertions_supported: int = 0
    assertions_contradicted: int = 0
    assertions_inconclusive: int = 0
    # Universal record-collection (grid/table/card-list) coverage — see
    # app.perception.models.RecordCollection. "Inspected" means GemmaQA
    # reasoned about the collection's CRUD surface (it produced at least one
    # CRUDWorkflowHypothesis or a temporary record was tied to it), never
    # merely that the collection was rendered on a visited page.
    collections_discovered: int = 0
    collections_inspected: int = 0
    # Interactive, non-navigating controls (buttons/inputs/toggles with no
    # `href`) on visited pages, vs. how many GemmaQA actually acted on this
    # run -- distinct from page-level navigation coverage above.
    local_controls_discovered: int = 0
    local_controls_exercised: int = 0
    # Goal Generation / Scenario Planning / QA Strategy / Autonomous
    # Investigation pipeline counts (app.intelligence.*) -- distinct from
    # tests_generated/tests_executed above, which count the OLDER
    # form/field-driven AppTestScenario generator (app.agent.tester.Tester).
    goals_generated: int = 0
    scenarios_generated: int = 0
    scenarios_executable: int = 0
    scenarios_executed: int = 0
    scenarios_passed: int = 0
    scenarios_failed: int = 0
    scenarios_blocked: int = 0
    # Temporary Record Registry cleanup coverage (app.agent.temporary_record_registry).
    cleanup_pending: int = 0
    cleanup_succeeded: int = 0
    cleanup_failed: int = 0
    cleanup_manual_required: int = 0
    bugs_found: int = 0
    suspected_issues: int = 0
    observations: int = 0
    action_budget_used: int = 0
    action_budget_total: int = 0
    observed_coverage_pct: float = 0.0
    explored_coverage_pct: float = 0.0
    executed_coverage_pct: float = 0.0
    local_control_coverage_pct: float = 0.0
    collection_coverage_pct: float = 0.0
    scenario_execution_coverage_pct: float = 0.0
    cleanup_coverage_pct: float = 0.0
    # Per-dimension breakdown (navigation, module, submodule, form_inspection,
    # safe_form_execution, workflow, candidate, goal, role, page_state) — the
    # single-number *_coverage_pct fields above are kept for backward compatibility,
    # but must never be read as "the" coverage figure; see CoverageDimension.
    dimensions: list[CoverageDimension] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)
    unexplored_areas: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "Coverage percentages reflect observed exploratory activity only "
        "and do not claim complete application coverage."
    )


class FinalReport(BaseModel):
    """Structured synchronized QA report — memory is the source of truth."""

    run_id: str
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    target_url: str = ""
    # What the operator asked for, verbatim; None when they did not say.
    testing_objective: Optional[str] = None
    executive_summary: str = ""
    product_overview: Optional[ProductOverview] = None
    inferred_business_domain: str = ""
    application_purpose: str = ""
    modules: list[ModuleRecord] = Field(default_factory=list)
    navigation_structure: str = ""
    page_inventory: list[dict[str, Any]] = Field(default_factory=list)
    forms_inventory: list[dict[str, Any]] = Field(default_factory=list)
    tables_inventory: list[dict[str, Any]] = Field(default_factory=list)
    role_observations: list[RoleObservation] = Field(default_factory=list)
    workflows: list[Workflow] = Field(default_factory=list)
    user_journeys: list[str] = Field(default_factory=list)
    business_rule_observations: list[str] = Field(default_factory=list)
    test_scenarios: list[TestScenario] = Field(default_factory=list)
    test_executions: list[TestExecution] = Field(default_factory=list)
    confirmed_bugs: list[BugReportEntry] = Field(default_factory=list)
    suspected_bugs: list[BugReportEntry] = Field(default_factory=list)
    ux_quality_observations: list[str] = Field(default_factory=list)
    coverage: Optional[CoverageRecord] = None
    regression_checklist: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    network_errors: list[str] = Field(default_factory=list)
    evidence_index: list[dict[str, Any]] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    recommended_next_testing_areas: list[str] = Field(default_factory=list)
    mermaid: dict[str, str] = Field(default_factory=dict)
    sections_markdown: dict[str, str] = Field(default_factory=dict)
    # Runtime transparency (provider / adapter / mode — never secrets)
    runtime: dict[str, Any] = Field(default_factory=dict)
    # ------------------------------------------------------------------
    # Reasoning-engine reporting (Senior QA Reporting Architect upgrade) —
    # structured, JSON-exportable blocks for the five newer intelligence
    # engines plus CRUD/collection/form-lifecycle/cleanup coverage and
    # blocked/unexecuted-scenario reasons. Every dict here is produced by
    # app.reporting.report_builder from RunMemory only (never invented) and
    # degrades to an empty dict/list when the corresponding engine wasn't
    # attached to this run (standard, non-autonomous runs).
    # ------------------------------------------------------------------
    knowledge_graph_summary: dict[str, Any] = Field(default_factory=dict)
    generated_goals: dict[str, Any] = Field(default_factory=dict)
    scenario_planning_summary: dict[str, Any] = Field(default_factory=dict)
    qa_strategy_summary: dict[str, Any] = Field(default_factory=dict)
    autonomous_investigation_summary: dict[str, Any] = Field(default_factory=dict)
    crud_workflow_coverage: dict[str, Any] = Field(default_factory=dict)
    form_lifecycle_summary: dict[str, Any] = Field(default_factory=dict)
    collection_coverage: dict[str, Any] = Field(default_factory=dict)
    executed_assertions: list[dict[str, Any]] = Field(default_factory=list)
    blocked_scenarios: list[dict[str, Any]] = Field(default_factory=list)
    unexecuted_scenarios: list[dict[str, Any]] = Field(default_factory=list)
    temporary_records: dict[str, Any] = Field(default_factory=dict)
    # How the model's context was actually built for this run — page source,
    # sections reduced/omitted, evidence counts, rejected evidence ids. Safe by
    # construction: counts, names, and flags only, never prompt text, raw DOM,
    # credentials, or evidence payloads. See app/gemma/base.py
    # GenerationRequest.context_metadata().
    model_context_summary: dict[str, Any] = Field(default_factory=dict)
    # What the application did with each write GemmaQA attempted: accepted,
    # refused (4xx), failed (5xx), or unproven. Distinguishes "we submitted a
    # form" from "the application accepted the data" — see
    # app/agent/submission_outcome.py.
    submission_outcome_summary: dict[str, Any] = Field(default_factory=dict)
    # Work the application offered, GemmaQA was ready to do, and this run's
    # configuration forbade — with the exact flags that would enable it. Empty
    # means policy did not limit the run. Without this, a run that stopped after
    # 27 of 480 allowed actions because every remaining candidate was a write
    # looked identical to one that had genuinely finished exploring.
    write_permission_gap: dict[str, Any] = Field(default_factory=dict)
    cleanup_status: dict[str, Any] = Field(default_factory=dict)
    stop_summary: dict[str, Any] = Field(default_factory=dict)
    capability_disclosure: dict[str, Any] = Field(default_factory=dict)
    # Legacy / compatibility fields
    summary: str = ""
    domain_purpose: str = ""
    navigation_map: str = ""
    bugs: list[Defect] = Field(default_factory=list)
    mermaid_diagrams: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API responses / WebSocket events
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"
    app: str = "GemmaQA"
    version: str = "0.1.0"


class AIHealthResponse(BaseModel):
    """Gemma provider health (never includes API keys)."""

    provider_type: str
    configured: bool
    reachable: bool | None = None
    model_identifier: Optional[str] = None
    multimodal_support: bool = False
    last_error_summary: Optional[str] = None
    consecutive_failures: int = 0


class CreateRunResponse(BaseModel):
    run_id: str
    status: RunStatusEnum
    message: str = "Run created"


class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None


class WSEvent(BaseModel):
    """Legacy WS envelope — prefer RunEvent for new clients."""

    event: str
    run_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict)


class WSMessage(BaseModel):
    """Canonical WebSocket message wrapping a RunEvent (or heartbeat)."""

    type: str  # event | heartbeat | history_complete | subscribed
    run_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    event: Optional[RunEvent] = None
    data: dict[str, Any] = Field(default_factory=dict)

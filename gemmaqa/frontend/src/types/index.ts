/** Typed frontend models aligned with backend schemas. */

export type RunStatusEnum =
  | "created"
  | "initializing"
  | "opening_browser"
  | "navigating"
  | "authenticating"
  | "observing"
  | "planning"
  | "executing"
  | "analyzing"
  | "documenting"
  | "completed"
  | "failed"
  | "cancelled"
  | "paused"
  | "pending"
  | "logging_in"
  | "exploring"
  | "testing"
  | "stopped";

export type WsEventType =
  | "subscribed"
  | "run_started"
  | "run_status_changed"
  | "state_changed"
  | "page_observed"
  | "page_classified"
  | "action_planned"
  | "action_planning"
  | "action_started"
  | "action_completed"
  | "action_finished"
  | "action_failed"
  | "action_blocked"
  | "test_generated"
  | "tests_proposed"
  | "test_executed"
  | "potential_bug_found"
  | "bug_found"
  | "bug_confirmed"
  | "evidence_captured"
  | "documentation_updated"
  | "run_completed"
  | "run_failed"
  | "run_cancelled"
  | "run_paused"
  | "run_resumed"
  | "browser_opened"
  | "navigated"
  | "state_compared"
  | "ai_failure"
  | string;

export interface RunConfiguration {
  max_pages?: number;
  max_actions?: number;
  max_runtime_seconds?: number;
  max_screenshots?: number;
  max_retries?: number;
  safe_mode: boolean;
  // Write permissions. These are what actually decide whether GemmaQA may
  // create, modify, and remove data — `safe_mode` does not gate them. Without
  // allow_safe_test_data_creation no record is ever created, so no CRUD
  // behaviour can be tested at all; without allow_destructive_actions the
  // records GemmaQA creates are left on the application under test.
  allow_controlled_writes: boolean;
  allow_safe_test_data_creation?: boolean;
  allow_destructive_actions?: boolean;
  allow_test_account_creation?: boolean;
  allow_login?: boolean;
  testing_objective?: string | null;
  headless: boolean;
  allow_cross_domain: boolean;
  allow_subdomains?: boolean;
  login_url?: string | null;
  username_selector?: string | null;
  password_selector?: string | null;
  submit_selector?: string | null;
  wait_after_login_ms?: number;
  enable_autonomous_investigation?: boolean;
}

export interface CreateRunRequest {
  url: string;
  username?: string;
  password?: string;
  configuration: RunConfiguration;
  notes?: string;
  /** Required: only test systems you own or are authorized to test */
  authorization_ack: boolean;
  auto_start?: boolean;
}

export interface CreateRunResponse {
  run_id: string;
  status: RunStatusEnum;
  message: string;
}

export interface StrandsStatus {
  installed: boolean;
  enabled: boolean;
  configured: boolean;
  ready: boolean;
  model_id: string | null;
  aws_region: string | null;
  sdk: string;
}

export interface RunStatus {
  run_id: string;
  status: RunStatusEnum;
  url: string;
  current_url?: string | null;
  pages_visited: number;
  actions_taken: number;
  bugs_found: number;
  progress_pct: number;
  message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  notes?: string | null;
  username?: string | null;
  configuration?: RunConfiguration;
  execution_speed?: number;
  action_pause?: number;
}

export interface WSEvent {
  event?: string;
  type?: string;
  run_id: string;
  timestamp: string;
  data?: Record<string, unknown>;
  payload?: Record<string, unknown>;
}

export interface NormalizedWsEvent {
  type: WsEventType;
  run_id: string;
  timestamp: string;
  payload: Record<string, unknown>;
  raw: WSEvent;
}

export interface PageItem {
  id: string;
  url: string;
  title: string;
  page_type: string;
  screenshot_path?: string | null;
}

export interface ActionItem {
  id: string;
  action_type: string;
  success: boolean;
  message: string;
  duration_ms: number;
  created_at?: string | null;
}

export interface BugItem {
  id: string;
  title: string;
  description: string;
  severity: string;
  status: string;
  payload?: Record<string, unknown>;
}

export interface BugReportEntry {
  bug_id: string;
  title: string;
  module: string;
  page: string;
  url: string;
  classification: string;
  severity: string;
  priority: string;
  preconditions: string[];
  test_data: string;
  steps_to_reproduce: string[];
  expected_result: string;
  actual_result: string;
  business_impact: string;
  possible_root_cause_hypothesis: string;
  confidence: number;
  screenshot_evidence: string[];
  trace_evidence: string[];
  console_evidence: string[];
  network_evidence: string[];
  discovery_timestamp?: string | null;
  run_id: string;
}

export interface ModuleRecord {
  module_id: string;
  name: string;
  description: string;
  entry_urls: string[];
  page_ids: string[];
}

export interface WorkflowStep {
  step_id: string;
  order: number;
  action: string;
  description: string;
  expected?: string | null;
  page_url?: string | null;
}

export interface Workflow {
  workflow_id: string;
  name: string;
  description: string;
  starting_page?: string | null;
  steps: WorkflowStep[];
  mermaid?: string;
}

export interface TestScenario {
  test_id: string;
  title: string;
  description: string;
  category: string;
  priority: string;
  preconditions: string[];
  steps: string[];
  expected_results: string[];
  related_workflow_id?: string | null;
}

export interface TestExecution {
  execution_id: string;
  test_id: string;
  run_id: string;
  status: "passed" | "failed" | "blocked" | "skipped" | "not_run";
  notes: string;
  evidence_ids: string[];
  executed_at?: string | null;
}

export interface CoverageRecord {
  run_id: string;
  pages_discovered: number;
  pages_explored: number;
  navigation_items_discovered: number;
  navigation_items_used: number;
  forms_discovered: number;
  forms_inspected: number;
  forms_tested: number;
  tables_discovered: number;
  tables_inspected: number;
  workflows_identified: number;
  tests_generated: number;
  tests_executed: number;
  passed: number;
  failed: number;
  bugs_found: number;
  suspected_issues: number;
  observations: number;
  action_budget_used: number;
  action_budget_total: number;
  observed_coverage_pct: number;
  explored_coverage_pct: number;
  executed_coverage_pct: number;
  coverage_notes: string[];
  unexplored_areas: string[];
  disclaimer: string;
}

export interface ProductOverview {
  run_id: string;
  product_name: string;
  summary: string;
  primary_purpose: string;
  target_users: string[];
  key_features: string[];
  tech_observations: string[];
}

export interface EvidenceFile {
  path: string;
  kind: string;
  size: number;
  url: string;
}

export interface FinalReport {
  run_id: string;
  generated_at: string;
  target_url: string;
  executive_summary: string;
  product_overview?: ProductOverview | null;
  inferred_business_domain: string;
  application_purpose: string;
  modules: ModuleRecord[];
  navigation_structure: string;
  page_inventory: Array<Record<string, unknown>>;
  forms_inventory: Array<Record<string, unknown>>;
  tables_inventory: Array<Record<string, unknown>>;
  workflows: Workflow[];
  user_journeys: string[];
  business_rule_observations: string[];
  test_scenarios: TestScenario[];
  test_executions: TestExecution[];
  confirmed_bugs: BugReportEntry[];
  suspected_bugs: BugReportEntry[];
  ux_quality_observations: string[];
  coverage?: CoverageRecord | null;
  regression_checklist: string[];
  console_errors: string[];
  network_errors: string[];
  evidence_index: Array<Record<string, unknown>>;
  known_limitations: string[];
  recommended_next_testing_areas: string[];
  mermaid: Record<string, string>;
  sections_markdown: Record<string, string>;
  summary?: string;
  navigation_map?: string;
}

/** Aggregated live-run view model used by the dashboard. */
export interface LiveRunView {
  run: RunStatus;
  productName: string;
  currentModule: string;
  currentPageType: string;
  currentAction: string;
  expectedResult: string;
  decisionSummary: string;
  observation: string;
  screenshotUrl: string | null;
  screenshotAt: string | null;
  pages: PageItem[];
  modules: ModuleRecord[];
  workflows: Workflow[];
  bugs: BugReportEntry[];
  tests: TestScenario[];
  executions: TestExecution[];
  evidence: EvidenceFile[];
  consoleErrors: string[];
  networkFailures: string[];
  docSections: Record<string, string>;
  navigationMermaid: string;
  timeline: NormalizedWsEvent[];
  inferredLabels: string[];
}

/**
 * One entry in a run's durable activity log — see
 * backend/app/reporting/activity_log.py. `summary` is the human-readable line
 * the backend already composed; the UI renders it rather than re-deriving one,
 * so the stored log and the live view can never disagree.
 */
export interface ActivityRecord {
  seq: number;
  run_id: string;
  at: string;
  /** Milliseconds since the run started — the "second by second" axis. */
  elapsed_ms: number;
  phase: string;
  event: string;
  summary: string;
  detail: Record<string, unknown>;
  /** Present only on measured spans (observation, planning, execution). */
  duration_ms?: number;
}

export interface ActivityLogPage {
  run_id: string;
  records: ActivityRecord[];
  count: number;
  last_seq: number;
  more_available: boolean;
}

/** Implemented LLM backends. Adding a backend in the catalog API lists it here. */
export interface ProviderCatalogEntry {
  id: string;
  label: string;
  description: string;
  configured: boolean;
  selectable: boolean;
  note: string;
  model_id: string | null;
}

export interface ProviderCatalog {
  active: string;
  active_label: string;
  run_mode: string;
  env_default: string;
  override: string | null;
  applies_to: string;
  active_runs: number;
  warning?: string;
  providers: ProviderCatalogEntry[];
}

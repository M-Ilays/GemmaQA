/**
 * Demo fixture data for VITE_DEMO_MODE=true.
 * Clearly labeled as demo — not live exploration results.
 */

import type {
  ActionItem,
  BugItem,
  CreateRunRequest,
  CreateRunResponse,
  EvidenceFile,
  FinalReport,
  PageItem,
  ProviderCatalog,
  RunStatus,
  StrandsStatus,
  WSEvent,
} from "../types";

export const DEMO_RUN_ID = "sample-run-001";

const PLACEHOLDER_SVG =
  "data:image/svg+xml," +
  encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="900" viewBox="0 0 1440 900">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0f766e"/>
      <stop offset="100%" stop-color="#0b1b2b"/>
    </linearGradient>
  </defs>
  <rect width="1440" height="900" fill="url(#g)"/>
  <text x="72" y="120" fill="#99f6e4" font-family="Georgia,serif" font-size="48">GemmaQA Demo</text>
  <text x="72" y="180" fill="#cbd5e1" font-family="sans-serif" font-size="22">Live browser screenshot unavailable in demo mode</text>
  <rect x="72" y="240" width="420" height="48" rx="8" fill="#134e4a"/>
  <text x="92" y="272" fill="#ecfdf5" font-family="sans-serif" font-size="18">Dashboard · Inventory · Tickets</text>
  <rect x="72" y="320" width="900" height="420" rx="12" fill="#0b1220" stroke="#334155"/>
  <text x="100" y="380" fill="#94a3b8" font-family="sans-serif" font-size="16">Observed page: Inventory</text>
</svg>`);

export const demoRun: RunStatus = {
  run_id: DEMO_RUN_ID,
  status: "completed",
  url: "https://demo.gemmaqa.local/",
  current_url: "https://demo.gemmaqa.local/inventory",
  pages_visited: 3,
  actions_taken: 12,
  bugs_found: 1,
  progress_pct: 100,
  message: "Demo run completed (fixture data)",
  started_at: "2026-07-23T22:00:00Z",
  finished_at: "2026-07-23T22:08:00Z",
};

export const demoPages: PageItem[] = [
  {
    id: "page-login",
    url: "https://demo.gemmaqa.local/login",
    title: "Login",
    page_type: "authentication",
    screenshot_path: "screenshots/login.png",
  },
  {
    id: "page-dash",
    url: "https://demo.gemmaqa.local/dashboard",
    title: "Dashboard",
    page_type: "dashboard",
    screenshot_path: "screenshots/dashboard.png",
  },
  {
    id: "page-inv",
    url: "https://demo.gemmaqa.local/inventory",
    title: "Inventory | GemmaQA Demo",
    page_type: "list",
    screenshot_path: "screenshots/inventory.png",
  },
];

export const demoActions: ActionItem[] = [
  {
    id: "act-1",
    action_type: "click",
    success: true,
    message: "Opened Inventory from dashboard",
    duration_ms: 420,
    created_at: "2026-07-23T22:05:00Z",
  },
  {
    id: "act-2",
    action_type: "inspect_form",
    success: true,
    message: "Inspected search form",
    duration_ms: 180,
    created_at: "2026-07-23T22:05:20Z",
  },
];

export const demoBugs: BugItem[] = [
  {
    id: "BUG-001",
    title: "HTTP 500 on session endpoint",
    description: "Session API returns 500 during login flow",
    severity: "critical",
    status: "open",
    payload: {
      module: "Authentication",
      classification: "confirmed_bug",
      expected: "200 OK",
      actual: "500",
      priority: "urgent",
      business_impact: "Users cannot establish a session",
    },
  },
];

export const demoEvidence: EvidenceFile[] = [
  {
    path: "screenshots/inventory.png",
    kind: "screenshot",
    size: 12000,
    url: PLACEHOLDER_SVG,
  },
  {
    path: "reports/final_report.md",
    kind: "report",
    size: 8000,
    url: "#",
  },
];

export const demoReport: FinalReport = {
  run_id: DEMO_RUN_ID,
  generated_at: "2026-07-23T22:29:08.347767",
  target_url: "https://demo.gemmaqa.local/",
  executive_summary:
    "Exploratory QA of Login at demo.gemmaqa.local visited 3 page(s), executed 12 action(s), generated 2 scenario(s), and recorded 1 confirmed bug(s) and 1 suspected issue(s). Observed coverage 75% (not complete coverage).",
  product_overview: {
    run_id: DEMO_RUN_ID,
    product_name: "GemmaQA Demo Console",
    summary: "Internal operations console for inventory and tickets",
    primary_purpose: "Help operators manage inventory and support tickets",
    target_users: ["operators"],
    key_features: ["Login", "Dashboard", "Inventory"],
    tech_observations: [],
  },
  inferred_business_domain: "Internal operations console for inventory and tickets",
  application_purpose: "Help operators manage inventory and support tickets",
  modules: [
    {
      module_id: "mod-auth",
      name: "Authentication",
      description: "Login and session",
      entry_urls: ["https://demo.gemmaqa.local/login"],
      page_ids: ["page-login"],
    },
    {
      module_id: "mod-inv",
      name: "Inventory",
      description: "Stock listing",
      entry_urls: ["https://demo.gemmaqa.local/inventory"],
      page_ids: ["page-inv"],
    },
  ],
  navigation_structure:
    'flowchart TD\n  P0["Login"]\n  P1["Dashboard"]\n  P2["Inventory"]\n  P0 -->|submit| P1\n  P1 -->|click| P2',
  page_inventory: demoPages.map((p) => ({
    page_id: p.id,
    title: p.title,
    url: p.url,
    page_type: p.page_type,
  })),
  forms_inventory: [{ form_id: "form-login", page_url: demoPages[0].url, field_count: 2 }],
  tables_inventory: [{ table_id: "tbl-tickets", page_url: demoPages[1].url, row_count: 3 }],
  workflows: [
    {
      workflow_id: "wf-login",
      name: "Login to dashboard",
      description: "Authenticate then land on dashboard",
      starting_page: demoPages[0].url,
      steps: [
        { step_id: "s1", order: 1, action: "fill", description: "Enter email" },
        { step_id: "s2", order: 2, action: "fill", description: "Enter password" },
        { step_id: "s3", order: 3, action: "click", description: "Submit login" },
      ],
      mermaid:
        'flowchart LR\n  S["Login"]\n  W0["1. fill"]\n  W1["2. fill"]\n  W2["3. click"]\n  S --> W0 --> W1 --> W2 --> E[End]',
    },
  ],
  user_journeys: ["Login → Dashboard → Inventory"],
  business_rule_observations: ["Form form-login marks required fields: Email, Password"],
  test_scenarios: [
    {
      test_id: "tc-001",
      title: "Login with valid credentials",
      description: "Smoke login",
      category: "smoke",
      priority: "high",
      preconditions: ["Valid demo account"],
      steps: ["Open login", "Submit form"],
      expected_results: ["Dashboard loads"],
    },
    {
      test_id: "tc-002",
      title: "Search inventory",
      description: "Search list",
      category: "functional",
      priority: "medium",
      preconditions: ["Authenticated"],
      steps: ["Open inventory", "Search"],
      expected_results: ["Results table updates"],
    },
  ],
  test_executions: [
    {
      execution_id: "ex-001",
      test_id: "tc-001",
      run_id: DEMO_RUN_ID,
      status: "passed",
      notes: "OK",
      evidence_ids: [],
      executed_at: "2026-07-23T22:06:00Z",
    },
    {
      execution_id: "ex-002",
      test_id: "tc-002",
      run_id: DEMO_RUN_ID,
      status: "failed",
      notes: "Table did not refresh",
      evidence_ids: [],
      executed_at: "2026-07-23T22:07:00Z",
    },
  ],
  confirmed_bugs: [
    {
      bug_id: "BUG-001",
      title: "HTTP 500 on session endpoint",
      module: "Authentication",
      page: "Login",
      url: "https://demo.gemmaqa.local/login",
      classification: "confirmed_bug",
      severity: "critical",
      priority: "urgent",
      preconditions: ["Reach login page"],
      test_data: "",
      steps_to_reproduce: ["Open login", "Submit credentials"],
      expected_result: "200 OK",
      actual_result: "500 from /api/session",
      business_impact: "Users cannot establish a session",
      possible_root_cause_hypothesis: "Unhandled exception in session service",
      confidence: 0.95,
      screenshot_evidence: ["screenshots/login.png"],
      trace_evidence: [],
      console_evidence: [],
      network_evidence: [],
      discovery_timestamp: "2026-07-23T22:04:00Z",
      run_id: DEMO_RUN_ID,
    },
  ],
  suspected_bugs: [
    {
      bug_id: "analysis-001",
      title: "Console TypeError on login",
      module: "Authentication",
      page: "Login",
      url: "https://demo.gemmaqa.local/login",
      classification: "suspected_bug",
      severity: "medium",
      priority: "medium",
      preconditions: [],
      test_data: "",
      steps_to_reproduce: ["Open login page"],
      expected_result: "No console errors",
      actual_result: "TypeError: x is undefined",
      business_impact: "May indicate broken client script",
      possible_root_cause_hypothesis: "Hypothesis only — not verified",
      confidence: 0.6,
      screenshot_evidence: [],
      trace_evidence: [],
      console_evidence: [],
      network_evidence: [],
      discovery_timestamp: "2026-07-23T22:04:10Z",
      run_id: DEMO_RUN_ID,
    },
  ],
  ux_quality_observations: ["Missing breadcrumbs on inventory"],
  coverage: {
    run_id: DEMO_RUN_ID,
    pages_discovered: 4,
    pages_explored: 3,
    navigation_items_discovered: 7,
    navigation_items_used: 2,
    forms_discovered: 2,
    forms_inspected: 1,
    forms_tested: 0,
    tables_discovered: 1,
    tables_inspected: 1,
    workflows_identified: 1,
    tests_generated: 2,
    tests_executed: 2,
    passed: 1,
    failed: 1,
    bugs_found: 1,
    suspected_issues: 1,
    observations: 1,
    action_budget_used: 12,
    action_budget_total: 40,
    observed_coverage_pct: 75,
    explored_coverage_pct: 75,
    executed_coverage_pct: 100,
    coverage_notes: [
      "Observed coverage (explored/discovered pages): 75%",
      "These metrics do not imply complete application coverage.",
    ],
    unexplored_areas: ["https://demo.gemmaqa.local/settings"],
    disclaimer:
      "Coverage percentages reflect observed exploratory activity only and do not claim complete application coverage.",
  },
  regression_checklist: [
    "Critical navigation paths remain reachable",
    "Primary forms render without console errors",
    "No new network 5xx on core pages",
  ],
  console_errors: ["TypeError: x is undefined"],
  network_errors: ["500 GET /api/session"],
  evidence_index: [
    {
      evidence_id: "ev-shot",
      kind: "screenshot",
      path: "screenshots/inventory.png",
      description: "Inventory page",
    },
  ],
  known_limitations: [
    "Exploration is budget-limited and does not claim complete coverage.",
    "Demo fixture data — not a live run.",
  ],
  recommended_next_testing_areas: [
    "Explore unvisited URL: https://demo.gemmaqa.local/settings",
    "Triage suspected bugs with targeted reproduction.",
  ],
  mermaid: {
    navigation:
      'flowchart TD\n  P0["Login"]\n  P1["Dashboard"]\n  P2["Inventory"]\n  P0 -->|submit| P1\n  P1 -->|click| P2',
    user_journey:
      'flowchart LR\n  S[Start]\n  J0["Login"]\n  J1["Dashboard"]\n  J2["Inventory"]\n  S --> J0 --> J1 --> J2 --> E[End]',
  },
  sections_markdown: {
    "Executive Summary": "## Executive Summary\n\nDemo fixture executive summary.",
    "Coverage Summary": "## Coverage Summary\n\nObserved coverage 75%.",
  },
};

export const demoTimeline: WSEvent[] = [
  {
    event: "run_status_changed",
    run_id: DEMO_RUN_ID,
    timestamp: "2026-07-23T22:00:01Z",
    data: { payload: { state: "initializing", message: "Initializing run" } },
  },
  {
    event: "page_observed",
    run_id: DEMO_RUN_ID,
    timestamp: "2026-07-23T22:01:00Z",
    data: {
      payload: {
        url: "https://demo.gemmaqa.local/login",
        title: "Login",
        page_type: "authentication",
      },
    },
  },
  {
    event: "action_planned",
    run_id: DEMO_RUN_ID,
    timestamp: "2026-07-23T22:02:00Z",
    data: {
      payload: {
        action: "click",
        reason: "Open inventory module",
        expected_result: "Inventory list loads",
      },
    },
  },
  {
    event: "action_completed",
    run_id: DEMO_RUN_ID,
    timestamp: "2026-07-23T22:02:10Z",
    data: { payload: { action: "click", success: true, message: "Navigated" } },
  },
  {
    event: "bug_confirmed",
    run_id: DEMO_RUN_ID,
    timestamp: "2026-07-23T22:04:00Z",
    data: { payload: { title: "HTTP 500 on session endpoint", severity: "critical" } },
  },
  {
    event: "documentation_updated",
    run_id: DEMO_RUN_ID,
    timestamp: "2026-07-23T22:08:00Z",
    data: { payload: { sections: ["Executive Summary", "Coverage Summary"] } },
  },
  {
    event: "run_completed",
    run_id: DEMO_RUN_ID,
    timestamp: "2026-07-23T22:08:05Z",
    data: { payload: { bugs: 1, pages: 3 } },
  },
];

export const demoPlaceholderScreenshot = PLACEHOLDER_SVG;

const DEMO_PROVIDER_CATALOG: ProviderCatalog = {
  active: "mock",
  active_label: "Mock",
  run_mode: "Mock / deterministic",
  env_default: "mock",
  override: null,
  applies_to: "new_runs",
  active_runs: 0,
  providers: [
    {
      id: "mock",
      label: "Mock",
      description: "Deterministic heuristics. No live model — exploration only.",
      configured: true,
      selectable: true,
      note: "Demo mode always uses Mock.",
      model_id: null,
    },
    {
      id: "openai_compatible",
      label: "Gemma (Ollama / OpenAI-compatible)",
      description: "Local Ollama or any OpenAI-compatible chat API.",
      configured: false,
      selectable: false,
      note: "Not available in demo mode.",
      model_id: null,
    },
    {
      id: "gemini",
      label: "Google Gemini",
      description: "Google AI Studio / Gemini API.",
      configured: false,
      selectable: false,
      note: "Not available in demo mode.",
      model_id: null,
    },
    {
      id: "bedrock",
      label: "Amazon Bedrock",
      description: "AWS Bedrock (Nova, Claude, Llama, and others).",
      configured: false,
      selectable: false,
      note: "Not available in demo mode.",
      model_id: null,
    },
    {
      id: "transformers",
      label: "Gemma (Transformers)",
      description: "Local Hugging Face transformers weights.",
      configured: false,
      selectable: false,
      note: "Not available in demo mode.",
      model_id: null,
    },
  ],
};

// Fix circular type - ExportKind is in api.ts. Define locally for demoApi.
type DemoExportKind = "json" | "md" | "html" | "bugs.csv" | "tests.csv" | "navigation.mmd";

export const demoApi = {
  health: async () => ({ status: "ok", app: "GemmaQA", version: "0.1.0-demo" }),
  strandsStatus: async (): Promise<StrandsStatus | null> => ({
    installed: false,
    enabled: false,
    configured: false,
    ready: false,
    model_id: null,
    aws_region: null,
    sdk: "strands-agents",
  }),
  listProviders: async (): Promise<ProviderCatalog> => DEMO_PROVIDER_CATALOG,
  setProvider: async (_provider: string): Promise<ProviderCatalog> => {
    void _provider;
    throw new Error("Provider switching is disabled in demo mode");
  },
  listRuns: async (): Promise<RunStatus[]> => [demoRun],
  getRun: async (runId: string): Promise<RunStatus> => {
    if (runId !== DEMO_RUN_ID) throw new Error("Demo mode only includes sample-run-001");
    return demoRun;
  },
  createRun: async (
    _body: CreateRunRequest,
    _opts?: { strands?: boolean },
  ): Promise<CreateRunResponse> => ({
    run_id: DEMO_RUN_ID,
    status: "completed",
    message: "Demo mode — loaded fixture run (no live browser)",
  }),
  startRun: async (runId: string) => demoApi.getRun(runId),
  cancelRun: async (runId: string) => demoApi.getRun(runId),
  pauseRun: async (runId: string) => demoApi.getRun(runId),
  resumeRun: async (runId: string) => demoApi.getRun(runId),
  setRunPacing: async (runId: string) => demoApi.getRun(runId),
  getActions: async (_runId?: string) => demoActions,
  // Demo mode has no backend writing an activity log; the live view falls back
  // to the WebSocket timeline it already replays from demoTimeline.
  getActivity: async (runId?: string) => ({
    run_id: runId || "demo",
    records: [],
    count: 0,
    last_seq: 0,
    more_available: false,
  }),
  getBugs: async (_runId?: string) => demoBugs,
  getPages: async (_runId?: string) => demoPages,
  getReport: async (_runId?: string) => demoReport,
  getReportMarkdown: async (_runId?: string) =>
    "# Demo GemmaQA Report\n\nThis is fixture documentation for hackathon demos.\n",
  getEvidence: async (_runId?: string) => demoEvidence,
  getNavigationMermaid: async (_runId?: string) => demoReport.navigation_structure,
  evidenceFileUrl: (_runId: string, _path: string) => PLACEHOLDER_SVG,
  exportUrl: (runId: string, kind: DemoExportKind) => {
    void runId;
    void kind;
    return "#demo-export";
  },
};

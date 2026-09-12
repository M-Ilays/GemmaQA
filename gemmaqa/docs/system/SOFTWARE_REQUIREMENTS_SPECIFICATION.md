# GemmaQA Software Requirements Specification

**Status:** Derived from direct repository inspection (backend source, schemas, tests, live-verification evidence).
**Verified baseline:** 1133 tests passed, 1 skipped, 0 failed.
**Structure:** Loosely inspired by IEEE 830/29148-style SRS organization. This is an internal engineering document, not a certified standards artifact, and makes no compliance claim beyond structural resemblance.

---

## 1. Introduction

### 1.1 Purpose
This document specifies the functional and non-functional requirements of GemmaQA as they exist in the current codebase, and states their implementation status precisely (implemented, partially implemented, or opt-in/disabled by default). It is intended to let a reader determine, requirement by requirement, whether a capability exists today and where to find it.

### 1.2 Scope
Covers the GemmaQA backend (`backend/app/`): browser execution, safety, the ten intelligence engines (Perception, Entity/Actor/Workflow/Dependency Discovery, Knowledge Graph, Goal Generation, Scenario Planning, QA Strategy, Autonomous Investigation), memory, reporting, and the API layer. Does not specify the frontend UI's behavior in detail.

### 1.3 Intended audience
Engineers extending GemmaQA, QA reviewers validating claims against code, and technical stakeholders assessing capability and risk before authorizing a run against a real application.

### 1.4 Document conventions
- **Requirement ID format:** `FR-<CATEGORY>-<NNN>` (functional) or `NFR-<CATEGORY>-<NNN>` (non-functional).
- **Implementation status** is one of: **Implemented**, **Implemented (opt-in, default off)**, **Partially implemented**, or **Not implemented (future possibility only)**.
- A requirement is only marked **Implemented** when a specific class/function/test was located in the current codebase supporting it.

### 1.5 Definitions and acronyms

| Term | Meaning |
|---|---|
| CanonicalPageModel | The structured, typed representation of one observed page, produced by the Perception Engine (`app/perception/models.py`). |
| Application Knowledge Graph | The unified node/edge graph projected from the four discovery registries (`app/intelligence/knowledge_graph/`). |
| InvestigationGoal | A prioritized, evidence-backed hypothesis worth investigating, produced by the Goal Generation Engine. Distinct from the legacy `ExplorationGoal` (see below). |
| ExplorationGoal | A legacy, deterministic wrapper over frontier candidates used by the *default* exploration loop (`app/agent/goals.py`) — unrelated to the reasoning-engine `InvestigationGoal`. |
| InvestigationScenario | A declarative, browser-independent, semantic investigation plan produced by the Scenario Planning Engine. |
| ExecutionCandidate | A scenario wrapped with priority/queue/batch metadata by the QA Strategy Engine — the unit the Autonomous Investigation Engine selects. |
| Runtime Planner | `Planner` (`app/agent/planner.py`) — selects the next concrete `BrowserAction`. |
| Safety Validator | `ActionValidator` (`app/safety/validator.py`) — the per-action safety gate. |
| BrowserAdapter | Abstract execution gateway to a concrete browser engine. |
| ActionExecutor | Executes one validated action via a `BrowserAdapter`. |
| RunMemory | The single authoritative per-run state object (`app/agent/memory.py`). |
| Actor (application-under-test) | A role/identity discovered *inside* the target application (e.g. "admin", "guest") — never to be confused with a GemmaQA system user. |

### 1.6 References
`docs/GEMMAQA_ARCHITECTURE.md`, and the per-engine docs referenced throughout (`docs/*.md`) — all in this repository.

---

## 2. Product overview

### 2.1 Product perspective
GemmaQA is a standalone backend service (FastAPI + SQLite + Playwright) with a companion frontend, operating against one externally supplied, authorized web application URL per run. It is not a plugin to an existing test framework and does not require the target application's source code.

### 2.2 Product functions (summary)
Observe a web application; discover its entities, actors, workflows, and business dependencies; build a knowledge graph; generate investigation goals; plan safe semantic scenarios; prioritize and batch them; optionally execute them autonomously through the same safety-gated pipeline used for normal exploration; collect evidence; verify assertions; and report findings.

### 2.3 User classes
See Section 3.

### 2.4 Operating environment
Python backend (FastAPI, SQLAlchemy async + SQLite by default, Playwright for browser automation, optional Playwright MCP transport), a React/TypeScript frontend (not detailed here), and an optional external or local LLM provider (`mock`, `openai_compatible`, or `transformers`).

### 2.5 Constraints
- Requires explicit authorization to test the target URL (`CreateRunRequest.authorization_ack`).
- Safety policy defaults to read-mostly behavior (`safe_mode=True`, `allow_controlled_writes=False`).
- Autonomous investigation is off by default and must be explicitly enabled per run.
- No cross-run persistence of reasoning-engine state (Section 13 of the Architecture document).

### 2.6 Assumptions and dependencies
Assumes a reachable, authorized target URL; assumes Playwright's browser binaries are installed; assumes, for anything beyond the deterministic `mock` LLM provider, that a real model endpoint or local weights are configured.

---

## 3. User classes and actors

**GemmaQA system users** (people/systems operating GemmaQA):

| Class | Description |
|---|---|
| QA engineer | Configures and reviews runs, interprets reports and evidence. |
| Developer | Extends engines, adds discovery signals, writes tests. |
| Engineering manager | Reviews coverage/confidence trends and defect reports across runs. |
| Administrator/operator | Configures provider/adapter settings, manages run lifecycle via the API (`RunManager`). |
| Autonomous investigation controller | Not a human — the system itself, acting as the internal actor that decides *when* to invoke `AutonomousInvestigationEngine.next_action()`/`observe_step_result()` inside the controller loop, when the feature is enabled. |

**Application-under-test actors** (roles GemmaQA *discovers*, never GemmaQA operators): any identity the target application itself recognizes (e.g., an "admin" or "guest" role) — represented as `ActorRecord` entries in `ActorRegistry`. These must never be confused with the GemmaQA system users above; they are data GemmaQA produces, not people operating GemmaQA.

**External application under test:** the target web application itself — a passive subject of observation and (optionally, safety-gated) action, never a GemmaQA component.

---

## 4. Functional requirements

Status legend: **I** = Implemented, **I-OPT** = Implemented, opt-in/default-off, **P** = Partially implemented, **N** = Not implemented (future only).

### FR-OBS — Observation and perception

| ID | Statement | Source component | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-OBS-001 | The system shall capture a structured `PageState` (headings, interactive elements, forms, tables, dialogs, console/network errors) from the live browser after every navigation or action. | `PageObserver.observe`/`observe_adapter` | `tests/test_browser_layer.py` passes; `PageState` populated with capped, normalized fields. | I |
| FR-OBS-002 | The system shall compute a de-noised state fingerprint that ignores volatile counters/timestamps, to detect genuine page-state changes. | `fingerprint_page_state` (`app/browser/fingerprint.py`) | Fingerprint stable across identical states with different timestamps/counters. | I |
| FR-OBS-003 | The system shall build a richer `CanonicalPageModel` (regions, navigation, forms, tables, images, dialogs) from the same observation. | `PerceptionEngine.observe`/`observe_adapter` | `tests/test_perception_engine.py`, `tests/test_canonical_page_model.py`. | I |
| FR-OBS-004 | The system may optionally analyze a bounded screenshot crop with a vision-capable model to classify otherwise-ambiguous visual elements. | `VisualAnalyzer`, gated by `VisualObservationPolicy` | `tests/test_visual_observation.py`; gated by `gemma_supports_images` (default `False`). | I-OPT |

### FR-ENT — Entity discovery

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-ENT-001 | The system shall discover candidate business-entity terms from observed page structure without any hardcoded entity vocabulary. | `EntityCandidateBuilder`, `EntityDiscoveryEngine` | AST-walking neutrality test in `tests/test_entity_discovery.py` passes. | I |
| FR-ENT-002 | The system shall assign each entity a confidence score and lifecycle status (`candidate`/`confirmed`/`incomplete`/`stale`) based on corroborating evidence diversity. | `entity_confidence.py` | `TestConfidence` class in `tests/test_entity_discovery.py`. | I |
| FR-ENT-003 | The system shall infer entity relationships (e.g. ownership, nesting) from URL structure and cross-referenced table/form fields. | `entity_relationship_builder.py` | `TestRelationships` class. | I |

### FR-ACT — Actor discovery

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-ACT-001 | The system shall discover candidate actor/role terms and distinguish an actually-observed authenticated session from a merely-named role. | `ActorCandidateBuilder`, `actor_memory.status_for` | `unverified` vs `confirmed` status distinction covered in `tests/test_actor_discovery.py`. | I |
| FR-ACT-002 | The system shall infer permission candidates (`can_<verb>[_<noun>]`) from visible/disabled controls, page reachability, and 401/403 responses. | `permission_discovery.py` | `TestPermissionDiscovery` class. | I |
| FR-ACT-003 | The system shall infer role relationships (co-assignment, permission-superset hierarchy) and support pairwise actor comparison. | `role_relationships.py` | `TestRoleRelationships`, `TestActorDifferenceAnalysis`. | I |

### FR-WFL — Workflow discovery

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-WFL-001 | The system shall reconstruct a workflow (actor → action → entity → state transition → outcome) from a before/after page-model pair and the action executed between them. | `WorkflowDiscoveryEngine.observe` | `tests/test_workflow_discovery.py`. | I |
| FR-WFL-002 | The system shall distinguish observed transitions from merely inferred ones and never silently collapse the two. | `STEP_STATUSES` (`observed`/`inferred`/...) | `TestStateTransitions`. | I |
| FR-WFL-003 | The system shall detect cross-role hand-offs and branch points (approve/reject, success/failure) where evidence supports them. | `workflow_relationship_builder.py` | `TestActorAwareWorkflows`, `TestBranches`. | I |

### FR-DEP — Dependency discovery

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-DEP-001 | The system shall identify visible derived outputs (KPIs, counters, charts, reports, queues) and exclude non-business numbers (nav numbering, dates, ids). | `OutputCandidateBuilder` | `TestOutputDiscovery` in `tests/test_dependency_discovery.py`. | I |
| FR-DEP-002 | The system shall connect an output to a candidate producing entity/workflow/actor and an inferred effect direction, without asserting a single formula unless evidence supports it. | `DependencyRelationshipBuilder`, `AggregationRuleInferer` | `TestAggregation`. | I |
| FR-DEP-003 | The system shall only mark a dependency `verified` through explicit before/after correlation with a scope-compatibility guard, never by assumption. | `DependencyCorrelator` | `TestBeforeAfterCorrelation`. | I |
| FR-DEP-004 | The system shall never auto-switch actors to validate a cross-role dependency. | `DependencyRelationshipBuilder` docstring + `requires_actor_switch()` | Explicit non-goal in `docs/BUSINESS_DEPENDENCY_DISCOVERY_ENGINE.md`; no actor-session-switch code path found in this package. | I (as a *restriction*, not a gap) |

### FR-KG — Knowledge Graph

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-KG-001 | The system shall synchronize all four discovery registries into one versioned node/edge graph in a single pass. | `ApplicationKnowledgeGraph.synchronize` | `tests/test_knowledge_graph.py` (105 tests). | I |
| FR-KG-002 | The system shall run bounded inference rules, consistency checks, and gap analysis as part of the same synchronization pass. | `GraphInferenceEngine`, `GraphConsistencyChecker`, `GraphGapAnalyzer` | Same suite. | I |
| FR-KG-003 | The system shall only increment the graph version when content genuinely changed (idempotent re-synchronization). | `KnowledgeGraphMemory.begin_pass/end_pass` | Idempotency test class present. | I |
| FR-KG-004 | The system shall provide a bounded traversal/query API and context projection for a given node. | `GraphQueryEngine`, `GraphContextProjector` | Present in `graph_query_engine.py`/`graph_context_projector.py`. | I |

### FR-GOAL — Goal generation

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-GOAL-001 | The system shall generate prioritized `InvestigationGoal` records purely from the current Knowledge Graph, with a transparent, named-factor priority score. | `GoalGenerationEngine.generate`, `goal_priority.py` | `tests/test_goal_generation.py` (53 tests). | I |
| FR-GOAL-002 | The system shall deduplicate goals and resolve inter-goal dependencies deterministically. | `goal_deduplicator.py`, `goal_dependency_resolver.py` | Same suite. | I |
| FR-GOAL-003 | The system shall generate goals idempotently — an unchanged graph must not spuriously regenerate or re-version goals. | `GoalMemory` pass-scoped versioning | Determinism/idempotency test class. | I |

### FR-SCN — Scenario planning

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-SCN-001 | The system shall convert a goal into one or more declarative `InvestigationScenario`s with semantic-only steps (no selectors). | `ScenarioPlanningEngine.generate` | `tests/test_scenario_planning.py` (107 tests). | I |
| FR-SCN-002 | The system shall assess feasibility and a multi-component risk score for every scenario before it is ever offered for execution. | `scenario_feasibility_analyzer.py`, `scenario_risk_analyzer.py` | `TestFeasibility`, `TestRisk`. | I |
| FR-SCN-003 | The system shall detect scenario-level dependencies, conflicts, and gaps and never silently drop a genuinely distinct alternative. | `scenario_dependency_resolver.py`, `scenario_conflict_detector.py`, `scenario_gap_analyzer.py` | `TestDependenciesAndConflicts`, `TestGaps`. | I |
| FR-SCN-004 | The system shall never execute a browser action from within this package. | Package `__init__.py` scope statement; no `BrowserAdapter`/`ActionExecutor`/Playwright import found | `TestNeutrality`/no-reference test in `tests/test_scenario_planning.py`. | I |

### FR-STR — QA strategy

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-STR-001 | The system shall assign every scenario a deterministic, explainable priority score under a selectable named policy. | `strategy_scoring.py` (12 signals, 10 named policies) | `tests/test_qa_strategy.py` (34 tests). | I |
| FR-STR-002 | The system shall classify every candidate into exactly one of 10 named execution queues. | `strategy_queue_builder.py` | `TestQueueGeneration`. | I |
| FR-STR-003 | The system shall batch executable candidates to minimize actor switching and navigation. | `strategy_batch_builder.py` | `TestBatchGeneration`. | I |
| FR-STR-004 | The system shall never execute a scenario itself. | Package scope statement; confirmed no browser imports | `TestNeutrality`/no-reference test. | I |

### FR-INV — Autonomous investigation

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-INV-001 | The system shall, when enabled, select the next executable scenario from the QA Strategy queues and drive it step-by-step. | `AutonomousInvestigationEngine.next_action`/`observe_step_result` | `tests/test_autonomous_investigation.py` (48 tests). | **I-OPT** (`enable_autonomous_investigation=False` by default) |
| FR-INV-002 | The system shall revalidate a scenario's preconditions against the current run state (not just planning-time state) before attempting it. | `precondition_validator.py` | `TestPreconditionValidator`. | I-OPT |
| FR-INV-003 | The system shall reject a scenario whose risk class is prohibited or whose feasibility is blocked, before attempting any step. | `investigation_safety_gate.py` | `TestSafetyGate`. | I-OPT |
| FR-INV-004 | The system shall never select the same candidate for execution more than once after it reaches a terminal outcome. | `InvestigationMemory.investigated_candidate_ids` | `TestExecutionLifecycle.test_every_ready_candidate_investigated_exactly_once`; regression test added after a real duplicate-execution bug was found and fixed. | I-OPT |
| FR-INV-005 | The system shall delegate every browser-driving semantic step to the existing Runtime Planner, never issuing a raw browser command itself. | `semantic_step_executor.resolve_step` → `planner.plan_by_priority` | No `BrowserAdapter`/`ActionExecutor`/Playwright import in the package (verified). | I-OPT |
| FR-INV-006 | The system shall evaluate assertions against genuinely observed evidence and report `supported`/`contradicted`/`inconclusive` honestly — never fabricating `supported` without a real signal. | `assertion_verifier.py` | `TestAssertionVerifier.test_no_signal_is_honestly_inconclusive_never_supported`. | I-OPT |
| FR-INV-007 | The system shall classify execution failures and apply a bounded, class-appropriate recovery action. | `recovery.py` | `TestRecovery`. | I-OPT |
| FR-INV-008 | The system shall stop initiating new investigations when budget is exhausted, failures repeat, no executable scenarios remain, or the run is cancelled. | `AutonomousInvestigationEngine.stop_reason` | `TestStopConditions`. | I-OPT |
| FR-INV-009 | The system shall re-invoke Goal Generation after an investigation completes and report which goal ids are new, without fabricating goal records itself. | `knowledge_feedback.py` | `TestKnowledgeCoverageConfidenceUpdates`; live-verified (SauceDemo run generated 5 new follow-up goals; ServiceFlow run generated 44). | I-OPT |

### FR-PLAN — Runtime planning

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-PLAN-001 | The system shall generate the single canonical candidate set for a page from `FrontierBuilder`, with no competing candidate-derivation path. | `FrontierBuilder.build` | `tests/test_frontier_navigation_priority.py`, `tests/test_frontier_canonical_integration.py`. | I |
| FR-PLAN-002 | The system shall score candidates via a transparent, named additive factor model, with a fully deterministic fallback when the LLM path is unavailable or repeats. | `PriorityEngine.build_decision`, `Planner.plan_by_priority` | `tests/test_priority_engine.py`. | I |
| FR-PLAN-003 | An LLM may only reorder a near-tied top group of candidates; it can never introduce a new candidate or override a safety rejection. | `apply_advisory_order`, `_apply_gemma_tie_break` | `tests/test_goal_ranking.py`. | I |

### FR-SAFE — Safety validation

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-SAFE-001 | The system shall validate every proposed action against budgets, URL scope, prohibited-intent patterns, and write permissions before execution. | `ActionValidator.validate` | `tests/test_safety_and_parser.py`, `tests/test_security_hardening.py` (28 tests). | I |
| FR-SAFE-002 | The system shall never allow `ActionExecutor.execute()` to run without a preceding validator approval on the same action object. | `controller.py` per-action sequence | Single `executor.execute()` call site in the codebase, reached only after the validation branch (confirmed via audit). | I |
| FR-SAFE-003 | The system shall rewrite (prefix) safe test-data write values with a recognizable marker rather than accepting arbitrary values, when safe-write mode is active. | `ActionValidator` (`TEST_DATA_PREFIX = "GemmaQA_TEST_"`) | `test_safe_write_prefixes_test_data`. | I |
| FR-SAFE-004 | The system shall re-validate navigation scope after execution, independent of the pre-execution check (in case a redirect left authorized scope). | `ActionValidator.validate_navigation_result` | Covered in `tests/test_security_hardening.py`. | I |

### FR-EXEC — Browser execution

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-EXEC-001 | The system shall execute a validated action through an abstract `BrowserAdapter`, supporting at least a direct in-process Playwright implementation and a Playwright MCP implementation. | `ActionExecutor`, `DirectPlaywrightAdapter`, `PlaywrightMCPAdapter` | `tests/test_adapter_execution.py` (15 tests). | I |
| FR-EXEC-002 | The system shall never silently fall back from the MCP adapter to the direct adapter on connection failure. | `PlaywrightMCPAdapter` (raises `RuntimeError`) | `test_no_silent_fallback_to_direct`. | I |
| FR-EXEC-003 | The system shall gate each action by the active adapter's declared capabilities, blocking (not guessing) unsupported actions. | `ACTION_CAPABILITY` map + `AdapterCapabilities` | `test_missing_capability_blocks_safely`. | I |

### FR-EVD — Evidence collection

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-EVD-001 | The system shall capture a before/after screenshot and metadata sidecar for every executed action, masking password field contents. | `EvidenceCollector` | Covered in `tests/test_browser_layer.py`. | I |
| FR-EVD-002 | The system shall reference evidence by a stable id rather than embedding raw payloads inside reasoning-engine records. | `EvidenceBundle.evidence_ids` (Autonomous Investigation), `EvidenceItem` | `TestEvidenceCollection`. | I |
| FR-EVD-003 | The system shall record console and network errors observed since the last mark, redacting sensitive query parameters from URLs. | `ConsoleMonitor`, `NetworkMonitor`, `sanitize_network_url` | Covered in `tests/test_browser_layer.py`. | I |

### FR-MEM — Memory

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-MEM-001 | The system shall expose one authoritative in-process state object per run that every engine attaches to. | `RunMemory` | Cross-cutting; every engine's `TestMemoryAndController` class. | I |
| FR-MEM-002 | Every RunMemory query method for an unattached engine shall degrade to `None`/`[]` rather than raising. | Every `*_engine`-guarded method in `memory.py` | `test_degrades_gracefully_with_no_*_engine` pattern, repeated per engine. | I |

### FR-COV — Coverage

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-COV-001 | The system shall compute a legacy exploration coverage record (pages/forms/tables/workflow/goal dimensions) from the `ApplicationModel`. | `compute_coverage` (`app/application/coverage.py`) | `tests/test_coverage_dimensions.py`. | I |
| FR-COV-002 | The system shall compute a before/after coverage diff (open graph gaps, feasible-scenario counts) for each completed investigation. | `coverage_confidence_updater.build_coverage_updates` | `TestKnowledgeCoverageConfidenceUpdates`. | I-OPT |
| FR-COV-003 | Reasoning-engine coverage diffs shall be exported into the structured run report alongside legacy coverage. | — | Not found in `ReportBuilder`/`FinalReport` (confirmed via audit: zero references to `investigation`/`qa_strategy`/etc. in `app/reporting/`). | **N** |

### FR-CONF — Confidence

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-CONF-001 | Every discovery/reasoning record shall carry a clamped `[0,1]` confidence score derived from named, inspectable factors. | Each engine's `*_confidence.py`/scoring module | Present across all engines; verified by direct reading. | I |
| FR-CONF-002 | The system shall compute a before/after confidence diff (consistency issues, average scenario confidence) for each completed investigation. | `coverage_confidence_updater.build_confidence_updates` | `TestKnowledgeCoverageConfidenceUpdates`; live-verified (both SauceDemo and ServiceFlow runs produced a populated `ConfidenceUpdates` record). | I-OPT |

### FR-REP — Reporting

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-REP-001 | The system shall build a deterministic structured report from `RunMemory` covering executive summary, modules, navigation, forms/tables, workflows, tests, bugs, coverage, and known limitations. | `ReportBuilder` | `tests/test_reporting.py`. | I |
| FR-REP-002 | The system shall export the report as JSON, Markdown, HTML, and CSV artifacts (bugs, tests, executions, pages, modules) plus Mermaid diagrams. | `ReportExporter` | Filenames confirmed both in code and in live-run log output (`final_report.json/.md/.html`, `bugs.csv`, `tests.csv`, `test_executions.csv`, `page_inventory.csv`, `module_inventory.csv`, `navigation.mmd`). | I |
| FR-REP-003 | The report's executive-summary wording may be optionally polished by an LLM, but must remain grounded in facts already present (no fabricated coverage claims). | `Documenter._optional_polish_executive`, `_summary_stays_grounded` | Present in `documenter.py`. | I |
| FR-REP-004 | The report shall include reasoning-engine (goal/scenario/strategy/investigation) findings. | — | Confirmed absent — see FR-COV-003. | **N** |

### FR-CONFGR — Configuration

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-CONFGR-001 | The system shall expose all run-level toggles through one `RunConfiguration` schema with explicit, safe defaults. | `RunConfiguration` (`app/schemas.py`, 21 fields) | Every field/default in Section 13 of `TECHNICAL_SYSTEM_DOCUMENTATION.md`. | I |
| FR-CONFGR-002 | The system shall default `enable_autonomous_investigation` to `False`. | `RunConfiguration.enable_autonomous_investigation: bool = False` | Directly confirmed in `backend/app/schemas.py`. | I |
| FR-CONFGR-003 | The system shall default `presentation_mode` to `False` and treat it as a distinct feature from autonomous investigation. | `RunConfiguration.presentation_mode: bool = False` | Directly confirmed. | I |

### FR-OBSERV — Observability

| ID | Statement | Source | Acceptance criteria | Status |
|---|---|---|---|---|
| FR-OBSERV-001 | The system shall emit structured trace events for perception, planning, and stop-policy decisions when explicitly enabled. | `GEMMAQA_EXPLORATION_TRACE` env var, `app/utils/exploration_trace.py` | `tests/test_exploration_trace.py`. | I-OPT (env-var gated, off by default) |
| FR-OBSERV-002 | The system shall record a per-run, sequenced, redacted event log for REST replay and WebSocket streaming. | `EventStore` | Confirmed via `app/agent/events.py` + `/ws/runs/{run_id}` route. | I |
| FR-OBSERV-003 | The system shall record every safety decision (allow/block) in an in-memory, secret-redacted audit trail. | `safety_audit` (`app/safety/audit.py`) | `test_audit_trail_records_decision_without_secrets`. | I |

---

## 5. Non-functional requirements

| ID | Requirement | Evidence |
|---|---|---|
| NFR-DET-001 | All reasoning-engine scoring/ordering must be a pure, deterministic function of its inputs — no `random`/wall-clock-dependent branching in scoring paths. | Each engine's own "determinism" test class; confirmed by direct code reading (deterministic ids, sorted iteration). |
| NFR-IDEM-001 | Re-running any reasoning engine's `generate()`/`synchronize()` against unchanged input must not bump its version counter or touch `created_at`. | `begin_pass()`/`end_pass()` + content-diff pattern in every engine's memory module. |
| NFR-PERF-001 | Run-level budgets (`max_actions`, `max_pages`, `max_runtime_seconds`, `max_screenshots`, `max_retries`) bound total resource consumption of a single run. | `SafetyPolicy` defaults; enforced in `ActionValidator.validate`. |
| NFR-REL-001 | A failure in any single non-fatal intelligence hook must not terminate the browser loop. | `try/except` wrapping in every `_run_*` controller method. |
| NFR-FAULT-001 | Faults are isolated per-engine — entity discovery failing must not block actor discovery, and vice versa (separate `try/except` blocks). | `_run_perception_engine` internals. |
| NFR-SAFE-001 | No action reaches the browser without passing the Safety Validator. | Single execution call site, confirmed reached only post-validation. |
| NFR-SEC-001 | Sensitive query parameters and secrets are redacted before storage/logging. | `sanitize_network_url`, `sanitize_dict`, `mask_secret`. |
| NFR-PRIV-001 | Credentials never enter LLM prompts or reports; the vault only exposes non-secret flags (`public_flags()`) to planning logic. | `CredentialVault.public_flags()`; confirmed no credential value is passed to any `GemmaProvider` call site. |
| NFR-MAINT-001 | Each reasoning engine's package uses a single orchestrator class as the only cross-package entry point, with internal collaborators kept private. | Stated and followed convention across all five newest engines' `__init__.py` docstrings. |
| NFR-EXT-001 | Closed vocabularies (frozensets) plus Pydantic validators make adding a new value to an existing category a single-point-of-change edit. | Uniform `_validator_for(allowed, label)` pattern across all reasoning-engine schemas. |
| NFR-TRACE-001 | Every discovered/reasoned record carries evidence references and (where applicable) source graph node/edge ids back to its origin. | `supporting_evidence`, `source_graph_nodes`/`source_graph_edges` fields throughout. |
| NFR-OBSERV-001 | Structured, queryable trace events exist for perception and planning decisions. | `exploration_trace.py`, `auth_trace.py`. |
| NFR-SCALE-001 | No requirement claims multi-run or multi-node horizontal scaling; each run is a single in-process `AgentController` instance. | `RunManager` spawns one `asyncio.create_task` per run within a single process — confirmed, no distributed-worker code found. |
| NFR-PORT-001 | Browser execution is abstracted behind `BrowserAdapter`, allowing a swap between in-process Playwright and the Playwright MCP protocol without changing `ActionExecutor`/`Planner` code. | `create_browser_adapter()` factory. |
| NFR-TEST-001 | Every reasoning engine ships synthetic-fixture unit/integration tests plus at least one real live-verification run. | Confirmed per-engine test files + `evidence/live_*.json` captures. |
| NFR-USE-001 | API responses degrade gracefully (empty lists/`None`) rather than erroring when an engine or feature was never attached/enabled. | `RunMemory` query passthrough pattern. |
| NFR-EXPLAIN-001 | Every priority/confidence/risk score is decomposable into named contributing factors, never a single opaque number. | `priority_breakdown`, `ScoreFactor`, `applied_penalties` fields throughout. |

---

## 6. External interface requirements

| Interface | Description | Source |
|---|---|---|
| **Browser interface** | Real browser session (Chromium via Playwright), headless by default. | `BrowserManager` |
| **Browser adapter interface** | Abstract contract (`BrowserAdapter`) with two implementations (`direct_playwright`, `playwright_mcp`), selected via `Settings.browser_adapter`. | `app/browser/adapters/` |
| **Model-provider interface** | `GemmaProvider` ABC with `mock`, `openai_compatible` (Ollama/LM Studio/vLLM/hosted API), and `transformers` (local weights) implementations, selected via `Settings.gemma_provider`. | `app/gemma/` |
| **API interface** | FastAPI REST + one WebSocket endpoint (`/ws/runs/{run_id}`) — run CRUD, page/module/navigation/form/coverage/workflow/test/action/bug/report/evidence queries, CSV/Markdown/HTML/JSON export, health checks, presentation-mode control. | `app/api/` (routers: `runs`, `reports`, `websocket`, `ai`, `health`, `presentation`) |
| **Configuration interface** | Environment-variable-backed `Settings` (`.env`) plus per-run `RunConfiguration` payload on `POST /api/runs`. | `app/config.py`, `app/schemas.py` |
| **File/report outputs** | JSON/Markdown/HTML/CSV report files, Mermaid diagrams, screenshots, Playwright trace archives — all under `evidence/<run_id>/`. | `app/reporting/exporters.py`, `app/browser/evidence.py` |
| **Logging/tracing interfaces** | `GEMMAQA_EXPLORATION_TRACE` and `GEMMAQA_AUTH_TRACE` env-var-gated structured trace logs; `EventStore` for run-event history/streaming. | `app/utils/exploration_trace.py`, `app/utils/auth_trace.py`, `app/agent/events.py` |
| **Authentication/session interfaces** | `AuthenticationStrategy` (target-application login/registration), `CredentialVault` (ephemeral secret storage) — distinct from any GemmaQA-operator authentication (none implemented; GemmaQA itself has no user-login system in this codebase). | `app/agent/auth_strategy.py`, `app/agent/credentials.py` |

---

## 7. Data requirements

| Data class | Requirement | Notes |
|---|---|---|
| Graph data | Nodes/edges must carry stable, deterministic ids and a `graph_version`; staleness must be explicit, never silently dropped. | `app/intelligence/knowledge_graph/schemas.py` |
| Registry data | Every discovery record must retain its full evidence list, not just a rolled-up confidence number. | Confirmed across all four discovery engines |
| Scenario data | Steps must never embed a concrete selector or URL (semantic-only). | `ScenarioStep` schema |
| Evidence references | Must be reference-only (UUID + disk path); no large binary payload embedded inline in a reasoning-engine record. | `EvidenceItem`, `EvidenceBundle` |
| Execution history | `InvestigationResult` history must be append-only and queryable by outcome. | `InvestigationMemory`, `InvestigationQueryEngine` |
| Versioning | Every reasoning engine's memory must expose a monotonically non-decreasing version counter that only advances on genuine change. | Confirmed pattern across all five newest engines |
| Retention | Only run metadata + action/page/bug/evidence/event logs persist to the SQLite database; reasoning-engine memory is in-process only for the run's lifetime. | `app/database.py`, `app/models.py` — **explicitly not** a full data-retention guarantee for reasoning output (see Architecture Section 13) |
| Secret-handling | Credentials must never be logged, embedded in prompts, or included in reports; only non-secret flags may be surfaced. | `CredentialVault.public_flags()`, `mask_secret`, `sanitize_dict` |

---

## 8. Safety requirements

| ID | Requirement | Status |
|---|---|---|
| SAFE-REQ-001 | Every action must be validated before execution (budgets, scope, prohibited-intent, write-permission). | Implemented |
| SAFE-REQ-002 | Prohibited operations (delete, payment/purchase/checkout, transfer funds, invite user, bulk delete/update, production config, etc. — 29 named patterns) must be blocked regardless of model output. | Implemented |
| SAFE-REQ-003 | Mutating writes (`FILL`/`SELECT`/`CHECK`/`UNCHECK`) must be blocked unless `safe_mode=False` or an explicit controlled/safe-test-write flag is set, and safe test-data values must be prefixed for identifiability. | Implemented |
| SAFE-REQ-004 | Destructive-action approval, if ever enabled (`allow_destructive_actions`), must not loosen destructive text-pattern blocking. | Implemented (`test_allow_financial_actions_never_loosens_destructive_patterns`; the financial-only flag is confirmed not to touch destructive patterns) |
| SAFE-REQ-005 | Action/page/screenshot/runtime/retry budgets must be enforced and cause a clean stop, not a crash. | Implemented |
| SAFE-REQ-006 | Cancellation must be honored (run-level `stop_reason`) and the Autonomous Investigation Engine must recognize it as `user_cancellation`. | Implemented |
| SAFE-REQ-007 | Investigation-level stop conditions (budget exceeded, repeated failures, no executable scenarios) must exist independently of the run-level stop policy. | Implemented (opt-in feature) |
| SAFE-REQ-008 | A scenario-level safety gate must reject a prohibited-risk or blocked-feasibility scenario before any of its steps are attempted. | Implemented (opt-in feature) |
| SAFE-REQ-009 | An explicit human approval step for high-risk actions before execution. | **Not implemented** — no "approval-required" workflow exists in code; the closest analog is the deterministic safety gate itself, which is automatic, not a human-in-the-loop approval queue. |

---

## 9. State and lifecycle requirements

### 9.1 Goal lifecycle
Statuses observed in `InvestigationGoal`/`GoalStatistics` handling: goals are generated, prioritized, and (per the engine's own vocabulary) may become superseded/duplicate/already-verified — reflected as scoring penalties rather than a separate state machine object. (The legacy, unrelated `ExplorationGoal` has its own explicit lifecycle: `proposed → active → completed | blocked | deferred | abandoned`, with `completed`/`abandoned` terminal.)

### 9.2 Scenario lifecycle
`InvestigationScenario.status`: `draft → feasible | conditionally_feasible | blocked | incomplete → superseded | selected | rejected | completed | failed | inconclusive | stale` (the last five reserved for QA Strategy/Autonomous Investigation feedback, per `ENGINE_PRODUCIBLE_STATUSES` vs. the full `SCENARIO_STATUSES` vocabulary — Scenario Planning itself only ever produces the first group).

### 9.3 Strategy candidate lifecycle
`ExecutionCandidate.recommended_action`: `execute | defer | block | skip`, refined in a second cross-candidate pass after redundancy/dependency analysis (see `strategy_candidate_builder.refine_actions`).

### 9.4 Investigation lifecycle
16-state machine (`InvestigationStateMachine`): `idle → preparing → ready → executing → waiting → observing → collecting_evidence → verifying → planning_next → (ready | updating_knowledge) → (completed | blocked | failed)`, with `recovery` and `paused`/`cancelled` as side-branches from most non-terminal states. See `docs/system/MODULE_AND_ENGINE_COMMUNICATION_FLOW.md` for the full diagram.

---

## 10. Acceptance criteria

System-level acceptance, based on what is actually tested and live-verified today:

1. Full backend test suite passes: **1133 passed, 1 skipped, 0 failed** (verified by direct rerun during this audit).
2. A live run against an authorized public target (historically SauceDemo and ServiceFlow; currently the Thinking Tester Contact List by default) completes without exception, with autonomous investigation both disabled (default) and enabled (opt-in), per each engine's own live-capture evidence file under `evidence/`.
3. With autonomous investigation enabled, zero duplicate executions and zero prohibited-risk executions occur across a full run (confirmed in both live captures referenced in `docs/AUTONOMOUS_INVESTIGATION_ENGINE.md`).
4. Every reasoning engine's `generate()`/`synchronize()` is idempotent under an unchanged input snapshot (unit-tested per engine).
5. No action reaches `ActionExecutor` without a preceding, passing `ActionValidator.validate()` call (structurally guaranteed — single call site).

---

## 11. Traceability matrix

| Requirement ID | Component | Source path | Tests | Status |
|---|---|---|---|---|
| FR-OBS-001..004 | Perception | `backend/app/perception/` | `test_perception_engine.py`, `test_canonical_page_model.py`, `test_visual_observation.py` | I / I-OPT |
| FR-ENT-001..003 | Entity Discovery | `backend/app/intelligence/entity_discovery/` | `test_entity_discovery.py` | I |
| FR-ACT-001..003 | Actor Discovery | `backend/app/intelligence/actor_discovery/` | `test_actor_discovery.py` | I |
| FR-WFL-001..003 | Workflow Discovery | `backend/app/intelligence/workflow_discovery/` | `test_workflow_discovery.py` | I |
| FR-DEP-001..004 | Dependency Discovery | `backend/app/intelligence/dependency_discovery/` | `test_dependency_discovery.py` | I |
| FR-KG-001..004 | Knowledge Graph | `backend/app/intelligence/knowledge_graph/` | `test_knowledge_graph.py` | I |
| FR-GOAL-001..003 | Goal Generation | `backend/app/intelligence/goal_generation/` | `test_goal_generation.py` | I |
| FR-SCN-001..004 | Scenario Planning | `backend/app/intelligence/scenario_planning/` | `test_scenario_planning.py` | I |
| FR-STR-001..004 | QA Strategy | `backend/app/intelligence/qa_strategy/` | `test_qa_strategy.py` | I |
| FR-INV-001..009 | Autonomous Investigation | `backend/app/intelligence/autonomous_investigation/` | `test_autonomous_investigation.py` | I-OPT |
| FR-PLAN-001..003 | Runtime Planner | `backend/app/agent/planner.py`, `frontier.py`, `priority_engine.py` | `test_priority_engine.py`, `test_frontier_navigation_priority.py`, `test_goal_ranking.py` | I |
| FR-SAFE-001..004 | Safety Validator | `backend/app/safety/` | `test_safety_and_parser.py`, `test_security_hardening.py` | I |
| FR-EXEC-001..003 | BrowserAdapter/ActionExecutor | `backend/app/browser/` | `test_adapter_execution.py`, `test_browser_layer.py` | I |
| FR-EVD-001..003 | Evidence | `backend/app/browser/evidence.py`, monitors | `test_browser_layer.py` | I |
| FR-MEM-001..002 | RunMemory | `backend/app/agent/memory.py` | per-engine `TestMemoryAndController` classes | I |
| FR-COV-001..003 | Coverage | `backend/app/application/coverage.py`, `coverage_confidence_updater.py` | `test_coverage_dimensions.py`, `TestKnowledgeCoverageConfidenceUpdates` | I / I-OPT / **N** |
| FR-CONF-001..002 | Confidence | per-engine confidence modules | throughout | I / I-OPT |
| FR-REP-001..004 | Reporting | `backend/app/reporting/` | `test_reporting.py` | I / **N** |
| FR-CONFGR-001..003 | Configuration | `backend/app/schemas.py` (`RunConfiguration`) | cross-cutting | I |
| FR-OBSERV-001..003 | Observability | `exploration_trace.py`, `events.py`, `audit.py` | `test_exploration_trace.py`, `test_security_hardening.py` | I / I-OPT |

---

## 12. Known limitations and out-of-scope items

The following are **explicitly not requirements today** — they are noted here so they are never mistaken for completed work:

- Human-in-the-loop approval workflow for high-risk actions (SAFE-REQ-009) — not implemented.
- Exporting reasoning-engine (goal/scenario/strategy/investigation) output into the persisted report/database (FR-COV-003, FR-REP-004) — not implemented; this data exists only in in-process `RunMemory` for the run's duration.
- Cross-run learning / persistent Knowledge Graph reuse across separate runs — not implemented.
- Automated multi-actor session orchestration within a single run — not implemented (by design; see FR-DEP-004).
- A structured metric-value extraction subsystem for assertion verification — not implemented; current verification is heuristic (before/after text comparison or last-action success/failure), and honestly reports `inconclusive` where no reliable signal exists.
- Formal, independently-tracked `coverage_target_reached`/`time_exceeded`/`max_actions_reached` stop reasons for the Autonomous Investigation Engine — currently implied via the same underlying budget/stop-reason signals the overall run already tracks, not separately measured.
- InsightBoard as a live-verification target — referenced in historical evidence/docs/tests but not reproducible in this repository (no application source present).

Do not treat any bullet above as "in progress" — none has a corresponding implementation task in the current codebase; they are documented as possible future work only (see `docs/system/GEMMAQA_ARCHITECTURE.md` Section 14).

# QA Reporting Upgrade — Reasoning-Engine Visibility

Extends `app.reporting.report_builder.ReportBuilder` so a report reader can
clearly distinguish page exploration, form discovery/inspection/testing,
generated vs. executable vs. executed scenarios, CRUD workflows, autonomous
investigations, evidence-backed verification, and blocked vs. untested work
— and so the five newer intelligence engines (Knowledge Graph, Goal
Generation, Scenario Planning, QA Strategy, Autonomous Investigation), which
previously had zero representation in the final report, are now fully
surfaced.

## Two parallel scenario systems, both now reported honestly

GemmaQA has always had two independent scenario mechanisms:

1. **Legacy** — `app.agent.tester.Tester` generates `TestScenario`/
   `TestExecution` records deterministically from observed forms. This is
   what previously showed "76 generated scenarios, all `not_run`" with no
   explanation. Every `not_run` entry now gets a reason via
   `classify_legacy_scenario_reason()` (see **Test Scenarios** /
   **Unexecuted Scenarios**, rows tagged `"source": "legacy_tester"`).
2. **Reasoning pipeline** — Knowledge Graph → Goal Generation → Scenario
   Planning → QA Strategy → Autonomous Investigation. This produces
   `InvestigationScenario` records with real feasibility/eligibility
   classification and, when a real reasoning provider drives them,
   `InvestigationResult`/assertion outcomes. This is the system the
   **Scenario Planning Summary** / **QA Strategy Summary** / **Autonomous
   Investigation Summary** / **Blocked Scenarios** / **Executed Assertions**
   sections report on.

The two are never conflated — the report always states which system each
number/section comes from.

## New report sections (`app/reporting/report_builder.py::REPORT_SECTION_ORDER`)

Configuration and Capability Disclosure, Form Lifecycle, Collection/Grid
Coverage, Knowledge Graph Summary, Generated Goals, Scenario Planning
Summary, QA Strategy Summary, Autonomous Investigation Summary, CRUD
Workflow Coverage, Executed Assertions, Blocked Scenarios, Unexecuted
Scenarios, Temporary Records, Cleanup Status, Stop Reason. Every section has
a matching `FinalReport` field (JSON-exported automatically) and a markdown
renderer; an engine that isn't attached this run renders a clear
"not available" note, never a crash or an empty table pretending to be zero
coverage.

## New coverage metrics (`app.schemas.CoverageRecord`)

`collections_discovered`/`collections_inspected`, `local_controls_discovered`/
`local_controls_exercised`, `goals_generated`, `scenarios_generated`/
`scenarios_executable`/`scenarios_executed`/`scenarios_passed`/
`scenarios_failed`/`scenarios_blocked`, `crud_create/read/update/delete_
discovered/executed`, `cleanup_pending/succeeded/failed/manual_required`,
plus matching `*_coverage_pct` fields and four new `CoverageDimension`
entries (`collection`, `local_control`, `scenario_execution`, `cleanup`) in
`app.application.coverage`.

**"CRUD read coverage" note:** `app.intelligence.crud_discovery` only tracks
`create`/`edit`/`delete` (`CRUD_OPERATIONS` — no `read` operation exists
there). Read coverage instead reads `collections_discovered`/
`collections_inspected`: a record-collection actually being observed and
reasoned about about (not merely rendered) IS the read/view operation for
CRUD purposes. This is stated explicitly in the report, never silently
substituted.

**Never conflate page coverage with test coverage:** the pre-existing
disclaimer/coverage_notes discipline is preserved and extended — pages
visited/discovered remains an EXPLORATION metric; scenario/CRUD/assertion
counts are separate, independently-zero-able numbers (see
`test_page_coverage_and_scenario_execution_coverage_are_independent_numbers`).

## Blocked / unexecuted reason vocabulary (`app/reporting/blocked_reasons.py`)

A closed 14-value vocabulary (`BLOCKED_REASONS`) — `autonomous_mode_
disabled`, `provider_incapable`, `controlled_writes_disabled`, `unsafe`,
`missing_actor`, `missing_data`, `unsupported_control`, `missing_
verification`, `dependency_unresolved`, `stale`, `duplicate`, `planner_
unavailable`, `budget_exhausted`, `other`. `classify_scenario_blocked_
reason()` maps the reasoning pipeline's own internal vocabularies
(`eligibility_classifier.ELIGIBILITY_STATUSES`, `STOP_REASONS`) onto this
set, with run-level conditions (autonomous mode off, provider incapable)
checked first since they explain the whole run, not one scenario.
`classify_legacy_scenario_reason()` gives the older Tester generator a
coarser (but never absent) reason. No code path ever reports a bare
`not_run` without a reason.

## Capability disclosure (`ReportBuilder._capability_disclosure`)

Provider, model, provider capability mode, browser adapter, autonomous
investigation enabled/disabled, controlled writes enabled/disabled, safe
mode, destructive actions allowed/blocked, actor switching available/
unavailable (from configured credential profile count), visual perception
enabled/disabled, test-data generation enabled/disabled. When the provider
is Mock and therefore `exploration_only`, a prominent notice states plainly
that generated-scenario counts are not a claim of autonomous CRUD testing
capability.

## Stop summary (`ReportBuilder._stop_summary`)

Answers, every run: why did it stop, what was the last meaningful
operation, what remained untested (scenario/form/CRUD counts), what
prevented execution (the set of blocked-reason values actually seen this
run), what configuration should change next run (derived directly from
which reasons were present), and how many temporary records were left
behind (pending + manual-cleanup-required).

## Files changed

- `backend/app/schemas.py` — `CoverageRecord`/`FinalReport` new fields.
- `backend/app/application/coverage.py` — new metric computation + 4 new dimensions.
- `backend/app/agent/memory.py` — `known_collection_ids`/`remember_collections`, new `coverage()` inputs.
- `backend/app/agent/controller.py` — one-line call to `remember_collections` alongside perception.
- `backend/app/reporting/blocked_reasons.py` (new) — the reason vocabulary + classifiers.
- `backend/app/reporting/report_builder.py` — 15 new sections, capability disclosure, stop summary.

## Tests

`tests/test_qa_reporting_upgrade.py` (20 tests) — new-engine data presence, degradation when absent, blocked/unexecuted reasons, autonomous-mode and Mock-capability disclosure, CRUD coverage correctness, page-vs-execution coverage independence, stop-reason presence, temporary-record cleanup reporting, and backward compatibility (legacy fields/sections still populate, JSON round-trips, `CoverageRecord` defaults safely for old callers).

## Remaining limitations

- **Executed Assertions is bounded to 200 rows** and **Blocked/Unexecuted Scenarios to 100/200 rows** per report — silently never a claim of exhaustiveness for very large runs; the underlying counts in `*_summary`/`coverage` are exact, only the row-level detail lists are capped.
- **Actor switching availability** is inferred from configured credential-profile count (`> 1`), not from an observed in-run actor switch — a run configured with two profiles but that only ever used one still reports "available".
- **Visual perception enabled** reads the server's global `effective_gemma_supports_images` setting, the same source `AgentController` itself uses to decide whether to wire in visual analysis — it is not a per-run override.
- **Local-control discovery** counts interactive elements with no `href` across every `PageState` observed — it is a structural approximation (any non-anchor control), not a claim that every such control is meaningfully "explorable".

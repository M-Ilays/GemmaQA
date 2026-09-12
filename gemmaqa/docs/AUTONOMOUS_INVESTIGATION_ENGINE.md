# Autonomous Investigation Engine

## Purpose

The Autonomous Investigation Engine is the execution brain: it consumes
the QA Strategy Engine's execution queues, selects the next executable
scenario, drives it step-by-step through the **existing** runtime
`Planner` -> `SafetyValidator` -> `ActionExecutor` -> `BrowserAdapter`
pipeline, verifies assertions against genuinely observed evidence, and
closes the loop by re-running Goal Generation against the Knowledge Graph
the controller's own existing pipeline already keeps in sync. It answers:

> **Given a ranked, batched execution strategy, actually go execute the
> next scenario -- safely, verifiably, and without duplicating work.**

It is strictly **opt-in** (`RunConfiguration.enable_autonomous_
investigation`, default `False`): every existing exploration run's
behaviour is completely unaffected unless a caller explicitly requests it.

It does **not** replace, reimplement, or bypass:

- The runtime `Planner` -- every browser-driving step delegates to
  `Planner.plan_by_priority`, unmodified, biased only by a `testing_
  objective` string derived from the step's own semantic description.
- `SafetyValidator`/`ActionValidator` -- every `BrowserAction` this engine
  produces still passes through the controller's existing per-action
  safety gate, exactly like any other action. This package's own
  `investigation_safety_gate.py` is an *additional*, scenario-level
  pre-check, never a substitute.
- `BrowserAdapter`/`ActionExecutor` -- neither is imported anywhere in this
  package.
- The Knowledge Graph, Goal Generation, Scenario Planning, or QA Strategy
  engines -- all four are consumed read-only, plus one explicit, narrow
  write-back: re-invoking `GoalGenerationEngine.generate()` after the
  graph has already been updated by the pipeline every other action
  already triggers (never a direct graph mutation).

## Architectural position

```
Knowledge Graph
    ↓
Goal Generation Engine
    ↓
Scenario Planning Engine
    ↓
QA Strategy Engine
    ↓
Autonomous Investigation Engine     <- this milestone
    ↓
Runtime Planner  (unchanged)
    ↓
Safety Validator  (unchanged, always enforced)
    ↓
BrowserAdapter / ActionExecutor  (unchanged)
    ↓
Evidence Collection  (unchanged)
    ↓
Knowledge Graph update (via the SAME registry-sync pipeline every action
already triggers)
    ↓
Goal Generation (re-invoked)
    ↓
(loop, via QA Strategy's next-ready candidate)
```

## The core design problem, and why it's solved this way

A `ScenarioStep` (Scenario Planning's own output) is purely semantic and
declarative: `entity_ids`, `workflow_id`, `permission_id`, `actor_id` --
never a concrete DOM selector, element id, or URL. There is no way to
hand-translate a step directly into a `BrowserAction` without either (a)
reinventing the page-understanding logic `FrontierBuilder`/`Planner`
already own, or (b) delegating back to the existing `Planner`. This
package always does (b): every browser-driving step type calls
`Planner.plan_by_priority(page_state, memory, context)` -- the SAME
deterministic, safety-neutral candidate-selection path Gemma's own
fallback already uses -- with `context["testing_objective"]` set to a
short string built from the scenario's title and the step's own
description. The Planner is never told *how* to act, only *what the
current step is trying to learn*.

## Package architecture

Package: `backend/app/intelligence/autonomous_investigation/`

| File | Responsibility |
|---|---|
| `schemas.py` | Result/trace/evidence/update schemas + closed vocabularies (16 execution states, assertion outcomes, failure classes, recovery actions, stop reasons) |
| `state_machine.py` | `InvestigationStateMachine` -- the deterministic 16-state transition table |
| `precondition_validator.py` | Revalidates a scenario's requirements against the CURRENT run state, using the same Knowledge Graph lookup Scenario Planning itself used |
| `investigation_safety_gate.py` | Scenario-level defense-in-depth safety check (never a substitute for `ActionValidator`) |
| `semantic_step_executor.py` | Converts one `ScenarioStep` into a `BrowserAction`-or-observation, delegating browser-driving steps to `Planner.plan_by_priority` |
| `recovery.py` | Failure classification + bounded retry decision |
| `assertion_verifier.py` | Evaluates `ScenarioAssertion`/`ScenarioComparison` against genuinely observed evidence -- honestly `inconclusive` where no reliable signal exists |
| `evidence_bundle_builder.py` | Assembles an `EvidenceBundle` from an investigation's own execution trace (references only) |
| `knowledge_feedback.py` | Re-invokes `GoalGenerationEngine.generate()` and reports which goal ids are new |
| `coverage_confidence_updater.py` | Diffs Knowledge Graph / Scenario Planning statistics from before to after an investigation |
| `investigation_memory.py` | Store for finalized `InvestigationResult` records + the single in-flight `ActiveInvestigation` |
| `investigation_query_engine.py` | The full query API |
| `autonomous_investigation_engine.py` | `AutonomousInvestigationEngine` -- the orchestrator |

## Schemas

`ExecutionTrace`, `AssertionResult`, `VerificationResult`, `EvidenceBundle`,
`KnowledgeUpdates`, `CoverageUpdates`, `ConfidenceUpdates`, `NextGoals`,
`InvestigationResult`, `InvestigationSummary`, `InvestigationStatistics`,
plus the internal mutable `ActiveInvestigation` working record (the ONE
in-flight investigation at a time; everything else is an immutable result).
No field defaults to a random id -- `candidate_id` is always a direct,
stable projection (`investigation_id = f"investigation:{candidate_id}:{attempt}"`),
carrying forward the determinism discipline established across the Goal
Generation -> Scenario Planning -> QA Strategy lineage.

## Execution lifecycle / state machine

16 states: `Idle`, `Preparing`, `Ready`, `Executing`, `Waiting`,
`Observing`, `CollectingEvidence`, `Verifying`, `UpdatingKnowledge`,
`PlanningNext`, `Completed`, `Blocked`, `Failed`, `Cancelled`, `Paused`,
`Recovery`. `InvestigationStateMachine` enforces a fixed allow-list of
transitions (`state_machine.ALLOWED_TRANSITIONS`); an invalid transition
is silently rejected (returns `False`), never raises -- consistent with
every other non-fatal engine hook in this codebase.

Two entry points, matching the two natural seams the controller's
existing per-action loop already has:

- **`next_action(page_state, run_memory, context, *, planner)`** -- called
  from the PLAN phase, *before* the normal frontier-based
  `Planner.next_action()` call, exactly like `self.presentation.
  next_action()` already is. Selects a candidate if idle (via
  `strategy_engine.query_engine.ready()`, precondition- and safety-gated),
  resolves the current step, and returns a `BrowserAction` to dispatch
  through the controller's *unchanged* VALIDATE -> EXECUTE -> COMPARE
  pipeline, or `None` to fall through to normal exploration. Pure-
  observation steps (`verify_state`, `verify_permission`, `verify_denial`,
  `observe_output`, `compare`) resolve instantly inside this same call,
  advancing to the next step without needing a browser round-trip.
- **`observe_step_result(*, before_state, after_state, result, run_memory,
  action)`** -- called from the post-action hook block (right after
  `_run_qa_strategy()`), with the `ActionResult` this iteration's
  pipeline already produced. Reconciles the in-flight investigation:
  advances to the next step on success, retries or aborts on failure
  (matched to the ORIGINATING action via its own `investigation_step_id`
  metadata tag, so a stray normal-exploration action is never misattributed).

## Precondition validation

Re-checks actor/session/permission/entity/state/workflow/data
requirements against the CURRENT run state -- but critically, against the
SAME source Scenario Planning itself used to resolve them
(`graph.query_engine.find_nodes_by_canonical_name`), not the raw Entity/
Actor/Workflow registries' own `known_*()` filters (which apply stricter
confidence/status thresholds and would silently reject scenarios Scenario
Planning already accepted -- see "Live verification findings" below).
Blocking statuses (`contradicted`, `blocked`) immediately reject the
scenario; deferrable statuses (`unresolved`, `unavailable`, `stale`,
`unknown`, or a not-yet-authenticated required session) mark it deferred
-- tried again later, never blocked outright.

## Safety

Every mutation still passes `ActionValidator` exactly as before -- this
package never bypasses it. On top of that, `investigation_safety_gate.
check_safety()` runs BEFORE a scenario is even attempted: `risk_class ==
"prohibited"`, `feasibility_status == "blocked"`, or any step's own
`safety_class == "prohibited"` all reject the scenario outright, recorded
as an immediate `blocked` outcome (0 steps executed) rather than silently
skipped.

## Recovery

Failure classes: `navigation_failure`, `timeout`, `missing_element`,
`unexpected_dialog`, `page_refresh`, `session_expiry`, `api_failure`,
`network_interruption`, `stale_dom`, `capability_unavailable`, `unknown`
-- classified from the `ActionResult`'s own `error`/`message` text.
Recovery is bounded (`MAX_RETRIES_PER_STEP = 2`): transient classes
(`timeout`, `network_interruption`, `stale_dom`, `unexpected_dialog`,
`navigation_failure`) get a bounded retry; `session_expiry` is never
retried (re-authentication is the normal exploration loop's job, reached
through `Planner.plan_by_priority` on the NEXT investigation attempt, not
re-implemented here); everything else either skips the step (if optional)
or aborts the scenario as `failed`.

## Assertion verification

`ScenarioAssertion`/`ScenarioComparison` share the same `operator`
vocabulary; both evaluate through one internal `_evaluate_operator()`:
`permission_allowed`/`permission_denied` read directly off the last
`ActionResult`'s success/error; text-comparable operators (`appears`,
`disappears`, `changes_to`, `remains_unchanged`, `contains`, `excludes`,
`increases`, `decreases`) compare before/after `PageState.
visible_text_summary` heuristically. Where no reliable signal exists (no
structured metric-value extraction subsystem exists in this codebase, and
building one is out of scope for this milestone), the outcome is honestly
`inconclusive` -- never a fabricated `supported`.

## Evidence

`EvidenceBundle` aggregates evidence ids, screenshot paths, visited URLs,
and console/network errors purely by reference -- every id already
produced by the existing `EvidenceCollector`/`ActionExecutor`, never a
copy of a raw payload.

## Knowledge / coverage / confidence updates

Nothing here mutates the Knowledge Graph directly. Every action this
engine produces flows through the controller's UNCHANGED per-action
pipeline (`_run_workflow_engine` -> `_run_dependency_engine` -> `_run_
knowledge_graph_sync` -> `_run_goal_generation` -> `_run_scenario_
planning` -> `_run_qa_strategy`), so by the time an investigation
finalizes, the graph already reflects whatever was learned. This
package's contribution is purely diff-and-report: `KnowledgeUpdates`/
`CoverageUpdates`/`ConfidenceUpdates` snapshot `GraphStatistics`/
`ScenarioStatistics` at investigation start and again at finalization and
report the delta. `NextGoals` re-invokes `GoalGenerationEngine.generate()`
against the now-updated graph and reports which goal ids are new --
closing the loop without this package ever fabricating a goal itself.

## Stop conditions

Checked at the top of every `next_action()` call (never mid-flight on an
already-started investigation, which is always allowed to finish its
current step): `budget_exceeded` (`run_memory.remaining_action_budget <=
0`), `repeated_failures` (3+ consecutive failed/blocked investigations),
`no_executable_scenarios` (every `ready()` candidate has already reached a
terminal outcome or is deferred), `user_cancellation`
(`run_memory.stop_reason` already set by the run itself).
`coverage_target_reached`/`time_exceeded`/`max_actions_reached` are
covered by the SAME underlying budget/stop-reason signals the overall run
already tracks, rather than duplicated stop logic.

## No duplicate execution

`InvestigationMemory.investigated_candidate_ids` records every
`candidate_id` that has reached ANY terminal outcome (`completed`,
`failed`, `blocked`) at least once; `_try_start_next` never re-selects one
-- see "Live verification findings" below for the bug this fixed.

## Memory and controller integration

`RunMemory` gained an `investigation_engine: Any` field plus:
`current_investigation()`, `completed_investigations()`,
`failed_investigations()`, `blocked_investigations()`,
`paused_investigations()`, `investigation_history()`,
`latest_investigation_evidence()`, `investigation_coverage()`,
`investigation_confidence()`, `investigation_statistics()` -- every
method degrades to `None`/`[]` with no `investigation_engine` attached. A
`memory_snapshot()` block reports live totals-by-outcome and the current
investigation's id/state.

`AgentController.__init__` instantiates `self.memory.investigation_engine
= AutonomousInvestigationEngine()` only when `RunConfiguration.
enable_autonomous_investigation` is `True` (default `False` -- every
other run's `investigation_engine` stays `None`, and `next_action()`/
`observe_step_result()` are simply never called). The PLAN phase checks
it right after the `presentation_mode` branch, before falling through to
`Planner.next_action()`; the post-action hook block calls `self._run_
autonomous_investigation()` immediately after `self._run_qa_strategy()`,
wrapped in the same non-fatal try/except-and-log-a-warning discipline as
every other intelligence engine hook.

## Query API

`InvestigationQueryEngine`: `current_investigation()`, `completed()`,
`failed()`, `blocked()`, `paused()`, `cancelled()`, `history()`,
`result_by_id()`, `latest_evidence()`, `coverage()`, `confidence()`,
`statistics()`.

## Testing

`tests/test_autonomous_investigation.py` -- 48 tests across 14 categories,
built on the same real Scenario Planning -> Goal Generation -> Knowledge
Graph -> QA Strategy pipeline `test_qa_strategy.py` uses, plus a
`ScriptedPlanner` stand-in (never a mocked `Planner` internal, just its
public `plan_by_priority` entry point) driving the full `next_action()`/
`observe_step_result()` cycle to exhaustion: `TestStateMachine`,
`TestPreconditionValidator`, `TestSafetyGate`, `TestSemanticStepExecutor`,
`TestRecovery`, `TestAssertionVerifier`, `TestExecutionLifecycle`,
`TestEvidenceCollection`, `TestKnowledgeCoverageConfidenceUpdates`,
`TestStopConditions`, `TestMemoryAndController`, `TestQueryAPI`,
`TestDeterminismAndIdempotency`, `TestNeutrality`.

Full backend suite: **1133 passed, 1 skipped, 0 failed** (baseline was
1085 passed / 1 skipped / 0 failed before this milestone).

## Live verification

Verified end-to-end (full `AgentController` run, real Playwright browser,
`enable_autonomous_investigation=True`, only the LLM mocked) against:

- **SauceDemo** (`https://www.saucedemo.com/`) -- 7 pages, 25 actions, 5
  investigations, all `completed`, 0 blocked/failed/duplicates/unsafe.
  Knowledge graph grew from version 1 to 5 (37->41 nodes, 80->97 edges)
  across a single investigation; real evidence (12 ids) collected; 5 new
  follow-up goals generated.
- **ServiceFlow** (`http://127.0.0.1:5500`, local demo app, authenticated
  as admin) -- 8 pages, 18 actions, 4 investigations, all `completed`, 0
  blocked/failed/duplicates/unsafe. Knowledge graph grew substantially
  (17->66 nodes across the run); 44 new follow-up goals generated.
- **InsightBoard** -- not available in this environment, same limitation
  already documented in `docs/QA_STRATEGY_ENGINE.md`; no such application
  exists in this repository or on this machine to point the harness at.

Capture harness: `backend/scripts/autonomous_investigation_live_capture.py`,
outputs saved to `evidence/live_autonomous_investigation_saucedemo.json`
and `evidence/live_autonomous_investigation_serviceflow.json`.

### Live verification findings

Two real, generalizable bugs were found and fixed during this milestone
(both caught by running the real pipeline against real fixture data, not
by any unit test in isolation):

1. **Duplicate execution loop.** `_try_start_next` originally re-selected
   whatever candidate `strategy_engine.query_engine.ready()` ranked
   highest every single call -- since completing an investigation doesn't
   itself change that candidate's own priority in the (unchanged) QA
   Strategy snapshot, the SAME candidate was investigated forever, never
   advancing to any other ready candidate. Fixed by adding
   `InvestigationMemory.investigated_candidate_ids` (populated on every
   terminal outcome) and skipping already-investigated candidates in
   `_try_start_next`. Regression test:
   `TestExecutionLifecycle.test_every_ready_candidate_investigated_exactly_once`.

2. **False "prohibited" `safety_class` on every step of permission-related
   scenarios.** `scenario_step_builder._safety_class()` scanned BOTH the
   step's controlled `semantic_action` text AND the goal's free-text
   `title` for `PROHIBITED_INTENT_PATTERNS` -- and one of those patterns
   is the bare word "permission". Any `verify_permission`-type goal (whose
   title legitimately discusses "permission" as an investigation subject)
   therefore had every one of its scenario's steps marked `safety_class =
   "prohibited"`, even though the scenario's own overall `risk_class` was
   `"moderate"` and `feasibility_status` was `"feasible"` -- Scenario
   Planning itself considered the scenario perfectly fine to execute, but
   this engine's own scenario-level safety gate rejected it outright. This
   is the exact same false-positive class already found and fixed once
   this session in `scenario_risk_analyzer.py` (see
   `docs/SCENARIO_PLANNING_ENGINE.md`'s own "Live verification finding"),
   but the fix was never applied to `scenario_step_builder.py`'s own
   `_safety_class()` helper. Fixed by removing `ctx.goal.title` from that
   call -- only the controlled `semantic_action` text is scanned now,
   matching the established precedent. Regression test:
   `TestSafetyGate.test_no_scenario_in_real_pipeline_is_falsely_rejected_for_permission_wording`.

No other generalizable bugs were found. Both live runs completed with
zero blocked/failed investigations, zero duplicate executions, and zero
unsafe (prohibited-risk) executions.

## Limitations

- Assertion verification is heuristic (before/after visible-text
  comparison, or the last action's own success/failure) -- there is no
  structured metric-value extraction subsystem in this codebase. Where no
  reliable signal exists, the honest answer is `inconclusive`, never a
  guess.
- Browser-driving steps are resolved by nudging the existing `Planner`'s
  frontier-based selection with a semantic objective string, not by
  precisely targeting the graph node the step actually references -- the
  Planner may pick a different, merely plausible action on a busy page.
  This is a deliberate trade-off (reuse over invention), not an oversight.
- `coverage_target_reached`/`time_exceeded`/`max_actions_reached` are not
  independently tracked; they are implied by the same
  `remaining_action_budget`/`stop_reason` signals every other stop check
  already uses.
- No InsightBoard-specific verification exists in this environment (see
  above) -- nothing in this engine's logic is application-specific, but
  its behaviour there is unconfirmed by this milestone.

## Definition of done

- [x] Scenarios execute autonomously (via `next_action()`/`observe_step_result()`).
- [x] Runtime Planner reused (`Planner.plan_by_priority`, unmodified).
- [x] BrowserAdapter reused (never imported by this package; reached only
      through the controller's unchanged `ActionExecutor` call).
- [x] ActionExecutor reused (same for the above).
- [x] SafetyValidator always enforced (every action still passes
      `ActionValidator`; this package's own gate is additive).
- [x] Assertions verified (`assertion_verifier.py`, honest outcomes).
- [x] Evidence collected (`EvidenceBundle`, references only).
- [x] Knowledge Graph updated (via the unchanged registry-sync pipeline).
- [x] Coverage updated (`CoverageUpdates`, before/after diff).
- [x] Confidence updated (`ConfidenceUpdates`, before/after diff).
- [x] Follow-up goals generated (`NextGoals`, via `GoalGenerationEngine.generate()`).
- [x] RunMemory integrated (`investigation_engine` field + full query API).
- [x] Controller integrated (PLAN-phase hook + `_run_autonomous_investigation()` post-action hook).
- [x] Query API implemented (`InvestigationQueryEngine`).
- [x] Recovery implemented (`recovery.py`, bounded retries).
- [x] Deterministic (stable ids, stable ordering, no randomness anywhere).
- [x] Idempotent (verified via unit test and live-run consistency checks).
- [x] Documentation complete (this file).
- [x] Live verification complete on SauceDemo and ServiceFlow; InsightBoard
      unavailable in this environment, honestly documented rather than
      fabricated.
- [x] Full backend suite passes: 1133 passed, 1 skipped, 0 failed.

Stopping here as instructed -- no new intelligence engine beyond
Autonomous Investigation begins in this milestone.

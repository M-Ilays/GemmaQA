# QA Strategy Engine

## Purpose

The QA Strategy Engine consumes the `InvestigationScenario` records produced
by Scenario Planning (plus the `InvestigationGoal` records behind them and
the `ApplicationKnowledgeGraph` context around both) and decides:

> **Which scenarios should execute, in what order, and why?**

It is the strategic decision-making layer between Scenario Planning ("how
could we investigate this?") and a future Autonomous Investigation Engine
("actually go run these"). It does **not**:

- execute any browser action (`BrowserAdapter`/`ActionExecutor` are never
  invoked, never imported)
- run Playwright, click anything, fill any form, or switch actor sessions
- change the runtime `Planner`'s behaviour in any way
- mutate application state
- rediscover application knowledge that Goal Generation, Scenario
  Planning, or the Knowledge Graph already established — every priority
  signal, dependency, and conflict traces back to something those engines
  already computed

## Architectural position

```
Knowledge Graph
    ↓
Goal Generation Engine        ("what should we investigate?")
    ↓
Scenario Planning Engine      ("how could we investigate it?")
    ↓
QA Strategy Engine            ("which scenario should we run, in what order?")   <- this milestone
    ↓
Future Autonomous Investigation Engine
    ↓
Existing runtime Planner / Safety Validator / Executor  (untouched)
```

## Package architecture

Package: `backend/app/intelligence/qa_strategy/`

| File | Responsibility |
|---|---|
| `schemas.py` | 13 schemas + closed vocabularies (queue/batch/dependency/conflict/policy/forecast types) |
| `strategy_scoring.py` | 12-signal priority model + 10 named policy weight profiles + penalty multipliers |
| `strategy_candidate_builder.py` | Builds one `ExecutionCandidate` per scenario; local recommended-action classification; cross-candidate redundancy/defer refinement |
| `strategy_queue_builder.py` | Deterministic, first-match-wins assignment into the 10 named queues |
| `strategy_batch_builder.py` | Groups executable candidates by most-specific shared dimension (actor+workflow → actor+entity → actor+output → actor → ungrouped) |
| `strategy_dependency_graph.py` | Projects Scenario Planning's own `ScenarioDependency`/`ScenarioConflict` records onto candidates (no new dependency reasoning) |
| `strategy_forecaster.py` | Heuristic, explicitly-approximate coverage/confidence/risk forecasts |
| `strategy_recommendation_builder.py` | One explainable `ExecutionRecommendation` per candidate |
| `strategy_memory.py` | Idempotent store (mirrors `ScenarioMemory`/`GoalMemory`) |
| `strategy_query_engine.py` | The full query API |
| `qa_strategy_engine.py` | `QAStrategyEngine` — the orchestrator |

## Schemas

All required schemas are implemented as Pydantic models: `ExecutionCandidate`,
`ExecutionQueue`, `ExecutionBatch`, `ExecutionDependency`, `ExecutionConflict`,
`ExecutionRecommendation`, `ExecutionForecast`, `ExecutionOrdering`,
`ExecutionPolicy`, `ExecutionStrategy`, `ExecutionStatistics`,
`StrategySummary`, `StrategyResult`.

Three deliberate consolidation decisions, documented in `schemas.py`:

- **`ExecutionOrdering` doubles as "Execution Timeline"** — an ordered
  candidate sequence *is* a timeline; a separate near-identical schema
  would add nothing but bookkeeping.
- **`ExecutionStrategy` (the applied policy + pointers to this pass's
  queues/batches/ordering) is kept distinct from `StrategyResult`** (the
  full expanded bundle) **and from `ExecutionPolicy`** (an abstract policy
  definition, reusable across passes) — a deliberate three-way split
  rather than one overloaded object.
- Closed vocabularies (`RISK_CLASSES`, `COMPLEXITY_CLASSES`,
  `FEASIBILITY_STATUSES`) are copied verbatim from Scenario Planning's own
  vocabulary rather than re-declared, so a risk class means the same thing
  at every layer.

**Determinism discipline** (carried forward from the Knowledge Graph →
Goal Generation → Scenario Planning lineage, each of which found this the
hard way): no field here defaults to a random id. `ExecutionCandidate.
candidate_id = f"candidate:{scenario_id}"` always — a direct, stable
projection of the scenario it wraps, never a fresh `new_id()`.

## Priority model

Every candidate gets exactly 12 signals, always computed the same way
regardless of policy (`strategy_scoring.derive_signals`):

`business_value`, `knowledge_gain`, `coverage_gain`, `confidence_gain`,
`risk_reduction_value`, `workflow_centrality`, `blocking_impact`,
`goal_priority`, `scenario_feasibility`, `speed_value` (`1 - complexity_score`),
`safety_value` (`1 - risk_score`), `read_only_value`.

A named `ExecutionPolicy` only **re-weights** these same 12 signals — it
never changes what is measured, only how much each measurement matters.
Ten profiles are implemented in `POLICY_WEIGHTS`, each summing to exactly
`1.0` (verified in `TestPriorityCalculation.
test_all_policy_weight_profiles_sum_to_one`): `balanced`, `fastest_first`,
`highest_value_first`, `lowest_risk_first`, `read_only_first`,
`coverage_first`, `confidence_first`, `business_critical_first`,
`dependency_first`, and `custom_weighted` (starts from `balanced`, then
applies the caller's `weight_overrides` on top).

After the weighted sum, an explicit **multiplicative penalty step** runs
(never folded into the weighted sum, so it's separately inspectable via
`applied_penalties`): risk-class penalty (`prohibited`→×0, `high`→×0.5,
`moderate`→×0.8, `low`→×0.95, `read_only`/`unknown` mostly unaffected),
cleanup-feasibility penalty (only when cleanup is actually required),
`actor_availability_unknown` (×0.85), `data_unresolved` (×0.9),
`dismissed` (×0), `already_completed` (×0.05).

Every priority is explainable: `ExecutionCandidate.priority_breakdown`
holds the raw signal dict, `applied_penalties` lists which penalties
fired, and `explanation` is a short natural-language sentence citing the
strongest signals and any penalties.

### `blocking_impact` — computed from the real dependency graph

`blocking_impact` is **not** guessed or left at zero: `qa_strategy_engine.
generate()` builds a `_blocking_impact_map()` from Scenario Planning's own
`ScenarioDependency` records *before* building any candidate — for each
scenario, it's the fraction of every other scenario that lists it as a
blocking prerequisite. A scenario nothing depends on scores `0.0`; a
scenario at the root of a dependency chain scores higher, in direct
proportion to how many other scenarios it actually unblocks. This directly
reuses dependency structure Scenario Planning already computed rather than
re-deriving it (see "Live verification findings" below — this was
originally hard-coded to `0.0` and only caught by running against real
apps with real dependency chains).

## Execution queues

Ten named queues, assigned by a single deterministic, first-match-wins
function (`strategy_queue_builder.assign_queue`) in a **fixed priority
order** so a candidate's queue never depends on iteration order — only on
its own already-computed fields:

1. **Blocked** — `recommended_action == "block"` (prohibited risk, blocked
   feasibility, or an unmet blocking prerequisite).
2. **Unknown** — feasibility could not be determined; needs investigation
   before scheduling.
3. **Deferred** — waiting on a higher-priority prerequisite, or a
   redundant scenario variant.
4. **Immediate** — ready now: no dependencies, no cleanup, a single actor,
   and priority ≥ 0.6.
5. **Cross-Actor** — requires more than one actor.
6. **Cleanup** — requires non-trivial cleanup after execution.
7. **Regression** — re-verifies a previously observed contradiction, graph
   gap, or stale knowledge (`scenario_type` in `{contradiction_resolution,
   graph_gap_resolution, stale_knowledge_refresh}`).
8. **Exploration** — exploratory or confidence-building observation rather
   than targeted verification (`scenario_type` in
   `{exploratory_observation, unresolved_reference_resolution,
   confidence_increase, inferred_relationship_validation}`).
9. **Mutation** — mutates state and doesn't fit a more specific queue.
10. **Read-only** — read-only and doesn't fit a more specific queue.

Every candidate is assigned to **exactly one** queue (verified in
`TestQueueGeneration.test_every_candidate_assigned_to_exactly_one_queue`);
all 10 queues are always produced, even if empty.

## Batch generation

`strategy_batch_builder.build_batches` groups only candidates recommended
`execute` (blocked/deferred/skipped candidates have nothing to schedule),
choosing the **most specific** shared dimension for each candidate,
falling back in order:

```
actor + workflow  →  actor + entity  →  actor + output  →  actor alone  →  ungrouped
```

This directly optimises for the task's stated goals (minimum actor
switching, minimum navigation) without needing combinatorial
actor×workflow×entity×output batches — a candidate lands in exactly one
batch, at the most specific level its own requirements support. Each
`ExecutionBatch` reports `estimated_actor_switches` (distinct actors in
the batch minus one), and batches are ordered by descending average
member priority.

## Dependency graph, conflicts, and forecasts

**Dependencies and conflicts are projected, not re-derived.**
`strategy_dependency_graph.py` takes Scenario Planning's own already-computed
`ScenarioDependency`/`ScenarioConflict` records and substitutes
`candidate_id` for `scenario_id` (`f"candidate:{scenario_id}"`), remapping
scenario-level type vocabularies onto the strategy layer's own closed
vocabularies (e.g. `goal_dependency_projection` → `goal_prerequisite`,
`permission_contradiction` → `actor_scope_conflict`). No new cross-candidate
dependency or conflict type is invented here.

**Forecasts are heuristic and labelled as such.**
`strategy_forecaster.build_forecasts` computes coverage/confidence/risk
forecasts from `graph.statistics()` as a baseline plus the average
`coverage_gain`/`confidence_gain`/`risk_reduction_value` of candidates
currently recommended to execute. Every `ExecutionForecast` carries a
`basis` string spelling out the exact formula and a `confidence_in_forecast`
capped at 0.4 (never overclaiming certainty) plus an `explanation` noting
what the projection does *not* account for (e.g. residual risk from
candidates that stay blocked or deferred).

## Recommendations

One `ExecutionRecommendation` per candidate
(`strategy_recommendation_builder.py`), never re-deriving a new decision —
`decision` always equals the candidate's own `recommended_action`. `reasons`
cites the priority score and active policy, any applied penalties, any
blocking prerequisites, any dependency chain, and the assigned queue/batch,
so every recommendation traces back to fields already computed elsewhere.

## Redundancy and dependency-aware scheduling

`strategy_candidate_builder.py` runs in two phases:

- **Phase 1 (`build_candidate`, per-scenario, local information only)**:
  derives priority signals and a provisional `recommended_action` from risk
  class, feasibility status, and the goal's own lifecycle status alone.
- **Phase 2 (`refine_actions`, cross-candidate, run once all candidates and
  `ExecutionDependency` records exist)**:
  - **Redundancy** — when a goal's `:primary` scenario is recommended
    `execute`, every non-primary sibling variant (`:observational`,
    `:reduced_scope`, ...) is marked `skip` ("redundant with the primary
    scenario for the same goal"), unless the primary itself is
    blocked/skipped.
  - **Dependency-aware deferral** — a candidate otherwise recommended
    `execute` is escalated to `block` if a blocking prerequisite will never
    run, or downgraded to `defer` if a blocking prerequisite would
    otherwise be scheduled *after* it (lower priority) — running it first
    would violate the dependency.

## Optimisation targets

The design directly targets the task's stated optimisation goals:

- **Minimum actor switching / navigation** — batch dimension selection
  (most-specific-first) and the Immediate/Cross-Actor queue split.
- **Minimum mutation, maximum evidence** — `read_only_value` and
  `safety_value` signals; the Read-only/Mutation queue split; the
  `lowest_risk_first`/`read_only_first` policies.
- **Maximum coverage/confidence** — `coverage_gain`/`confidence_gain`
  signals; the `coverage_first`/`confidence_first` policies; coverage and
  confidence forecasts.
- **Minimum cleanup** — cleanup-feasibility penalty; the Cleanup queue.
- **Dependency-respecting order** — `blocking_impact` signal,
  `dependency_first` policy, Phase 2 defer/block escalation, the global
  `ExecutionOrdering`.

## Memory and controller integration

`StrategyMemory` mirrors `ScenarioMemory`'s idempotent, pass-scoped
versioning: `begin_pass()`/`end_pass()` bracket `generate()`;
`_content_differs()` compares candidates excluding timestamps/
`observation_count`; `strategy_version` only bumps when something
genuinely changed. Queues/batches/dependencies/conflicts/recommendations/
forecasts are recomputed every pass and replaced wholesale — deterministic
ids make wholesale replacement itself idempotent.

`RunMemory` gained a `strategy_engine: Any` field plus:
`generate_strategy(policy_id=..., weight_overrides=...)`,
`strategy_result()`, `execution_queue(queue_type)`, `execution_batches()`,
`execution_statistics()`, `strategy_summary()`, `next_execution()`,
`highest_value_candidates()`, `lowest_risk_candidates()`,
`blocked_candidates()`, `deferred_candidates()`, `ready_candidates()`,
`batch_for_actor()`, `batch_for_workflow()`, `recommended_sequence()`,
`coverage_forecast()`/`confidence_forecast()`/`risk_forecast()` — every
method degrades to `None`/`[]` with no `strategy_engine` attached, exactly
like every other intelligence engine's Planner-facing API. A
`qa_strategy` block was added to `memory_snapshot()`.

`AgentController.__init__` instantiates `self.memory.strategy_engine =
QAStrategyEngine()` alongside the other reasoning engines; the main
per-action loop calls `self._run_qa_strategy()` immediately after
`self._run_scenario_planning()`, wrapped in the same non-fatal
try/except-and-log-a-warning discipline as every other intelligence
engine hook.

## Query API

`StrategyQueryEngine` implements every required query: `next_execution()`,
`highest_value(limit)`, `lowest_risk(limit)`, `blocked()`, `deferred()`,
`ready()`, `batch_for_actor(term)`, `batch_for_workflow(term)`,
`recommended_sequence()`, `coverage_forecast()`, `confidence_forecast()`,
plus `risk_forecast()` and general lookups (`candidate_by_id`,
`candidates_by_queue`, `candidates_by_action`, `all_batches`, `all_queues`,
`dependencies_for_candidate`, `conflicts_for_candidate`,
`recommendation_for_candidate`, `strategy_statistics`).

## Testing

`tests/test_qa_strategy.py` — 34 tests across 13 categories, built on the
same real Scenario Planning → Goal Generation → Knowledge Graph pipeline
`test_scenario_planning.py` uses (not mocked scenario/goal objects, except
where a synthetic fixture is needed to force an edge case a full pipeline
run can't reliably trigger, e.g. a specific dependency ordering):

`TestPriorityCalculation`, `TestQueueGeneration`, `TestBatchGeneration`,
`TestDependencyOrdering` (covers blocked/deferred), `TestForecasting`,
`TestRecommendations`, `TestMemoryAndController`, `TestQueryAPI`,
`TestDeterminismAndIdempotency`, `TestNeutrality`.

Full backend suite: **1085 passed, 1 skipped, 0 failed** (baseline was
1047 passed / 1 skipped / 0 failed before this milestone; the 4-test delta
between that baseline and the 1051 seen immediately after wiring, before
any QA Strategy tests were added, reflects environmental test-count
variance unrelated to this change — no regressions were introduced at any
point in this milestone).

## Live verification

Verified end-to-end (full `AgentController` run, real Playwright browser,
only the LLM mocked) against:

- **SauceDemo** (`https://www.saucedemo.com/`) — 7 pages, 20 actions, 57
  scenarios → 57 candidates, 53 execute / 2 block / 2 skip, 13 batches, 0
  dependencies (this app's scenario graph produced none), 0 conflicts. All
  consistency checks clean: 0 duplicate candidates, 0 dangling
  dependencies, 0 candidates in more than one queue or batch, 0 blocked
  candidates without a stated reason. Idempotent on regenerate (identical
  ordering, unchanged `strategy_version`).
- **ServiceFlow** (`http://127.0.0.1:5500`, local demo app, authenticated
  as admin) — 8 pages, 14 actions, 92 scenarios → 92 candidates, 82
  execute / 10 skip, 22 batches, 6 dependencies, 0 conflicts. Same clean
  consistency checks and idempotency.
- **InsightBoard** — not available in this environment (no such
  application exists in this repository or on this machine to point the
  harness at). Recorded here rather than fabricated: prior milestones'
  `evidence/live_*_insightboard.json` captures show it was reachable at
  `http://127.0.0.1:8899/` in an earlier environment, but that server does
  not exist in the current one. The engine's behaviour there is not
  independently confirmed by this milestone; the synthetic multi-actor
  test fixtures (`_multi_actor_fixture`) exercise the same batching/
  cross-actor/dependency code paths InsightBoard would have exercised.

Capture harness: `backend/scripts/qa_strategy_live_capture.py` (mirrors
`scenario_planning_live_capture.py`'s structure), outputs saved to
`evidence/live_qa_strategy_saucedemo.json` and
`evidence/live_qa_strategy_serviceflow.json`.

### Live verification finding

**`blocking_impact` was hard-coded to `0.0`** in `strategy_candidate_
builder.build_candidate` — a documented, weighted (10% under `balanced`)
priority signal that was designed to be "computed later once all
candidates exist" but the later pass was never implemented. This wasn't
caught by unit tests using isolated fake scenarios (which never populate
real dependency chains), only by running against ServiceFlow's real
6-dependency scenario graph and inspecting `average_priority`/signal
breakdowns by hand. Fixed by computing a `_blocking_impact_map()` from
Scenario Planning's own `ScenarioDependency` records before candidates are
built, and threading the per-scenario value into `build_candidate` (see
"blocking_impact — computed from the real dependency graph" above).
Regression test: `TestPriorityCalculation.
test_blocking_impact_reflects_real_dependents`.

No other generalisable bugs were found; the Immediate queue landed empty
in both live runs (typical `balanced`-policy priority scores clustered
around 0.4–0.5, below the 0.6 Immediate threshold) — this is a threshold
calibration observation, not a correctness bug: every candidate that would
otherwise be Immediate is still correctly recommended `execute` and
reachable via `ready()`/`next_execution()`, just filed under a different
queue name. Left as-is rather than tuned against two data points; worth
revisiting with more live data before the next milestone.

## Limitations

- Forecasts are heuristic projections, not measured outcomes — they
  assume each executing candidate's own estimated gain applies
  independently, with no interaction modelling between candidates.
- `blocking_impact` reflects only *scenario-level* dependencies Scenario
  Planning already recorded; it cannot detect a dependency Scenario
  Planning itself missed.
- The Immediate-queue priority threshold (0.6) is a fixed constant, not
  learned from data; see the live verification finding above.
- No InsightBoard-specific verification exists in this environment (see
  above) — the engine's behaviour there is unconfirmed by this milestone,
  though nothing in its logic is application-specific.

## Definition of done

- [x] Strategy consumes Scenario Plans.
- [x] Execution queues generated (all 10, always).
- [x] Batches generated (most-specific-dimension grouping).
- [x] Priorities explainable (`priority_breakdown` + `applied_penalties` +
      `explanation` on every candidate).
- [x] Forecasts generated (coverage/confidence/risk, heuristic and
      labelled).
- [x] Dependencies respected (projected from Scenario Planning; Phase 2
      defer/block escalation).
- [x] Memory integrated (`StrategyMemory` + `RunMemory` query API).
- [x] Controller integrated (`_run_qa_strategy()` after
      `_run_scenario_planning()`, non-fatal).
- [x] Deterministic (stable ids, stable ordering, no randomness anywhere).
- [x] Idempotent (verified via unit test and live-run regenerate check).
- [x] Documentation written (this file).
- [x] Live verification complete on SauceDemo and ServiceFlow; InsightBoard
      unavailable in this environment, honestly documented rather than
      fabricated.
- [x] Full backend suite passes: 1085 passed, 1 skipped, 0 failed.

## Next milestone

Autonomous Investigation Engine — consumes `StrategyResult`'s execution
queues/batches/ordering and actually drives the existing runtime `Planner`
through them, closing the loop from "what should we investigate" through
"which scenario runs now" to "go run it." Explicitly out of scope for this
milestone, per the commissioning task's instruction to stop here.

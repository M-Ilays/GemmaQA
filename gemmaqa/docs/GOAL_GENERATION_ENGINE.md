# Goal Generation Engine

## Purpose

The Goal Generation Engine is the first reasoning engine that **consumes** the
Application Knowledge Graph. Its only responsibility is to answer one question:

> **"What should GemmaQA investigate next?"**

It converts existing graph knowledge — gaps, contradictions, low-confidence
relationships, inferred edges, unverified nodes — into prioritised,
evidence-backed, explainable `InvestigationGoal` records.

**This milestone does not decide *how* to investigate anything.** It does not
implement Scenario Planning, browser execution, actor switching, QA Strategy
selection, or Autonomous Investigation — those are later milestones. It never
rediscovers application knowledge, never mutates the Knowledge Graph or any
upstream registry, and never touches the browser.

## Role: the bridge between Knowledge Graph and Scenario Planning

```
ApplicationKnowledgeGraph
        │
        ▼
GoalGenerationEngine   (this milestone)
        │
        ▼
  Scenario Planning Engine   (future milestone, not implemented here)
```

## Architecture

Package: `backend/app/intelligence/goal_generation/`

| File | Responsibility |
|---|---|
| `schemas.py` | All goal schemas + closed `GOAL_TYPES`/`GOAL_STATUSES`/`GOAL_GROUP_TYPES`/`GOAL_EVIDENCE_TYPES` vocabularies |
| `goal_candidate_builder.py` | Deterministically scans the Knowledge Graph into `GoalCandidate` objects, merging duplicate signals as it goes |
| `goal_deduplicator.py` | A defensive second pass merging any cross-subject-key duplicates the builder didn't already merge |
| `goal_priority.py` | The transparent, fully-explainable priority scoring model |
| `goal_dependency_resolver.py` | Structural goal-to-goal prerequisite edges (never executed, only represented) |
| `goal_grouping.py` | Clusters goals by entity/workflow/actor/output/module/business_process/graph_region/contradiction/gap |
| `goal_explainer.py` | Builds a human-readable `GoalExplanation` + `recommended_goal_type` per goal |
| `goal_memory.py` | The idempotent store (mirrors `KnowledgeGraphMemory`'s upsert pattern) |
| `goal_query_engine.py` | The full query API |
| `goal_generation_engine.py` | `GoalGenerationEngine` — the orchestrator; the only class other GemmaQA code talks to |

## Goal lifecycle

One `generate(graph, iteration)` call runs the full pipeline as a single
idempotency-tracked pass:

1. **Build candidates** — `GoalCandidateBuilder` scans the graph in 8
   deterministic passes (node completeness, gaps, unresolved references,
   contradictions, consistency issues, low-confidence edges, dependency
   edges, inferred relationships, business-rule/ownership/scope edges),
   merging every signal about the same subject into one `GoalCandidate` as it
   goes.
2. **Deduplicate** — `goal_deduplicator.deduplicate()` merges any remaining
   cross-subject-key duplicates (same goal type, same graph nodes) as a
   defensive safety net, emitting a `GoalConflict` record for each merge.
3. **Score** — each candidate's 8 priority signals are derived from the
   graph (`goal_priority.derive_priority_signals`) and combined into a
   `GoalPriority` breakdown (`goal_priority.compute_priority`).
4. **Resolve dependencies** — `GoalDependencyResolver` derives structural
   prerequisite edges between DIFFERENT goals from existing graph structure
   (has_step, acts_on, to_state/from_state, generated_by, requires,
   performed_by, has_permission). A goal with any unmet dependency is marked
   `blocked`.
5. **Explain** — `goal_explainer.explain()` builds a `GoalExplanation` and a
   `recommended_goal_type` (which OTHER goal type to tackle first) for every
   goal.
6. **Group** — `goal_grouping.build_groups()` clusters the final goal list
   along 9 dimensions.
7. **Store** — `GoalMemory.upsert_goal()` idempotently merges into the
   durable store (preserving `created_at`/`observation_count` when content is
   unchanged); goals whose evidence has fully disappeared since the last pass
   are removed (a goal carries no historical value once nothing motivates it
   any more, unlike a graph node/edge).
8. **Evaluate stop conditions** — a set of advisory flags (see below) is
   computed and returned, never acted on by this engine itself.

`goal_status` starts at `pending`, moves to `blocked` when an unmet
dependency exists, and can reach `completed`/`dismissed`/`superseded` — but
**nothing in this milestone's pipeline ever sets those three** (there is no
verification-outcome or scenario-planning feedback loop yet); they exist in
the closed vocabulary for a future engine to use, and `GoalMemory.
mark_goal_status()` is the (currently unused-by-this-engine) entry point for
that.

## Goal identity and deduplication

A goal's identity is a deterministic `subject_key`, built as
`f"{goal_type}:{subject}"` where `subject` is whichever graph object the
investigation is fundamentally about:

- A **node id** for goals about a single entity/workflow/actor/permission/
  operation/entity_state/prerequisite/workflow_branch/output (`verify_workflow`,
  `verify_kpi`, `verify_permission`, ...).
- An **edge id** for goals about a specific relationship (`verify_dependency`,
  `verify_business_rule`, `validate_ownership`, `validate_scope`,
  `increase_confidence`).
- A **gap id** / **contradiction id** / **reference id** for goals with no
  single clean owning node (`resolve_graph_gap`, `resolve_contradiction`,
  `resolve_unresolved_reference`).
- An **inference rule id** for goals about a whole class of inferred
  relationships (`validate_inferred_relationship`, `validate_actor_hand_off`
  — grouped by which of the Knowledge Graph's 7 bounded inference rules
  produced them, never one goal per individual inferred edge).

Every gap, contradiction, consistency issue, or low-confidence signal that
concerns the SAME subject is merged into the SAME candidate's
`supporting_evidence` as the builder scans — this is what satisfies "do not
create multiple goals representing the same investigation" structurally,
rather than as an afterthought. A Step-1 node candidate that ends up with
**zero** accumulated evidence (nothing gapped, contradicted, or
low-confidence about it) is pruned entirely — there is genuinely nothing to
investigate.

## Supported goal types

A **closed** vocabulary of 21 application-neutral categories (unlike the
Knowledge Graph's deliberately open node/edge types — the task explicitly
forbids inventing application-specific goal categories, so a fixed enum is
the right shape here):

`verify_workflow`, `verify_dependency`, `verify_kpi`, `verify_permission`,
`verify_actor_capability`, `verify_state_transition`,
`verify_entity_lifecycle`, `verify_business_rule`, `resolve_contradiction`,
`resolve_graph_gap`, `resolve_unresolved_reference`, `increase_confidence`,
`validate_inferred_relationship`, `validate_workflow_branch`,
`validate_prerequisite`, `validate_dashboard_output`, `validate_report`,
`validate_notification`, `validate_aggregation_rule`,
`validate_actor_hand_off`, `validate_ownership`, `validate_scope`.

## Goal schema

`InvestigationGoal` carries every field the task specifies (`goal_id`,
`goal_type`, `title`, `description`, `priority_score`, `business_value`,
`risk_score`, `coverage_value`, `confidence`, `required_entities/actors/
workflows/outputs/permissions/states/context`, `blocking_gaps`,
`supporting_evidence/graph_nodes/graph_edges`, `contradictions`,
`recommended_goal_type`, `estimated_complexity/actor_count/workflow_depth/
browser_actions`, `goal_status`, `created_at`) plus bookkeeping fields needed
for idempotency and traceability (`updated_at`, `observation_count`,
`graph_version`, `group_id`, `depends_on_goal_ids`, an embedded `priority:
GoalPriority` and `explanation: GoalExplanation`, and the `source_*_ids`
lists that make dependency/query lookups possible without re-deriving them).

`recommended_goal_type` is **not** a duplicate of `goal_type`: it names
whichever OTHER goal type should be tackled first when this goal is blocked
by an unresolved dependency (e.g. a `verify_kpi` goal blocked by its
workflow recommends `verify_workflow`), or the goal's own type when nothing
blocks direct investigation.

`estimated_complexity`/`estimated_actor_count`/`estimated_workflow_depth`/
`estimated_browser_actions` are **rough sizing signals** for a future
Scenario Planning Engine to triage by — they are explicitly not a plan and
this engine never decides how those estimates would be executed.

## Priority model

The task describes priority as combining eight factors with "×" between
them. Multiplying eight independent [0, 1] signals collapses to near-zero
the moment any single factor is low — e.g. a freshly-discovered, poorly
-connected gap would always have `graph_centrality ≈ 0`, which would zero
out an otherwise legitimate, high-business-value goal. Every other
confidence model already in this codebase (`graph_confidence.py`,
`dependency_confidence.py`) uses a weighted combination plus an explicit,
separately-explained penalty step instead of raw multiplication, so
`goal_priority.py` follows the same shape:

```
weighted_score = Σ(signal × weight)   for the 8 signals below
final_score    = weighted_score × penalty_multiplier
```

| Signal | Weight | What it measures |
|---|---|---|
| `business_value` | 0.20 | Heuristic per goal type + consumer-edge boost (`visible_to`/`generated_by`) |
| `risk` | 0.15 | Contradictions present, blocking gaps present, low source confidence |
| `knowledge_gain` | 0.15 | How much investigating this would actually teach us (evidence density + inverse confidence) |
| `coverage_improvement` | 0.15 | Fraction of missing structural completeness (gap count / expected checks) |
| `dependency_impact` | 0.10 | Bounded-depth graph descendant count (how much else depends on this) |
| `graph_centrality` | 0.10 | Normalised node degree relative to the graph's max degree this pass |
| `blocking_severity` | 0.10 | 1.0 if any source gap is `blocking`, else scaled down |
| `confidence_gap` | 0.05 | `1 - source_confidence` |

Penalties (multiplicative, applied after the weighted sum): `already_verified`
(×0.1), `superseded` (×0.05), `duplicate` (×0.2, reserved — true duplicates
are merged before scoring, never scored separately), `low_business_impact`
(×0.5 when `business_value < 0.15`).

Every `GoalPriority` record retains every raw signal, its weight, the
weighted score, every applied penalty, and a human-readable `explanation`
string — nothing is collapsed to just the final number.

## Goal grouping

`goal_grouping.build_groups()` clusters goals along the 9 dimensions the
task specifies:

- **entity / workflow / actor / output** — goals sharing a supporting graph
  node of that type.
- **module** — goals sharing a page, via `appears_on` edges.
- **business_process** — a connected cluster restricted to {workflow,
  workflow_step, entity} node types only (a pragmatic operationalisation:
  the Knowledge Graph has no dedicated "business process" node type of its
  own).
- **graph_region** — any connected component from the Knowledge Graph's own
  `connected_components()` other than the largest one.
- **contradiction / gap** — goals sharing a contradiction id, or goals whose
  source gaps share a `gap_type`.

Each `GoalGroup` exposes `importance` (mean member priority), `coverage`
(mean member confidence), `risk` (mean member risk), and `goal_count`.

## Goal dependencies

`GoalDependencyResolver` derives structural (never executed) prerequisite
edges directly from existing graph structure, matching the task's
illustrative chain — *"Verify KPI depends on Verify Workflow depends on
Verify Transition depends on Verify Permission"*:

- An output-type goal (`verify_kpi`, `validate_dashboard_output`,
  `validate_report`, `validate_notification`, `validate_aggregation_rule`)
  depends on the `verify_workflow` goal for whichever workflow produces it
  (via `generated_by`/`affects` edges).
- A `verify_dependency` goal depends on the `verify_workflow` goal for its
  source workflow.
- A `verify_workflow` goal depends on the `verify_state_transition` goal for
  any state it transitions through, on the `verify_actor_capability` goal
  for any actor performing its steps, and on the `validate_prerequisite`
  goal for anything it `requires`.
- A `verify_state_transition` goal depends on the `verify_actor_capability`
  goal for the actor whose step caused that transition.
- A `verify_actor_capability` goal (when its subject is an actor) depends on
  the `verify_permission` goal for each permission that actor holds.

A goal with any unmet dependency is marked `goal_status = "blocked"`.
Dependencies are represented only — this engine never executes them or
decides an investigation order beyond the advisory `recommended_goal_type`.

## Goal explanations

Every goal carries a `GoalExplanation` built from its own supporting
evidence descriptions plus (when blocked) a note naming its unresolved
prerequisite goals, e.g.:

> This KPI depends on an inferred workflow. The workflow is only partially
> observed. Confidence: 0.54. Investigate 'Verify workflow "job workflow"'
> before 'Verify KPI "open jobs"'.

## Stop conditions

`GoalGenerationResult.stop_conditions` is a set of advisory flags (never
acted on by this engine) that a future caller can read:

- `no_useful_goals_remain` — no pending/blocked goal exceeds a minimal
  priority floor (0.05).
- `only_low_value_goals_remain` — every pending/blocked goal has
  `business_value < 0.2`.
- `coverage_target_reached` — ≥85% of graph nodes have no open gap
  referencing them.
- `confidence_target_reached` — average goal-subject confidence ≥ 0.8.
- `application_sufficiently_understood` — zero open gaps, zero open
  consistency issues, and zero contradictions in the current graph.

## Query API

`GoalQueryEngine` provides: `goal_by_id`, `all_goals`,
`highest_priority_goals(limit)`, `goals_for_actor/_entity/_workflow/_output`,
`goals_for_gap`, `goals_for_contradiction`, `goals_by_type`,
`blocked_goals`/`completed_goals`/`pending_goals`/`dismissed_goals`,
`groups_for_goal`/`all_groups`, and `goal_statistics()`.

## Memory integration

`RunMemory.goal_engine: GoalGenerationEngine` is created once per run in
`AgentController.__init__`, alongside `knowledge_graph`. `RunMemory` exposes:
`generate_goals()`, `goal_generation_result()`, `goal_statistics()`,
`goal_snapshot()`, `goal_summary()`, `highest_priority_goals()`,
`goals_for_actor/_entity/_workflow/_output/_gap/_contradiction()`,
`blocked_goals()`/`completed_goals()`/`pending_goals()` — every method
degrades to `None`/`[]` with no `goal_engine` attached. `memory_snapshot()`
includes a compact `goal_generation` block (total goals, counts by
type/status, average priority, high-priority/blocked/group/dependency
counts, and the top 5 goals) — never the full goal list.

This is **not** a second memory layer: `GoalMemory` only holds the OUTPUT of
goal generation (derived investigation goals), never rediscovered
application knowledge of its own.

## Controller integration

`AgentController` instantiates `self.memory.goal_engine = GoalGenerationEngine()`
alongside the knowledge graph, and calls `self._run_goal_generation()`
immediately after `self._run_knowledge_graph_sync()` in the per-action
intelligence pipeline:

```
Observe → Perception → Entity Discovery → Actor Discovery →
Workflow Discovery → Dependency Discovery → Knowledge Graph →
Goal Generation
```

`_run_goal_generation()` is non-fatal (a failure is logged and exploration
continues, identical discipline to every other intelligence engine hook).
**The Planner is untouched** — it continues using the existing execution
pipeline; this milestone does not wire goal generation into candidate
selection or browser execution in any way.

## Testing

`tests/test_goal_generation.py` (53 tests) covers: goal creation for every
supported trigger (node completeness, gaps, contradictions, consistency
issues, unresolved references, low-confidence edges, dependency edges,
inferred relationships grouped by rule, business-rule/ownership/scope
edges), scoring (breakdown completeness, penalty application), deduplication
(subject-key merge + cross-subject-key safety net), grouping (all 9
dimensions), dependencies (the full KPI→workflow→transition/actor→permission
chain, no self-loops), priority ordering (stable, deterministic),
determinism/idempotency (same graph instance re-run is byte-for-byte
identical; a goal disappears once its last evidence resolves), `RunMemory`
integration, controller non-fatal wiring, the full query API, and
application neutrality (an AST contract test scanning every file in the
package for hardcoded business vocabulary).

## Live verification

The same, unmodified implementation was run against three structurally
different applications via `backend/scripts/goal_generation_live_capture.py`
(a real Playwright browser through `AgentController` + a deterministic mock
LLM provider):

| Application | Goals | Groups | Dependencies | Conflicts | Duplicates | Unlinked evidence |
|---|---|---|---|---|---|---|
| ServiceFlow | 99 | 50 | 13 | 0 | 0 | 0 |
| SauceDemo | 60 | 27 | 0 | 0 | 0 | 0 |
| InsightBoard | 65 | 42 | 6 | 0 | 0 | 0 |

Every goal's `supporting_graph_nodes`/`supporting_graph_edges` resolved to
real, currently-present Knowledge Graph nodes/edges on all three apps (the
"unlinked evidence" check). Priority ordering was sensible and explainable
on all three — e.g. ServiceFlow's top goal was `verify_actor_capability` for
the session actor (high centrality, touches many downstream investigations),
followed by inferred-permission validation and core workflow verification.
No application-specific goal types or vocabulary appeared anywhere.

### Live verification finding and fix

One generalisable bug was found: `GoalGenerationEngine.statistics()` (and
therefore `RunMemory.goal_statistics()`), when called independently after
`generate()` rather than by reading the `GoalGenerationResult` it returned,
always reported `graph_version = 0`. The version patch was only ever applied
to the statistics object embedded in the `GoalGenerationResult`, never to
`GoalMemory` itself, so any later independent call to `goal_statistics()`
built a fresh `GoalStatistics` with the schema's bare default. **Fix:**
`GoalMemory` now tracks `last_graph_version`, set once per `generate()` pass,
and `GoalQueryEngine.goal_statistics()` reads it from there. Regression test:
`test_statistics_graph_version_matches_when_queried_independently`. Verified
live: re-running the ServiceFlow capture showed `graph_version` correctly
reporting 50 (matching the Knowledge Graph's actual version) with identical
goal counts.

### Known observation (not a bug)

`validate_ownership` goals were the single largest category after
`increase_confidence` on all three apps (11–17 goals). This accurately
reflects that Actor Discovery's `known_entities` cross-reference always
produces a `manages` edge at a fixed, moderate confidence (0.4) — every such
edge is genuinely "not yet independently confirmed," so a `validate_ownership`
goal for each is correct, not noise. Some of the underlying `manages` edges
trace back to entities Entity Discovery mis-classified from incidental page
text (e.g. a footer link); that upstream imprecision is a known, previously
accepted limitation of the already-completed Entity/Actor Discovery
milestones, not something this engine should — or does — try to correct.

## Known limitations

- `estimated_browser_actions`/`estimated_workflow_depth`/`estimated_actor_count`
  are coarse heuristics (step counts, degree counts), not a real cost model —
  intentionally, since sizing an investigation is not the same as planning
  its execution.
- `graph_centrality` is a simple normalised-degree proxy, not full
  betweenness centrality — chosen for boundedness and explainability over
  precision, consistent with the Knowledge Graph's own centrality
  simplifications.
- No goal in this milestone's pipeline is ever automatically marked
  `completed`, `dismissed`, or `superseded` — there is no
  verification-outcome or scenario-planning feedback loop yet. The
  vocabulary and `GoalMemory.mark_goal_status()` exist for a future engine to
  use.
- `business_process` grouping is a pragmatic node-type-restricted connected
  component, not a semantically-derived process boundary (the Knowledge
  Graph has no dedicated business-process node type).

## Next milestone

**This milestone does not generate scenarios, execute browser actions,
switch actors, select a QA strategy, or run autonomous investigations.** The
next recommended milestone is a **Scenario Planning Engine** that consumes
`InvestigationGoal` records (ordered by `highest_priority_goals()`,
respecting `depends_on_goal_ids` and `goal_status`) and turns each one into a
concrete, executable sequence of actions.

# Scenario Planning Engine

## Purpose

The Scenario Planning Engine converts an `InvestigationGoal` (from Goal
Generation) into one or more declarative, browser-independent
`InvestigationScenario` records. It answers:

> **"How could GemmaQA investigate this goal safely and obtain sufficient
> evidence?"**

It does **not** answer "what should GemmaQA investigate next?" (Goal
Generation's job), and it does **not**:

- execute any browser action (`BrowserAdapter`/`ActionExecutor` are never
  invoked)
- decide which scenario should run globally (a future QA Strategy
  Engine's job)
- perform actor-session switching (represented declaratively via a
  `switch_actor` step, never performed)
- mutate application state
- create unrestricted test data
- bypass safety controls
- replace the existing runtime `Planner`
- generate implementation-specific Playwright code or brittle
  CSS/XPath selectors
- claim that an outcome occurred, or mark a goal verified merely because
  a plan exists

## Architectural position

```
Knowledge Graph
    ↓
Goal Generation Engine        ("what should we investigate?")
    ↓
Scenario Planning Engine      ("how could we investigate it?")   <- this milestone
    ↓
Future QA Strategy Engine     ("which scenario should we run?")
    ↓
Future Autonomous Investigation Engine
    ↓
Existing runtime Planner / Safety Validator / Executor  (untouched)
```

## Distinction between goals, scenarios, and execution

| | Example |
|---|---|
| **Goal** | "Verify whether completing an entity updates a dashboard counter." |
| **Scenario** | 1. Observe the counter before the workflow. 2. Record baseline + scope. 3. Perform the workflow through the expected actor sequence. 4. Observe the resulting entity state. 5. Observe the counter again. 6. Compare baseline and final values. 7. Capture evidence. 8. Determine supported/contradicted/inconclusive. |
| **Execution** | Future milestone. |

A scenario step is a SEMANTIC planning action ("perform the workflow's
ordered steps", "observe the output again") — never `page.locator(...)`,
a CSS selector, an XPath expression, or `waitForTimeout(3000)`.

## Package architecture

Package: `backend/app/intelligence/scenario_planning/`

| File | Responsibility |
|---|---|
| `schemas.py` | 30 schemas + closed vocabularies (scenario/step types, requirement statuses, risk/complexity classes, checkpoint types, comparison operators, evidence types, gap types, branch types) |
| `scenario_template_registry.py` | Goal-type → scenario-type mapping + reusable step-blueprint templates |
| `scenario_decomposer.py` | Goal validation + bounded graph context loading (`GoalValidation`) |
| `scenario_candidate_builder.py` | Scenario-type resolution (incl. the one genuinely ambiguous case, `verify_permission`) + bounded alternative generation |
| `scenario_actor_resolver.py` | `ScenarioActorRequirement`/`ScenarioPermissionRequirement` resolution |
| `scenario_data_requirement_builder.py` | Entity/state/workflow/output/data requirement resolution |
| `scenario_step_builder.py` | Template → concrete `ScenarioStep`/`ScenarioCheckpoint` expansion + pre/postconditions + observational/reduced-scope variant transforms |
| `scenario_evidence_planner.py` | `ScenarioEvidenceRequirement` planning |
| `scenario_comparison_planner.py` | Before/after `ScenarioComparison` planning |
| `scenario_branch_builder.py` | `ScenarioBranch` construction from graph branches, actor availability, delay uncertainty |
| `scenario_feasibility_analyzer.py` | `ScenarioFeasibilityAssessment` — explainable feasibility |
| `scenario_risk_analyzer.py` | `ScenarioRiskAssessment` + `ScenarioCleanupPlan`/`ScenarioRollbackPlan` |
| `scenario_dependency_resolver.py` | Goal-dependency → scenario-dependency projection |
| `scenario_conflict_detector.py` | `ScenarioConflict` detection (retained, never silently dropped) |
| `scenario_gap_analyzer.py` | `ScenarioGap` analysis (25 of the task's 31 gap types have a real trigger) |
| `scenario_deduplicator.py` | Semantic-signature deduplication |
| `scenario_scoring.py` | Complexity estimation + transparent priority scoring |
| `scenario_memory.py` | Idempotent store (mirrors `GoalMemory`/`KnowledgeGraphMemory`) |
| `scenario_query_engine.py` | The full query API |
| `scenario_serializer.py` | Compact/full/human-readable/executor-handoff-stub serialisation |
| `scenario_planning_engine.py` | `ScenarioPlanningEngine` — the orchestrator |

## Scenario schemas

All 30 required schemas are implemented as Pydantic models: `ScenarioPlan`,
`InvestigationScenario`, `ScenarioCandidate`, `ScenarioStep`,
`ScenarioBranch`, `ScenarioCheckpoint`, `ScenarioObservation`,
`ScenarioAssertion`, `ScenarioComparison`, `ScenarioPrecondition`,
`ScenarioPostcondition`, `ScenarioDataRequirement`,
`ScenarioActorRequirement`, `ScenarioPermissionRequirement`,
`ScenarioEntityRequirement`, `ScenarioStateRequirement`,
`ScenarioWorkflowRequirement`, `ScenarioOutputRequirement`,
`ScenarioEvidenceRequirement`, `ScenarioCleanupPlan`,
`ScenarioRollbackPlan`, `ScenarioRiskAssessment`,
`ScenarioFeasibilityAssessment`, `ScenarioDependency`, `ScenarioConflict`,
`ScenarioGap`, `ScenarioAlternative`, `ScenarioPlanningResult`,
`ScenarioStatistics`, `ScenarioVersion`, plus `ScenarioProvenance`/
`ScenarioEvidenceReference` for pointer-only evidence.

**Determinism discipline**: unlike the Knowledge Graph's evidence objects
(which learned this lesson the hard way — see the Goal Generation
milestone's live-verification finding), **no field in this package
defaults to a random id**. Every id is either required or defaults to `""`
for the constructing code to fill in deterministically. This same fix was
retroactively applied to three Knowledge Graph schemas this milestone's
own stricter cross-construction identity test exposed as latent bugs (see
"Live verification findings" below).

## Scenario lifecycle

```
InvestigationGoal
    ↓
Goal validation (GoalValidation: exists, can_plan, stale?, missing nodes,
                 contradictions, blocked?, unresolved dependencies,
                 already-verified?)
    ↓
Graph context retrieval (bounded context_for_node projection)
    ↓
Scenario template selection (goal_type -> scenario_type, evidence-resolved
                              for verify_permission)
    ↓
Candidate generation (primary + up to 3 bounded, materially-distinct
                       alternatives: observational, reduced_scope,
                       negative_capability)
    ↓
Requirement resolution (actor, permission, entity, state, workflow,
                         output, data)
    ↓
Step decomposition (template blueprints -> concrete ScenarioSteps;
                     observational/reduced_scope variants get their
                     mutating steps actually transformed, not just
                     relabelled)
    ↓
Checkpoint + evidence planning
    ↓
Before/after + comparison planning
    ↓
Branch construction
    ↓
Feasibility analysis
    ↓
Risk / cleanup / rollback analysis
    ↓
Dependency resolution (cross-scenario, needs the full scenario set)
    ↓
Conflict detection (per-scenario + cross-alternative duplicate detection)
    ↓
Deduplication (semantic signature, never title-only)
    ↓
Scoring
    ↓
ScenarioPlanningResult
```

If a goal cannot currently be planned (missing/stale graph context), the
pipeline produces exactly ONE `scenario_type="unknown"`,
`variant="incomplete"` scenario carrying an `insufficient_information` gap
— never a fabricated "complete" plan.

## Goal-to-scenario mapping

A deterministic, closed mapping in `scenario_template_registry.
GOAL_TYPE_TO_SCENARIO_TYPES` — asserted at import time to cover every
`GOAL_TYPES` value:

| Goal type | Scenario type(s) |
|---|---|
| `verify_workflow` | `workflow_verification` |
| `verify_dependency` | `dependency_verification` |
| `verify_kpi` | `metric_verification` |
| `verify_permission` | `permission_positive_verification` **and/or** `permission_negative_verification` (evidence-resolved) |
| `verify_actor_capability` | `actor_capability_verification` |
| `verify_state_transition` | `state_transition_verification` |
| `verify_entity_lifecycle` | `entity_lifecycle_verification` |
| `verify_business_rule` | `business_rule_verification` (added — see "Known limitations") |
| `resolve_contradiction` | `contradiction_resolution` |
| `resolve_graph_gap` | `graph_gap_resolution` |
| `resolve_unresolved_reference` | `unresolved_reference_resolution` |
| `increase_confidence` | `confidence_increase` |
| `validate_inferred_relationship` | `inferred_relationship_validation` |
| `validate_workflow_branch` | `branch_verification` |
| `validate_prerequisite` | `prerequisite_verification` |
| `validate_dashboard_output` | `output_verification` |
| `validate_report` | `report_verification` |
| `validate_notification` | `notification_verification` |
| `validate_aggregation_rule` | `aggregation_verification` |
| `validate_actor_hand_off` | `actor_handoff_verification` |
| `validate_ownership` | `ownership_verification` |
| `validate_scope` | `scope_verification` |

Unknown/unmapped goal types fall through to a generic
`exploratory_observation`-style plan rather than crashing.

## Template registry

Templates are ordered `StepBlueprint` sequences (step_type, semantic_action,
mutation_type, optional checkpoint markers) — never application-specific.
The step builder skips or marks-optional any blueprint whose requirement
never resolved rather than forcing every scenario through the full
template. A shared `_GENERIC_OBSERVATION` template ("establish session →
navigate → observe → capture evidence") backs every scenario type that is
fundamentally "go observe X and capture evidence" rather than a multi-step
workflow (`graph_gap_resolution`, `confidence_increase`,
`inferred_relationship_validation`, `unresolved_reference_resolution`,
`stale_knowledge_refresh`, `exploratory_observation`, `unknown`).

## Requirement resolution

Every requirement (`ScenarioActorRequirement`, `ScenarioPermissionRequirement`,
`ScenarioEntityRequirement`, `ScenarioStateRequirement`,
`ScenarioWorkflowRequirement`, `ScenarioOutputRequirement`,
`ScenarioDataRequirement`) carries `requirement_id`, `status`
(`satisfied`/`satisfiable`/`unresolved`/`contradicted`/`blocked`/
`unavailable`/`stale`/`unknown`), `confidence`, `mandatory`, `resolved`,
`resolution_source`, `missing_reason`, `supporting_graph_nodes/edges`. A
node existing in the graph makes a requirement `satisfied`/`satisfiable`,
never automatically `resolved` in the sense of "confirmed live" — that
distinction stays honest throughout.

## Actor requirements

The **only** actor ever treated as currently available is "current
session" (the dominant actor concept Actor Discovery already produces for
the live-authenticated session this run is exploring as). Every other
actor — including one derived from a workflow's own `performed_by` edges
or, when those are still only pending references, from the workflow
step's raw actor-name evidence — is marked `current_availability="unknown"`,
which downstream feasibility analysis treats as a **conditional**
feasibility reason, never a fabricated "yes". Credentials are never
assumed to exist and never appear in any scenario output.

## Data requirements

`ScenarioDataRequirement` represents data SEMANTICALLY (entity type, field
purpose, required state, uniqueness, scope, ownership, source options,
mutation/cleanup requirement, sensitivity, generation policy) — never a
fabricated concrete value. Only mutating scenarios get data requirements
at all (an observational alternative locates existing evidence and needs
none). `generation_policy` defaults to `reuse_existing` since the graph
can never confirm live data availability without a browser.

## Step decomposition

`ScenarioStep` carries `step_type` (23-value closed vocabulary),
`semantic_action`, `mutation_type`, `reversibility`, `safety_class`,
`depends_on_step_ids` (a simple sequential chain), and
`source_graph_nodes` for every step. The **observational** and
**reduced_scope** alternatives don't just relabel the primary's steps —
`to_observational()`/`to_reduced_scope()` genuinely transform the step
sequence (replacing a mutating `perform_workflow_step`/`trigger_transition`
with a read-only `locate_subject` step, or truncating to the first
workflow action) so they differ MATERIALLY from the primary, not just
cosmetically (see "Live verification findings" — this was a real bug).

## Checkpoints and evidence planning

12 checkpoint types (`initial_context` through `cleanup_complete`) mark
significant points in a scenario. `ScenarioEvidenceRequirement` declares
what evidence would be `sufficient`/`supporting`/`optional`/`contradictory`/
`inconclusive_if_missing` for each step — this engine never captures
evidence itself.

## Before/after and comparison planning

Dependency/metric-verification scenarios follow the 9-step before/after
shape: baseline checkpoint → scope capture → source identification →
triggering action → state verification → optional delay handling
(`wait_for_effect`) → final checkpoint → comparison → evidence
sufficiency. `ScenarioComparison.comparison_operator` is derived **ONLY**
from the underlying dependency edge's `effect_direction` attribute
(`increase`→`increases`, `decrease`→`decreases`); when that's unknown, the
operator stays `unknown` — never a fabricated `remains_unchanged` or an
invented numerical `expected_delta`.

## Branch handling

`ScenarioBranch` is built from three genuine graph/requirement signals,
never invented: (1) real `workflow_branch` nodes (`branches_to` edges),
classified into `approval_rejection`/`success_failure`/
`permission_allow_deny`/`unknown` from their option labels; (2) unresolved
actor-session availability (`actor_session_availability`); (3) a
`wait_for_effect` step's implied `immediate_vs_delayed` uncertainty.

## Alternatives

Bounded to at most 3 alternatives + 1 primary per goal
(`MAX_SCENARIOS_PER_GOAL = 4`): an **observational** alternative for any
mutating scenario type, a **reduced_scope** alternative for a multi-step
workflow, and (for `verify_permission` goals with BOTH positive and
negative evidence) the opposite-polarity scenario as a
`negative_capability` alternative. No cosmetic duplicates — alternatives
differ materially in mutation level, step sequence, and therefore risk.

## Feasibility model

`ScenarioFeasibilityAssessment` computes `blocked_reasons`/
`conditional_reasons` from real signals (unresolved/contradicted/blocked
mandatory requirements, unknown actor-session availability, role-switch
requirement, stale graph context, unresolved goal dependencies,
already-verified underlying knowledge) and is always explainable — e.g.
*"Status: blocked. 29/30 requirement(s) resolved. Blocked because:
required permission 'permission:can_create' is blocked."* (a real example
from ServiceFlow live verification).

## Risk model

`ScenarioRiskAssessment` combines 8 named, individually-inspectable
components (`destructive_mutation`, `irreversible_transition`,
`cross_actor`, `permission_escalation`, `cleanup_uncertainty`,
`prohibited_intent`, `long_workflow`, `evidence_ambiguity`) into a
`final_risk_score`, plus a `risk_class` (`read_only`/`low`/`moderate`/
`high`/`prohibited`/`unknown`). `prohibited_intent` reuses the existing
Safety Validator's `PROHIBITED_INTENT_PATTERNS` as an advisory
planning-time signal — this engine can mark a scenario `prohibited`, but
it never overrides or replaces the live `ActionValidator`, which remains
the sole execution-time authority. **Critically, this scan only looks at
each step's own `semantic_action`** (which, for a real workflow step,
already embeds the step's actual verb/name) — never the goal's or step's
free-text title, which routinely and legitimately discusses "permission"/
"notification"/etc. as the investigation subject (see "Live verification
findings").

## Reversibility, cleanup, and rollback

Every mutating scenario gets a `ScenarioCleanupPlan`
(`cleanup_required`, `cleanup_feasibility`, `unresolved_cleanup_gaps`) and
`ScenarioRollbackPlan` (`rollback_possible`, `rollback_risk`,
`irreversible_reason`). An irreversible step (`reversibility="irreversible"`)
forces `cleanup_feasibility="blocked"` and `rollback_possible=False` —
never a claimed-safe rollback without graph support.

## Scenario dependencies

`ScenarioDependency` projects Goal Generation's own `depends_on_goal_ids`
onto the corresponding PRIMARY scenario pair — the only dependency type
with a genuine, non-fabricated signal behind it (`dependency_type=
"goal_dependency_projection"`). Computed once, across the FULL scenario
set, after all scenarios for all goals are built.

## Conflicts

`ScenarioConflictDetector` retains (never silently drops) 6 real,
evidence-backed conflict types: `permission_contradiction`,
`incompatible_scope`, `mixed_mutation_readonly`, `stale_reference`,
`cleanup_permission_unavailable`, and (across a goal's own alternatives)
`duplicate_alternative`.

## Gaps

`ScenarioGapAnalyzer` implements 25 of the task's 31 gap types with a
genuine graph/requirement trigger (the remaining 6 —
`unknown_data_constraints`, `missing_output_location`,
`missing_baseline_method`, `missing_observation_method`,
`missing_workflow_step`, `missing_scope`'s narrower siblings — are valid
schema members without a dedicated synthesis rule yet, matching the same
pragmatic-coverage precedent set by Goal Generation's own gap analyzer).
Gaps only expose a `recommended_resolution_goal_type`; they never create
a new `InvestigationGoal` themselves.

## Scoring

`scenario_scoring.py` mirrors `goal_generation/goal_priority.py`'s shape:
a weighted sum of 8 signals (`information_gain`, `goal_coverage`,
`evidence_strength`, `feasibility`, `determinism`, `reversibility`,
`graph_confidence`, `scope_clarity`) rather than raw multiplication, for
the same reason — multiplying eight independent [0,1] signals collapses
to near-zero the moment any single one is low. `priority_hint` is the
combined score; complexity is estimated separately (step/actor/branch/
comparison/data/cleanup counts → `trivial`/`simple`/`moderate`/`complex`/
`very_complex`). This engine never selects the globally best scenario —
that is QA Strategy's job.

## Determinism

Every id in this package is either required or deterministically derived
(`scenario:{scenario_type}:{goal_id}:{variant}`, `{scenario_id}:step:{seq}:
{step_type}`, `{scenario_id}:gap:{gap_type}:{index}`, etc.) — never a
random default. Collections are always sorted before iteration or
comparison. Repeated generation against an unchanged goal set is
byte-for-byte idempotent (`created_at`/`observation_count` untouched,
`scenario_plan_version` does not increment).

## Versioning

`ScenarioMemory` tracks `scenario_plan_version`, `last_graph_version`,
`last_goal_generation_count`, and pass-scoped added/updated/stale scenario
ids + added/resolved gap/conflict ids, exactly mirroring the Knowledge
Graph's and Goal Generation's own versioning pattern. A no-op regeneration
never increments the version; deduplication prefers keeping the
`:primary` variant when a genuine signature collision occurs (predictable,
rather than whichever variant happens to sort first alphabetically).

## Memory integration

`RunMemory.scenario_engine: ScenarioPlanningEngine` is created once per
run in `AgentController.__init__`, alongside `goal_engine`. `RunMemory`
exposes `generate_scenarios()`, `scenario_planning_result()`,
`scenario_statistics()`, `scenario_snapshot()`, `scenario_summary()`,
`scenario_by_id()`, `scenarios_for_goal/_actor/_entity/_workflow/_output()`,
`feasible_scenarios()`/`blocked_scenarios()`/`incomplete_scenarios()`,
`scenarios_requiring_actor_switch/_mutation()`, `read_only_scenarios()`,
`high_risk_scenarios()`, `scenario_gaps/_conflicts/_dependencies()` — every
method degrades to `None`/`[]` with no `scenario_engine` attached.
`memory_snapshot()` includes a compact `scenario_planning` block. This is
**not** a second memory layer — `ScenarioMemory` only holds the output of
scenario planning.

## Controller integration

`AgentController` instantiates `self.memory.scenario_engine =
ScenarioPlanningEngine()` alongside the goal engine, and calls
`self._run_scenario_planning()` immediately after
`self._run_goal_generation()`:

```
Observe → Perception → Entity Discovery → Actor Discovery →
Workflow Discovery → Dependency Discovery → Knowledge Graph →
Goal Generation → Scenario Planning → existing runtime continues unchanged
```

`_run_scenario_planning()` is non-fatal. **The Planner is untouched** — it
continues selecting immediate browser actions exactly as before; this
milestone never calls `ActionExecutor`/`BrowserAdapter` and never performs
an actor switch.

## Query API

`ScenarioQueryEngine` implements the full required set: `scenario_by_id`,
`scenarios_by_status/_type/_goal/_feasibility/_risk`, `scenarios_for_actor/
_entity/_workflow/_output/_permission`, `scenarios_requiring_state/
_actor_switch/_mutation`, `read_only_scenarios`, `blocked_scenarios`,
`conditionally_feasible_scenarios`, `scenarios_with_gaps/_conflicts`,
`alternatives_for_scenario`, `dependencies_for_scenario`,
`prerequisites_for_scenario`, `highest_information_gain_scenarios`,
`lowest_risk_scenarios`, `highest_confidence_gain_scenarios` — all
deterministically ordered and bounded by an explicit `limit`.

## Serialisation

`ScenarioSerializer` provides `compact_snapshot()`, `full_snapshot()`,
`bounded_scenario_export()`, `human_readable_summary()`, and
`executor_handoff_stub()` (a forward-looking shape for a future
Autonomous Investigation engine — explicitly labelled as a planning
artifact, never an execution instruction). Nothing in this package's
schemas ever carries a password/token/cookie/secret header/personal test
data, by construction.

## Testing

`tests/test_scenario_planning.py` (107 tests) covers categories A–P from
the commissioning task (goal mapping, scenario identity/determinism,
requirement resolution, step decomposition, before/after planning,
permission scenarios, workflow scenarios, evidence planning, feasibility,
risk, branches/alternatives, dependencies/conflicts, gaps, query API,
memory/controller integration, neutrality) plus all 8 synthetic
integration fixtures (single-actor transition, cross-role approval,
permission denial, branched workflow, incomplete dependency, delayed
output, irreversible mutation, scope-sensitive output).

## Live verification

The same, unmodified implementation was run against ServiceFlow,
SauceDemo, and InsightBoard via
`backend/scripts/scenario_planning_live_capture.py`:

| Application | Goals | Scenarios | Feasible | Cond. feasible | Blocked | Read-only | Mutating | High-risk | Deps | Conflicts | Gaps | Duplicates |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ServiceFlow | 99 | 113 | 100 | 12 | 1 | 92 | 21 | 0 | 33 | 4 | 75 | 0 |
| SauceDemo | 60 | 64 | 62 | 0 | 2 | 53 | 11 | 4 | 0 | 5 | 27 | 0 |
| InsightBoard | 65 | 77 | 61 | 16 | 0 | 64 | 13 | 0 | 16 | 0 | 52 | 0 |

Zero duplicate scenarios, zero self-dependencies, zero unlinked evidence
(every `source_graph_nodes` entry resolved to a real, current graph node),
and zero stale graph references across all three apps. SauceDemo's 4
"high-risk" scenarios were verified to be a **correct** classification: its
cart workflow genuinely contains a `checkout` step (the real SauceDemo
"CHECKOUT" button), which legitimately matches the Safety Validator's
`PROHIBITED_INTENT_PATTERNS` — not a false positive.

### Live verification findings and fixes

Found and fixed via pre-verification testing (before the live runs, per
the mandated "test thoroughly, then live-verify" discipline):

1. **False "prohibited" risk on every permission-related scenario.**
   `PROHIBITED_INTENT_PATTERNS` contains the bare word `"permission"` —
   meant to catch real UI text like "Manage Permissions", but the risk
   analyzer was scanning goal/step **titles**, which routinely and
   legitimately discuss "permission" as the investigation subject (every
   `verify_permission`-derived goal's title does). **Fix:** scan only each
   step's own `semantic_action` (the engine's own controlled vocabulary),
   never goal/step free-text titles. Regression test:
   `test_goal_title_mentioning_permission_does_not_false_positive_prohibited`.
2. **Alternatives were cosmetic duplicates, silently losing the primary
   scenario.** The "observational"/"reduced_scope" alternatives only
   changed `mutation_level` metadata, never the actual step sequence —
   making them byte-for-byte identical to the primary and triggering the
   deduplicator to drop one of them (arbitrarily, by alphabetical id
   order — frequently the PRIMARY). **Fix:** `to_observational()`/
   `to_reduced_scope()` genuinely transform the step sequence; the
   deduplicator now also prefers keeping `:primary` on a genuine
   collision. Regression tests: `test_observational_alternative_for_
   mutating_scenario`, `test_reduced_scope_alternative_for_multi_step_
   workflow`, `test_semantic_duplicates_removed`.
3. **Cross-scenario determinism gap for goals without pre-populated
   `required_actors`.** A `validate_actor_hand_off` goal built from a
   standalone `unresolved_actor_hand_off` gap never got `required_actors`
   populated (only Step-1 node-subject goals do), so the actor resolver
   silently fell back to a single `"current session"` actor instead of
   the workflow's real two actors. **Fix:** added a graph-structure
   fallback (`_derive_actor_terms_from_graph`) that walks from the goal's
   supporting nodes to the owning workflow and collects every actor that
   performs a step in it — including actors only known via a still-
   pending `performed_by` reference. Regression tests: `test_actor_
   unresolved`/`test_fixture_2_cross_role_approval`.
4. **Four Knowledge Graph schemas had random-default ids that broke
   cross-construction determinism one layer up.** `PendingReference.
   reference_id`, `GraphContradiction.contradiction_id`,
   `GraphConsistencyIssue.issue_id`, and `GraphGap.gap_id` all defaulted
   via `Field(default_factory=new_id)` — fine for idempotency WITHIN one
   graph's lifetime (their `add_*` methods already dedup by content), but
   two independently-built, structurally-identical graphs got different
   ids for the same fact, which Goal Generation's `resolve_unresolved_
   reference`/`resolve_contradiction` goals (and therefore this engine's
   scenario ids) inherit directly. Found by this milestone's own stricter
   cross-construction identity test — exactly the kind of latent bug
   the Goal Generation milestone's live-verification finding (random
   `GoalEvidence.evidence_id`) predicted could recur elsewhere. **Fix:**
   all four now default to a deterministic key derived from the same
   content their dedup logic already keys on (e.g.
   `f"pending:{source_node_id}:{edge_type}:{target_hint}"`). Regression
   test: `test_scenario_ids_are_stable_across_runs`. Verified no
   regression in the Knowledge Graph's own 100+ tests.
5. **A truthy-guard silently disabled staleness detection for
   graph_version=0.** `if goal.graph_version and goal.graph_version <
   memory.graph_version` treats a legitimate version-0 goal (from the
   very first synchronise pass) as "not set" and skips the comparison
   entirely. **Fix:** removed the truthy guard —
   `graph_version=0` is a real value, not a sentinel. Regression test:
   `test_stale_graph_context_reduces_feasibility`.

No live-verification-only bugs were found beyond confirming these fixes
work correctly against real applications (e.g. SauceDemo's `checkout`
step correctly triggers `prohibited` risk post-fix, and no
false-positive "permission" scenarios appear anywhere in any of the
three apps' output).

## Known limitations

- `business_rule_verification` is a scenario type added beyond the task's
  illustrative list, to give `verify_business_rule` goals (Goal
  Generation's own closed vocabulary) a non-forced home rather than
  overloading `entity_lifecycle_verification` or `unknown`.
- 6 of the 31 gap types are valid schema members without a dedicated
  synthesis trigger yet (`unknown_data_constraints`,
  `missing_output_location`, `missing_baseline_method`,
  `missing_observation_method`, `missing_workflow_step`, and one
  `missing_scope` variant) — the same pragmatic-coverage trade-off Goal
  Generation's own gap analyzer made.
- `graph_centrality`-equivalent signals aren't used in scenario scoring
  (unlike Goal Generation's priority model); scenario scoring instead
  weights goal-inherited priority (`goal_coverage`) directly.
- API-assisted observation alternatives (mentioned in the task's
  "ALTERNATIVE SCENARIOS" examples) are not generated — the Knowledge
  Graph currently has no populated `api_resource`/`api_operation` nodes to
  ground such an alternative in real evidence, and fabricating one would
  violate "do not fabricate."
- Complexity/runtime estimates are conservative heuristics (step/actor/
  branch counts), not a real cost model — deliberately, since sizing an
  investigation is not the same as planning its execution.

## Next recommended milestone

**This milestone does not select the best scenario, execute anything, or
implement QA Strategy or Autonomous Investigation.** The next recommended
milestone is a **QA Strategy Engine** that consumes `InvestigationScenario`
records (via `highest_information_gain_scenarios()`/`lowest_risk_scenarios()`/
`highest_confidence_gain_scenarios()`, respecting `feasibility_status`,
`dependencies_for_scenario()`, and `risk_assessment`) and selects which
scenario(s) should actually run, given resource and risk budgets.

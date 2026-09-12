# Business Dependency Discovery Engine

GemmaQA reasons beyond "this KPI card exists" — it asks what entity a
visible output summarizes, which workflow produces the underlying data,
which actions increase or decrease it, which states are included/excluded,
which actor produces vs. consumes it, what scope (time/tenant/actor/filter)
applies, and how the dependency could eventually be validated. This engine
connects visible outputs (KPIs, counters, badges, charts, reports, queues)
to candidate entities, workflows, and actors already discovered by
`app.intelligence.entity_discovery`/`actor_discovery`/`workflow_discovery`.

**Status:** implemented, wired into the observation loop
(`app/agent/controller.py`), best-effort/non-fatal, live-verified against
three real applications. **Scope is deliberately narrow** — see
[Non-goals](#non-goals-this-milestone) and
[Known limitations](#known-limitations).

## Non-goals (this milestone)

Per the task that commissioned this engine: no automatic role switching, no
autonomous multi-role scenario execution, no full test-strategy generation,
no destructive dependency experiments, no unrestricted data creation, no
speculative dependency execution without evidence. This engine
**represents** dependencies and verification plans; it never executes them.

## Architecture

```
app/intelligence/dependency_discovery/
    schemas.py                    DerivedOutputDescriptor, MetricDescriptor,
                                   CounterDescriptor, BadgeDescriptor, ChartDescriptor,
                                   ReportDescriptor, QueueDescriptor, SummaryDescriptor,
                                   DependencyDescriptor, DependencyCandidate,
                                   DependencySource, DependencyTarget, DependencyRule,
                                   AggregationRule, StateInclusionRule, StateExclusionRule,
                                   ScopeDimension, TemporalScope, ActorScope, TenantScope,
                                   FilterScope, DependencyEffect, DependencyEvidence,
                                   DependencyGap, DependencyContradiction,
                                   VerificationRequirement, DependencyRegistrySnapshot
    dependency_confidence.py      5-way confidence sub-scores + composite
    output_candidate_builder.py   CanonicalPageModel -> raw DerivedOutputDescriptors
    metric_semantic_analyzer.py   generic metric-type classification + entity/state/
                                   workflow term corroboration
    dependency_candidate_builder.py  output + metric -> raw DependencyCandidates
    dependency_relationship_builder.py  effect-direction inference, cross-role
                                   producer/processor/consumer reasoning
    aggregation_rule_inferer.py   candidate aggregation-rule + inclusion/exclusion
                                   state rules
    scope_dimension_analyzer.py   scope extraction + scope-compatibility checking
    dependency_correlator.py      before/after correlation (the ONLY path to "verified")
    dependency_memory.py          merge/dedupe outputs + dependencies, delay detection
    dependency_registry.py        the queryable catalog (Planner-facing)
    dependency_gap_analyzer.py    DependencyGap generation
    dependency_verification_planner.py  ordered VerificationRequirement plans (never executed)
    dependency_discovery_engine.py  orchestrator (`.observe()`) + tracing
```

Sits alongside `app/intelligence/entity_discovery/`, `actor_discovery/`, and
`workflow_discovery/` (same package family, same guardrails) and **reuses**
their primitives rather than duplicating: `normalize_term`/`url_path_terms`
(entity discovery), the Entity/Actor/Workflow Registries as read-only
cross-reference inputs (never a second application-memory system), and the
exact same before/after-pair controller-hook pattern workflow discovery
established. No generic normalisation/evidence/confidence utility is
duplicated; each package still owns its own `*_confidence.py` (mirroring
`workflow_confidence.py`'s independent-but-parallel design), since the
richness/contradiction/repeat-decay shape differs per domain.

**A note on what did NOT exist to reuse:** `CanonicalPageModel` has no
KPI/chart/metric schema of its own (confirmed by audit before writing any
code) — every derived output here is discovered from generic, already-
existing structural evidence: headings/text_blocks, `card_group`-classified
`PageRegion`s (from the Perception Engine's own region classifier), images
classified `visual_semantic_type == "chart"` (from the Image Extractor),
table totals, alerts, and network evidence. Nothing new was added to
`CanonicalPageModel` itself.

## Pipeline (`DependencyDiscoveryEngine.observe`)

Called once per **executed action** — like Workflow Discovery, before/after
correlation is meaningless from a single observation, so this shares the
exact same before/after `CanonicalPageModel` pair and executed-action
context, via the same controller hook.

1. **`OutputCandidateBuilder.build()`** on both `before_model` and
   `after_model` (see [Derived output discovery](#derived-output-discovery)).
2. For each output anchor (deduped across before/after and across the
   registry's own history): **`MetricSemanticAnalyzer.analyze()`** classifies
   it and finds candidate entity/state/workflow terms (never label-matching
   alone — see [Metric semantic analysis](#metric-semantic-analysis)).
3. **`DependencyCandidateBuilder.build()`** connects the output to
   entity/workflow/actor/network-resource candidates (see
   [Dependency candidate generation](#dependency-candidate-generation)).
4. **`ScopeDimensionAnalyzer.analyze()`** extracts scope (actor/tenant/time/
   filter/tab) for this observation (see [Scope model](#scope-model)).
5. **`AggregationRuleInferer`** infers candidate aggregation rules +
   inclusion/exclusion state rules (see
   [Aggregation inference](#aggregation-rule-inference)).
6. **`DependencyRelationshipBuilder`** infers effect direction from Workflow
   Registry data, and cross-role producer/processor/consumer findings (see
   [Effect-direction inference](#effect-direction-inference),
   [Cross-role reasoning](#cross-role-reasoning)).
7. **`DependencyCorrelator`** attempts before/after correlation whenever a
   prior value for the same output anchor exists (see
   [Before/after correlation](#beforeafter-correlation)).
8. **`DependencyMemory`** merges everything into (new-or-existing) registry
   `DependencyDescriptor`s; **`dependency_confidence`** recomputes the five
   sub-scores + composite + status.
9. **`DependencyGapAnalyzer.analyze()`** (if an entity/workflow registry is
   supplied).
10. **Trace** — one `dependency_discovery.observation` event.

## Core schemas

All required schemas are implemented in `schemas.py` with
`field_validator`-enforced controlled vocabularies, matching the entity/
actor/workflow discovery pattern exactly. `CounterDescriptor`/
`BadgeDescriptor`/`ChartDescriptor`/`ReportDescriptor`/`QueueDescriptor`/
`SummaryDescriptor` are thin, intention-revealing subclasses of
`DerivedOutputDescriptor` (no extra required fields beyond `ChartDescriptor`'s
optional `legend_label`/`series_terms`) — a single underlying shape, named
per the task's own output taxonomy.

**Dependency statuses** (`DEPENDENCY_STATUSES`): `observed`,
`partially_observed`, `inferred`, `candidate`, `verified`, `contradicted`,
`blocked`, `stale`. **`verified` has exactly one path**: a real,
scope-compatible, before/after correlation whose result is `supported` or
`delayed` — never label matching, never terminology corroboration alone,
no matter how confident those make the composite score. This is enforced
structurally in `dependency_memory.merge_execution_observation()` /
`dependency_confidence.status_for(..., verified_observed=...)` — the flag is
computed once, from the correlator's actual output, and threaded through;
nothing else can set it.

## Derived output discovery

`OutputCandidateBuilder` runs six independent detectors per observation,
all reusing exclusion patterns already proven in Workflow Discovery's own
live-testing history (nav-region/tab-membership/tag-role exclusions):

- **KPI cards**: a `card_group`-classified `PageRegion` (the Perception
  Engine's own region-classifier output) containing a heading + a numeric
  text/interactive element.
- **Badges/queues**: any numeric text near a queue/alert/notification hint
  word (`"queue"`, `"backlog"`, `"pending"`, `"alert"`, `"warning"`, ...) —
  generic UI vocabulary, never one application's terms.
- **Charts**: an `ImageDescriptor` already classified `visual_semantic_type
  == "chart"` by the (deterministic, pre-existing) Image Extractor.
- **Table totals**: a table's `"Total"`/`"Sum"`/`"Subtotal"` row, or (much
  lower confidence) its bare `row_count`.
- **Report summaries**: a `text_block` classified `block_type in
  {"summary", "description"}` carrying a number — see
  [Live findings](#live-findings-bugs-found-and-fixed-by-live-testing) #1/#2
  for the two exclusions live testing proved necessary here.
- **Derived form fields**: a disabled (not-enabled) form field carrying a
  numeric value — the only generic, markup-neutral signal available today
  for "this is read-only/system-calculated" (no explicit
  readonly/computed flag exists on `FormFieldDescriptor`).

**Not every number is a KPI.** `_looks_like_non_business_number()` rejects
dates, versions (`v2.4`), phone numbers, identifier-like tokens
(`CUST-001`, `#A1B2`), and bare years. Navigation-numbering, stepper labels,
and pagination-control numbers are excluded structurally by cross-
referencing `model.navigation_regions`/`model.tabs`/`model.pagination`
element ids — the exact same exclusion pattern Workflow Discovery's live
testing already proved necessary for its own candidate builder, reused here
rather than re-derived.

`parse_numeric_value()` safely extracts a plain number, a percentage
(`"42%"` → `42.0`, unit `percent`), or a currency amount (`"$1,234.56"` →
`1234.56`, unit `currency`) — never guessing across ambiguous text; returns
`(None, None, None)` for anything that doesn't parse cleanly or matches a
non-business pattern.

## Metric semantic analysis

`classify_metric_semantic_type()` is an ordered, deterministic keyword-rule
list (percentage → average → ratio → backlog → completion → failure →
warning → alert → capacity → utilisation → duration → age_based →
revenue_like → activity → status_distribution → total → count), falling
back to output-type and unit hints, then `"unknown"`. Every hint is generic
metric-shape English ("total", "average", "backlog", "completed") — never
one application's business nouns.

Candidate entity/state/workflow terms are extracted from the output's label
+ nearby headings via `normalize_term()`, but **a term is only accepted
when it actually matches an already-known Entity/Workflow Registry term**
(canonical name, alias, or a known state) — mirroring
`workflow_candidate_builder._entity_cross_references`'s "match against
ALREADY-known terms" pattern exactly. Every match carries its own
`DependencyEvidence` (`source_kind="entity_registry"` or
`"workflow_registry"`), so a match is always explainable, never a bare
label-similarity guess.

## Dependency candidate generation

`DependencyCandidateBuilder.build()` produces one `DependencyCandidate` per
matched signal:

- Each matched **entity term** → an `entity_dependency` candidate
  (`relationship_type="counts"`, or `"filters_by_state"` when a state term
  also matched).
- Each matched **state term** → a `state_dependency` candidate, cross-
  referencing which entity's `known_states` the state belongs to.
- Each matched **workflow term** → a `workflow_dependency` candidate
  (`relationship_type` defaults to `"creates_alert"`/`"populates_queue"`
  when the output itself is alert/queue-shaped, else `"unknown_effect"`
  pending effect-direction inference).
- The **current session actor** (if known) → an `actor_dependency` candidate
  (`relationship_type="controls_visibility"`) — this output was observed
  under this actor's session.
- **Network resource matches**: `NetworkEvidence.text` (the only network
  data GemmaQA's `NetworkMonitor` captures today — see
  [Known limitations](#known-limitations)) is split into path/word terms
  and matched against known entity/workflow terms, producing
  `aggregate_dependency` candidates.

## Effect-direction inference

`DependencyRelationshipBuilder.infer_effect()` never guesses a formula —
it reads what Workflow Discovery has ALREADY reconstructed:

- For a **state-scoped** candidate: does any `WorkflowTransition` on the
  relevant entity have this state as its `target_state` (→ `increase`) or
  `source_state` (→ `decrease`)? Both → `unknown` (genuinely ambiguous,
  never a false claim).
- For an **entity- or workflow-scoped** candidate: the entity's/workflow's
  own `WorkflowStep.semantic_action`s are checked against a generic verb→
  direction map (`create`→`increase`, `delete`/`archive`→`decrease`,
  `assign`/`approve`/`reject`/`complete`/`cancel`→`move_between_categories`,
  `acknowledge`→`decrease`, `restore`/`refund`→`increase`, `pay`→`decrease`,
  ...) — preferring an actually-**observed** step over a merely
  structurally-present one, at correspondingly higher confidence.

Every inferred direction is a CANDIDATE (`DependencyEffect.confidence` stays
low, 0.1–0.3) until a before/after correlation confirms it.

## Aggregation rule inference

`AggregationRuleInferer.infer_rules()` maps metric semantic type + output
type + scope to candidate `AggregationRule`s (`count_all`, `count_in_state`,
`count_by_actor`, `count_in_time_range`, `sum_field`, `average_field`,
`percentage_of_total`, `ratio_between_categories`, `group_by_state`,
`unknown`) — **multiple competing rules are stored side by side** when the
evidence is genuinely ambiguous (e.g. an output matching two different
entity states), never collapsed into one guess.
`infer_state_rules()` derives `StateInclusionRule`s from the states the
output's own label actually names, and `StateExclusionRule`s for the
SIBLING states of the same entity that were NOT named — a structural
inference ("a count of 'open X' implicitly excludes X's other known
states"), not a label-only one.

## Scope model

`ScopeDimensionAnalyzer.analyze()` extracts, per observation: selected tab
(`model.tabs[].selected_tab_id`), pagination state, an active filter
(a form field whose label hints at filter/search/status and carries a
value), a relative time hint (`"today"`/`"this week"`/`"this month"` in
headings/text), the current actor (`ActorScope`), and a tenant hint (from
breadcrumbs mentioning organisation/workspace/account wording).

**`scopes_compatible()`** is the gate that prevents false verification: two
scope-dimension lists are compatible only if every dimension TYPE present
in BOTH carries the SAME value — a dimension type present in only one side
is not itself a conflict (unknown scope ≠ conflicting scope), but any
shared type with a differing value (different actor, different tenant,
different tab, different filter) makes the two observations
non-comparable. The correlator refuses to compare across incompatible
scope, returning `"scope_incompatible"` rather than silently correlating
apples to oranges.

## Before/after correlation

`DependencyCorrelator.correlate()` is the **only** mechanism that can move
a dependency toward `verified`. Given a predicted direction, whether the
triggering action succeeded, and a before/after value pair with their
respective scopes:

- Incompatible scope → `"scope_incompatible"` (never compared).
- Either value unparseable, or the action failed → `"inconclusive"`.
- No predicted direction (`"unknown"`) → `"inconclusive"`.
- `"increase"`/`"add"`/`"create"`/`"activate"`/`"enable"` predicted and the
  value rose → `"supported"`; the opposite sign → `"contradicted"`; no
  change → `"unsupported"`.
- `"replace"`/`"recalculate"`/`"redistribute"`/`"move_between_categories"`
  (direction-agnostic) predicted → `"supported"` on ANY change,
  `"unsupported"` on none.

**Delayed updates**: `DependencyMemory` tracks each dependency's last
correlation result; if a PRIOR observation was `"unsupported"` (predicted
change didn't show up) and a LATER one for the same dependency is
`"supported"` with the same predicted direction, it is reclassified
`"delayed"` with `delay_iterations` set — a genuinely delayed/stale-cache
update is distinguished from "still no change," and both `"supported"` and
`"delayed"` qualify a dependency for `verified` status.

Per the task's explicit instruction, a correlation result is stored as
evidence, **never** immediately reported as a bug — that judgment belongs
to a future QA-strategy milestone.

## Cross-role reasoning

`DependencyRelationshipBuilder.build_cross_role_finding()` uses the
Workflow Registry's own actor participations: the workflow's `initiator` is
the candidate **producer**; an `approver`/`assignee`/`reviewer` is the
candidate **processor**; the session actor the output was observed under is
the **consumer**. When producer and consumer differ (or a distinct
processor exists), an ordered `VerificationRequirement` PLAN is generated —
`login_as_actor(producer)` → `create_source_record` →
[`login_as_actor(processor)` → `perform_transition`] →
`return_as_actor(consumer)` → `compare_output` — matching the task's own
illustrative multi-actor pattern exactly. **Per the task's explicit scope,
nothing here ever switches actors automatically** — these are plans a
future milestone would execute, never executed here. When no initiator is
identifiable at all, the dependency instead records an unresolved
`"required_actor:<workflow>"` prerequisite
(`DependencyDescriptor.requires_actor_switch()`).

## Registry & memory

Two anchor-keyed stores (`DependencyMemory`), because a dependency's
TARGET must itself be a stable, deduped identity before the dependency
claim can merge:

1. **Outputs**, anchored by `(normalized_label, output_type)` — the same
   KPI seen on different pages/actors/sessions merges into ONE
   `DerivedOutputDescriptor`, confidence rising modestly with repeat
   observation (capped, never unbounded).
2. **Dependencies**, anchored by `(relationship_type, source_key,
   output_anchor)` where `source_key` is `entity:<id>` /`workflow:<id>`/
   `actor:<id>`/`state:<normalized_label>` — the same claim merges
   regardless of which page/session surfaced it, with stable
   `dependency_id`s across repeat observations.

`DependencyRegistry` exposes the full query API required: `known_
dependencies()`, `unresolved_dependencies()`, `unresolved_kpi_dependencies()`,
`dependencies_by_entity/_workflow/_actor()`, `cross_role_dependencies()`,
`dependencies_requiring_actor_switch()`,
`dependencies_with_compatible_before_after_evidence()`,
`dependencies_with_contradictions()`, `high_value_verification_
requirements()`, plus `outputs_by_page/_actor()`, `known_outputs()`,
`add_gap()`/`dependency_gaps()`, `summary()`, `snapshot()`, `to_dict()`.

## Confidence model

`dependency_confidence.py` computes **five separate sub-scores**, per the
task's explicit requirement, before deriving the composite:

- **`relationship_confidence`** — evidence-kind-weighted score (repeats of
  the exact same `(source_kind, observed_text, page_url)` decay at 0.15×,
  never inflating unboundedly) + a bonus for having both a source and a
  target attached, minus a per-contradiction penalty.
- **`formula_confidence`** — the primary aggregation rule's own confidence,
  reduced by 0.08 per competing rule (ambiguity costs confidence, never
  silently resolved).
- **`scope_confidence`** — how many of the four scope-kind slots
  (temporal/actor/tenant/filter) are actually known.
- **`effect_direction_confidence`** — 0 if `"unknown"`, else a base 0.35
  reduced per contradiction.
- **`verification_confidence`** — **0 unless `status == "verified"`** —
  this is the sub-score that structurally prevents label/terminology
  matching from ever producing a high "verification" reading.

`composite = 0.45·relationship + 0.2·formula + 0.15·scope + 0.1·effect +
0.1·verification`, plus a richness bonus (aggregation rule present, scope
known, producer+consumer both known, cross-role, verification plan
present) minus a contradiction penalty, clamped to `[0, 1]`.
`status_for()` then combines: contradictions → `contradicted`; a real
supported/delayed correlation → `verified`; an unresolved actor-switch
prerequisite with no known producer → `blocked`; direct
before/after/network-aggregate evidence with both a known source and
target → `observed`; below the distinct-evidence-kind/confidence floor →
`candidate`; a known source+target with weaker evidence → `partially_
observed`; otherwise → `inferred`.

## Memory & Planner integration

`RunMemory` gains `dependency_registry: Any = field(default=None,
repr=False)` (the same optional/degrading pattern as `entity_registry`/
`actor_registry`/`workflow_registry`) and 13 pass-through query methods:
`known_outputs()`, `known_dependencies()`, `unresolved_dependencies()`,
`unresolved_kpi_dependencies()`, `dependencies_by_entity/_workflow/_actor()`,
`cross_role_dependencies()`, `dependencies_requiring_actor_switch()`,
`dependencies_with_unknown_producer/_consumer()`,
`dependencies_with_contradictions()`, `high_value_verification_
requirements()`, `dependency_gaps()`, `dependency_graph_snapshot()` — every
one degrades to `[]`/`None` with no registry. A guarded try/except block in
`memory_snapshot()` folds a compact `known`/`unresolved`/`cross_role`/
`gap_count` summary in, matching the entity/actor/workflow summary blocks
already there.

**This milestone exposes dependency intelligence to the Planner only
through these `RunMemory` query methods** — no change was made to
`FrontierBuilder`/`PriorityEngine` scoring internals, and no second
candidate-generation or ranking pipeline was introduced. This mirrors
Workflow Discovery's own deliberate scope decision exactly, for the same
reason: the task explicitly forbids a second ranking system, and wiring
dependency-value into live candidate scoring is a reviewed step for a
future milestone once this query surface has real callers.

## Controller integration

Sequence: Observe → CanonicalPageModel → Entity Registry → Actor Registry →
(action executed) → re-observe → Workflow Registry →
`_run_dependency_engine(before_model, action, result)` → Dependency
Registry → gap generation. `_run_dependency_engine` is called from the same
post-action hook as `_run_workflow_engine`, immediately after it, reusing
the identical `before_workflow_model`/`action`/`result` — and reading
`self.memory.workflow_registry` (by then already updated for this
iteration) as one of its cross-reference inputs. Non-fatal, isolated
try/except, identical discipline to every other intelligence engine hook.

## Observability

One `dependency_discovery.observation` trace event per executed action:
`url`, `state_fingerprint`, `outputs_discovered` (label/type/value/
confidence per output), `dependencies_touched` (full summary dicts —
status/confidence/relationship/effect/sources/targets/producers/
consumers/cross-role), `gaps_generated`, and running `registry_totals`
(including a `verified` count, so a report can immediately see how many
dependencies moved from candidate to actually-proven-by-observation).
Every dependency's `supporting_evidence` points at the literal observed
text/element/page that produced it.

## Dependency gaps

`DependencyGapAnalyzer.analyze()` produces: `kpi_without_source_entity`/
`alert_without_known_trigger`/`queue_without_entry_workflow` (an output
with no explaining dependency at all), `output_without_producing_workflow`
(entity known, workflow not), `report_total_without_aggregation_rule`,
`competing_dependency_explanations` (similarly-confident rival aggregation
rules), `unresolved_role_switch`, `unknown_actor_consumer`,
`ambiguous_scope` (a real relationship with no captured scope), and
`workflow_outcome_without_consumer` (cross-referencing the Workflow
Registry for outcomes with no dependency pointing at them). Per the task's
explicit scope, every gap is recorded, never turned into an executed
investigation.

## Testing

`tests/test_dependency_discovery.py` (72 tests) covers all ten required
categories: output discovery (KPI/badge/queue/chart/report/table-total/
notification, with static/date/version/identifier/stepper rejection),
entity dependencies (label match, network-resource match, shared-entity,
ambiguous candidates, no match, alias merging), workflow dependencies
(create/delete/state-transition/assignment/approval/cancellation effect
inference, unknown-output workflows produce no dependency), actor
dependencies (same actor, cross-role, processing actor between producer
and consumer, unresolved verification actor, single-role application),
aggregation (count/filtered-count/sum/average/percentage/grouped/ambiguous/
competing), scope (date/tenant/actor/tab/filter detection, incompatible-
not-compared, compatible-correlated), before/after correlation (increase/
decrease/category-movement/no-change/wrong-magnitude-still-direction-only/
action-failed/scope-incompatible/contradicted/delayed-across-two-
observations), registry/merging (duplicate KPI across pages, same metric
across actors, cross-session merging, repeated-evidence dedup, contradiction
retention, stable ids), confidence (correlated > label-only, contradiction
lowers, duplicate evidence bounded, inferred < verified, incompatible scope
never raises), and an AST-based application-neutrality contract test plus
two generic fixtures (a logistics app, a dashboard app) proving zero
hardcoded business vocabulary.

## Live verification

Run with the identical, unmodified engine against all three applications
already used by prior milestones, via
`backend/scripts/dependency_live_capture.py` (mirrors
`workflow_live_capture.py`'s harness exactly):

- **ServiceFlow** (authenticated as admin, 50 actions / 11 pages): 2
  outputs discovered (a table row count, a "Demo data reset" alert
  mentioning customers/jobs), 7 dependencies — including
  `entity:customer`/`entity:job` → alert_count (`counts`, effect
  `increase`/`move_between_categories`) and three `creates_alert`
  workflow-sourced dependencies, all `partially_observed`.
- **SauceDemo** (authenticated, 40 actions / 8 pages): **0 outputs, 0
  dependencies** — SauceDemo genuinely has no KPI/dashboard/summary
  content (it's a shopping-cart flow, not a reporting surface); this is
  the correct, honest result, not a gap (see
  [Live findings](#live-findings-bugs-found-and-fixed-by-live-testing) #2
  for why an early version of this run produced a spurious "output" here
  and why zero is now right).
- **InsightBoard** (unauthenticated, 28 actions / 5 pages): 2 outputs
  (`"Open Jobs 12"` → `queue_count`, a table row count), 3 dependencies
  including `entity:job` → queue_count (`counts`), plus 4 gaps
  (`output_without_producing_workflow`, `competing_dependency_
  explanations` ×3 — InsightBoard is a static read-only dashboard with no
  workflows to discover, so the "no producing workflow known" gap is
  exactly correct).

All three runs completed with **zero exceptions from the dependency
discovery hook**. No dependency reached `verified` in live testing — every
discovered output in these three apps was observed at most once per
distinct value (a one-time login alert, a static dashboard figure), so no
genuine before/after pair with a real triggering action existed to
correlate against. **This report does not claim a verified KPI dependency
was live-proven** — that capability is unit-tested and proven in isolation
(see `TestBeforeAfterCorrelation`/the `test_delayed_change_detected_on_
second_observation` engine-level test), but a live run that actually
authenticates, creates an entity, and observes its counter increase on the
same dashboard was not performed this milestone.

## Live findings (bugs found and fixed by live testing)

1. **Whole-page text dumps misclassified as report summaries.** A
   ServiceFlow observation had a `text_block` (mis-tagged
   `block_type="summary"` upstream) holding the ENTIRE page's nav+header+
   footer chrome concatenated (up to the 160-char label-truncation limit),
   containing "5 customers, 4 jobs" among hundreds of unrelated words —
   this produced dependencies linking FOUR different phantom/real workflow
   terms to the same garbled 160-character label. Fixed by rejecting any
   candidate summary text longer than 200 characters — a genuine business
   summary figure is a short phrase, not several paragraphs of page dump.
   Regression: `test_whole_page_text_block_rejected_as_summary`.
2. **Newline-fragmented chrome text under the length cap still slipped
   through.** A SauceDemo observation had a SHORTER (<200 char) but
   newline-fragmented `"summary"` block — `"Open Menu\nYour Cart\n
   Checkout\nTwitter\nFacebook\n..."` — stacked nav/footer labels, not
   prose. The length cap alone didn't catch it, and it produced spurious
   entity dependencies for `"twitter"`, `"facebook"`, `"linkedin"`,
   `"swag lab"`, `"checkout"`, `"continue shopping"` (all really Entity
   Discovery's own upstream candidate-term noise, amplified by this
   engine treating the block as a legitimate summary in the first place).
   Fixed by rejecting any candidate summary text containing more than one
   newline — a genuine summary is prose (one line, maybe wrapping once),
   never several stacked one-or-two-word lines. This eliminated the
   SauceDemo output entirely, correctly: SauceDemo has no real KPI/summary
   content, so zero outputs is the honest result once the chrome-dump
   false positive is gone. Regression:
   `test_newline_fragmented_chrome_text_rejected_as_summary`.
3. **Workflow-sourced effect direction was never inferred.**
   `infer_effect()` originally handled only `state_label`/`entity_id`
   sources, silently returning `DependencyEffect()` (all-unknown) for any
   `workflow_dependency` candidate (`source.workflow_id` set,
   `entity_id` None) — found during manual end-to-end smoke-testing before
   the automated live-verification runs even began, via the same
   `create`→`increase` fixture used in `TestWorkflowDependencies`. Fixed by
   adding `_effect_from_workflow_steps()`, applying the identical verb→
   direction map directly to the named workflow's own steps. Confirmed via
   the engine-level smoke test producing `effect_direction="increase"` and
   `status="verified"` for a workflow-sourced dependency, matching the
   entity-sourced case exactly.

Each fix was verified by re-running the **same, unmodified** live-capture
script against the app that surfaced it, confirming the specific noise
disappeared before moving to the next app.

## Known limitations

- **Network evidence is structurally limited.** GemmaQA's `NetworkMonitor`
  captures only failed (4xx/5xx) requests, and never response bodies or
  headers for any request (the same limitation already documented in Actor
  Discovery's known limitations, confirmed unchanged by this milestone's
  own audit). "Aggregate response fields", "response count matches visible
  count", and "API/UI mismatch" detection as described in the task are
  therefore NOT implemented beyond matching `NetworkEvidence.text` (an
  endpoint description string) against known entity/workflow terms — real
  payload-level aggregate-field correlation would need new `NetworkMonitor`
  plumbing, out of scope here.
- **No live run actually verified a KPI dependency end-to-end.** The
  before/after correlator, delay detection, and `verified` status are all
  unit-proven, but no live run in this milestone authenticated, performed a
  create/transition action, and observed a matching counter change on the
  SAME dashboard in the SAME session — the three live applications either
  don't expose a revisitable counter that changes from the actions taken
  (ServiceFlow/SauceDemo) or have no workflows to trigger at all
  (InsightBoard). This is recorded honestly, not glossed over.
- **Cross-role verification plans are unit-proven, not live-proven.**
  `build_cross_role_finding()` and its `VerificationRequirement` chains are
  covered by direct unit tests with multi-actor fixtures; no live run
  authenticated as more than one actor identity, so a real, observed,
  cross-role hand-off producing a verification plan was not captured live.
- **Upstream entity/workflow-term noise still surfaces here.** Entity
  Discovery's own known limitations (an application's brand name or a
  social-media footer link discovered as a phantom "entity") propagate into
  this engine's candidate matching when they happen to co-occur with a
  legitimate summary text — this milestone's two live-found fixes (#1, #2
  above) removed the WORST amplifier (giant/fragmented text blocks), but
  did not and should not attempt to re-open or re-fix Entity Discovery's own
  term-extraction logic, per the same scope discipline Workflow Discovery's
  documentation already established.
- **Aggregation and scope coverage is intentionally narrow.** Only the
  aggregation rule types and scope dimension types with clear, generic,
  structural evidence sources are implemented; more precise formula
  inference (e.g. distinguishing `sum_field` from `cumulative_total`) would
  require deeper before/after value-trend analysis across many
  observations, out of scope for this milestone.
- **English-only linguistics and markup-dependent**, inherited from the
  shared entity/workflow discovery primitives this engine reuses — same
  caveat already documented for those engines.

## Definition of done — status against the task's checklist

- Derived outputs discoverable generically — **done**, live-proven across
  three unrelated applications (including the correct "zero" result for an
  app with no KPI content).
- KPIs/counters/badges/charts/reports/queues representable — **done**
  (schemas + `OutputCandidateBuilder`).
- Outputs linkable to candidate entities/workflows — **done**, live-proven
  (ServiceFlow/InsightBoard entity links; workflow links unit-proven +
  live-observed for ServiceFlow).
- Producer/consumer actors representable — **done** (unit-proven;
  cross-role plan generation unit-proven, not live-proven — see
  limitations).
- Cross-role dependency requirements representable — **done**
  (`VerificationRequirement` plans); **never executed**, as required.
- Aggregation-rule candidates representable — **done**, including
  competing-rule storage under ambiguity.
- State inclusion/exclusion rules representable — **done**.
- Scope dimensions representable, with compatibility gating — **done**,
  unit-proven.
- Before/after observations correlated safely — **done**, unit-proven
  (including delayed-update detection); not live-proven end-to-end (see
  limitations — honestly not claimed).
- Contradictory evidence retained — **done** (`DependencyContradiction`,
  confidence penalty, `contradicted` status).
- Dependency gaps generated — **done**, live-proven (InsightBoard produced
  4 real gaps).
- Verification requirements generated — **done**, unit-proven.
- `DependencyRegistry` persisted in shared memory
  (`RunMemory.dependency_registry`) — **done**.
- Planner can query dependency intelligence — **done**, via `RunMemory`
  pass-through methods only (deliberate scope decision, see
  [Memory & Planner integration](#memory--planner-integration)).
- No automatic role switching — **true**.
- No unrestricted source-data creation — **true**, this engine never
  creates data; it only observes and correlates.
- No second frontier or ranking system — **true**, `FrontierBuilder`/
  `PriorityEngine` internals are untouched.
- No application-specific names required — **true**, enforced by the AST
  contract test and demonstrated by three structurally unrelated live
  applications.
- Existing perception/entity/actor/workflow engines remain functional —
  **true**, full suite green throughout (778 passed / 1 skipped / 0 failed,
  up from the 706/1/0 baseline recorded before this milestone — net +72
  dependency-discovery tests, zero regressions).
- Existing authentication and safety guarantees remain functional —
  **true**, unchanged.
- Live verification completed — **true** (see above); results reported
  honestly, including where no positive signal exists.
- Every runtime bug fixed has a regression test — **true**, 3 fixes / 3
  regressions (see [Live findings](#live-findings-bugs-found-and-fixed-by-live-testing)).
- Complete suite passes — **true** (see above).

## Next recommended milestone

The most valuable next step, based directly on what this milestone's live
verification could NOT prove: a live run that (1) authenticates, (2)
performs a create/transition action against an entity with a revisitable
dashboard counter, and (3) re-observes the SAME counter in the SAME
session — to obtain the first genuinely live-verified `status="verified"`
dependency (the mechanism is unit-proven; only the live proof is missing).
A close second: extending `NetworkMonitor` to optionally capture 2xx
response bodies (scrubbed of secrets) so aggregate-field/API-UI-agreement
evidence — currently endpoint-name-matching only — can reach its intended
strength. Per the task's own explicit boundary, KPI dependency
**validation** (turning a `verified` dependency into a pass/fail QA
assertion), autonomous actor switching, and QA test-strategy generation
remain out of scope until a future milestone.

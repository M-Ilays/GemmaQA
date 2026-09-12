# Autonomous Workflow Discovery and Reconstruction Engine

GemmaQA reconstructs the **business workflows** an application implements —
not a page sequence, but Actor → action → entity → state transition →
outcome (→ possibly another actor) — from evidence already produced by the
Perception Engine, Entity Discovery, and Actor Discovery. Application-neutral
by construction: no business/entity/actor/app name exists as a string
literal anywhere in this package's code, enforced by an AST-walking contract
test (see [Testing](#testing)).

**Status:** implemented, wired into the observation loop
(`app/agent/controller.py`), best-effort/non-fatal, live-verified against
three real applications. **Scope for this milestone is deliberately
narrow** — see [Non-goals](#non-goals-this-milestone) and
[Known limitations](#known-limitations) before treating any output as a
finished workflow model.

## Non-goals (this milestone)

Per the task that commissioned this engine, the following are explicitly
**not** implemented here: KPI dependency validation, automatic actor/role
switching, full scenario execution, QA test-strategy generation. Workflow
Discovery **represents** cross-role hand-offs and unresolved prerequisites;
it does not resolve or execute them.

## Architecture

```
app/intelligence/workflow_discovery/
    schemas.py                      15 schemas: WorkflowDescriptor, WorkflowCandidate,
                                     WorkflowStep, WorkflowActorParticipation,
                                     WorkflowEntityParticipation, WorkflowTransition,
                                     WorkflowTrigger, WorkflowOutcome, WorkflowPrerequisite,
                                     WorkflowBranch, WorkflowGap, WorkflowEvidence,
                                     WorkflowExecutionObservation, WorkflowState,
                                     WorkflowRegistrySnapshot
    workflow_confidence.py          evidence scoring, richness bonus, contradiction penalty
    state_transition_detector.py    deterministic before/after CanonicalPageModel diffing
    workflow_candidate_builder.py   CanonicalPageModel (+ before/after pair) -> raw
                                     per-observation WorkflowCandidates
    workflow_step_extractor.py      candidates -> WorkflowStep (action-verb classification,
                                     observed/unverified/blocked status assignment)
    workflow_relationship_builder.py hand-offs, prerequisites, branches
    workflow_reconstructor.py       connects one observation's steps/transitions/handoffs/
                                     prerequisites/branches into registry WorkflowDescriptors
    workflow_memory.py              merge/dedupe into persistent WorkflowDescriptors
    workflow_gap_analyzer.py        WorkflowGap generation (structural + entity + actor gaps)
    workflow_registry.py            the queryable catalog (Planner-facing)
    workflow_discovery_engine.py    orchestrator (`.observe()`) + tracing
```

Sits alongside `app/intelligence/entity_discovery/` and
`app/intelligence/actor_discovery/` (same package family, same guardrails)
and **reuses** rather than duplicates their generic primitives: the
`OPERATION_VERBS`/`HTTP_METHOD_OPERATIONS`/`NON_ENTITY_OPERATIONS` verb
lexicon (extended, not forked, by `WORKFLOW_EXTRA_VERBS` for
approve/reject/escalate/dispatch/... which entity discovery never needed),
`ASSIGNMENT_HINTS`/`APPROVAL_HINTS`/`ROLE_FIELD_HINTS` from actor discovery,
and both registries as read-only cross-reference inputs. No generic
normalisation/evidence/confidence/entity/actor/memory/graph utility is
duplicated here.

**Note on naming:** `app/schemas.py` separately defines an unrelated
`Workflow`/`WorkflowStep` pair (a narrative, single-run action-log journey
built by `RunMemory.finalize_workflow()`) and `app/agent/form_workflow.py`
defines a `GenericFormWorkflow` state machine (single-form-fill lifecycle).
Neither is reused or replaced by this package — they solve different
problems and live in different modules with no cross-import.

## Why workflow discovery cannot reuse the perception hook

Entity and Actor Discovery each classify **one** observation. Workflow
discovery fundamentally needs a **before/after `CanonicalPageModel` pair
plus the action executed between them** — a state transition, and whether a
step was actually observed vs. merely structurally present, are meaningless
without knowing what changed and what caused it.

`app/agent/controller.py`'s existing `_run_perception_engine()` runs twice
per iteration (pre-action and post-action observation), each time
overwriting `self.memory.canonical_page_model` — it cannot hold both
snapshots at once. The controller instead captures
`before_workflow_model = self.memory.canonical_page_model` immediately after
the pre-action observation (before it is overwritten), then calls a
dedicated `self._run_workflow_engine(before_model=..., action=..., result=...)`
immediately after the post-action observation, once `self.memory.
canonical_page_model` holds the "after" snapshot. Non-fatal, isolated
try/except, identical discipline to every other engine hook.

## Pipeline (`WorkflowDiscoveryEngine.observe`)

Called once per **executed action** (not once per raw observation):

1. **`StateTransitionDetector.detect(before, after)`** — deterministic diff
   (see [State transition detection](#state-transition-detection)).
2. **`_merged_candidates()`** — builds `WorkflowCandidate`s from **both**
   `before_model` and `after_model`, deduped by `(candidate_type,
   element_id)`. This is not cosmetic: the control an action actually
   targeted (e.g. a "Submit" button) routinely no longer exists on the
   after-page in the extremely common submit → redirect pattern — building
   candidates only from `after_model` silently lost every such step (see
   [Live findings](#live-findings-bugs-found-and-fixed-by-live-testing), #1).
   Newly revealed affordances on the after-page (e.g. an "Approve" button
   that only appears once status changes) are still picked up from there.
3. **`WorkflowStepExtractor.extract()`** — candidates → `WorkflowStep`s,
   status-stamped (see [Step status discipline](#step-status-discipline)).
4. **`WorkflowRelationshipBuilder`** — hand-offs, prerequisites, branches
   (see [Multi-actor reasoning](#multi-actor-workflow-reasoning),
   [Prerequisites](#prerequisite-discovery), [Branches](#branch-and-failure-path-discovery)).
5. **`WorkflowReconstructor.reconstruct()`** — groups steps by entity anchor,
   merges into (new-or-existing) registry `WorkflowDescriptor`s (see
   [Reconstruction](#workflow-reconstruction--anchoring)).
6. **`_populate_actor_known_workflows()`** — writes into
   `ActorRecord.known_workflows`, a field Actor Discovery declared but never
   populated (confirmed by audit before writing any code here).
7. **`WorkflowGapAnalyzer.analyze()`** — if an entity or actor registry is
   supplied (see [Gaps](#workflow-gaps)).
8. **Trace** — one `workflow_discovery.observation` event.

## Core schemas

All 15 required schemas are implemented in `schemas.py` with
`field_validator`-enforced controlled vocabularies (frozensets), matching
the entity/actor discovery pattern. Key ones:

- **`WorkflowDescriptor`** — the persistent, merged record: `workflow_id`,
  `canonical_name`, `aliases`, `status`, `confidence`, `supporting_evidence`,
  `source_pages`/`source_states`/`source_elements`/`source_network_evidence`,
  `actors` (`WorkflowActorParticipation`), `entities`
  (`WorkflowEntityParticipation`), `steps`, `triggers`, `prerequisites`,
  `transitions`, `branches`, `outcomes`, `known_entry_points`/
  `known_exit_points`, `known_failure_paths`/`known_success_paths`,
  `unresolved_gaps`, `first_seen`/`last_seen`, `observation_count`,
  `version`. Methods: `observed_step_count()`, `inferred_step_count()`,
  `is_cross_role()`, `requires_another_actor()`, `to_summary_dict()`.
- **`WorkflowStep`** — `step_id`, `sequence_hint`, `action_verb`,
  `semantic_action`, `actor_id`, `entity_id`, `source_entity_id`/
  `target_entity_id`, `source_state`/`target_state`, `page_id`,
  `page_state_fingerprint`, `control_id`/`form_id`/`dialog_id`,
  `endpoint_evidence`, `prerequisite_ids`, `expected_outcomes`/
  `observed_outcomes`, `confidence`, `evidence`, `status`.

**Step statuses** (`STEP_STATUSES`): `observed`, `partially_observed`,
`inferred`, `blocked`, `unverified`, `contradicted`, `completed`. **An
inferred step is never treated as observed** — enforced structurally, not
just by convention (see below).

**Workflow statuses** (`WORKFLOW_STATUSES`): `candidate`, `partial`,
`confirmed`, `contradicted`, `stale` (`stale` reserved, not yet actively
computed — mirrors Actor Discovery's own unused `stale`).

Other controlled vocabularies: `TRIGGER_TYPES` (7), `OUTCOME_TYPES` (7),
`PREREQUISITE_TYPES` (10), `BRANCH_TYPES` (8, incl. `unclassified` — never
silently discarded), `GAP_TYPES` (12), `WORKFLOW_ROLES` (6:
initiator/approver/reviewer/assignee/observer/system).

## Step status discipline

A structurally-present candidate (a button that exists on the page) always
starts `unverified`. **Only** the step whose `control_id` equals the actually
executed element's id is promoted — to `observed` if the action succeeded,
`blocked` if it failed. This is the concrete mechanism behind "do not treat
inferred steps as observed": every other step on the same page, no matter
how confidently classified, stays `unverified` until it is itself the one
actually clicked.

## Evidence sources

`WorkflowEvidence.source_kind` draws from a 30-value controlled vocabulary
covering: `CanonicalPageModel` structural elements (forms, tables, status
columns, badges, dialogs, confirmations, breadcrumbs, headings, button
labels, field labels, assignment/approval controls, status dropdowns),
before/after page and entity state comparisons, network evidence
(method/status/resource-type diffs), Entity/Actor Registry cross-references,
and application memory. Every `WorkflowEvidence` records `source_kind`,
`observed_text` (truncated to 160 chars), `page_url`, `state_fingerprint`,
and `element_id` — never an interpretation, always a pointer to what was
actually seen.

## Workflow candidate discovery

`WorkflowCandidateBuilder.build()` runs nine independent detectors per
observation: `_forms` (create-vs-edit via filled-field-ratio heuristic),
`_status_controls` (label hints + table status/assignment columns),
`_assignment_and_approval_controls` (forms + tables + buttons),
`_dialogs`, `_action_controls` (any qualifying button/link resolving to a
recognized-or-unknown-but-preserved verb — see below), `_multi_step_form`
(form + stepper/pagination), `_list_to_detail_navigation` (breadcrumbs, or
table + view control), `_actor_queue` (my-tasks/inbox-style headings/nav),
`_entity_cross_references` (table/form labels matching an **already-known**
entity term). None of these hint lists are one application's business
vocabulary — every one is generic UI/structural English ("a status column",
"a confirm dialog", "a my tasks queue") universal across web applications.

`_action_controls` is deliberately permissive by design — any visible
button/link resolving to *any* verb (recognized or not) becomes a
candidate, because the task requires supporting unknown semantic actions
rather than discarding them. Live testing showed this permissiveness needs
real, generalizable **exclusions** to avoid misclassifying non-action
content as a workflow verb — see items #2–#6 in
[Live findings](#live-findings-bugs-found-and-fixed-by-live-testing).

## Action semantics

`classify_semantic_action()` (`workflow_step_extractor.py`) checks
`OPERATION_VERBS` (reused from entity discovery), then
`WORKFLOW_EXTRA_VERBS` (accept/decline/verify/acknowledge/publish/schedule/
dispatch/transfer/escalate/resolve/pay/refund/notify/unassign/reassign/
complete/reopen/continue — verbs entity discovery never needed), then falls
through to **preserving the verb verbatim** if still unrecognized, and only
as a last resort falls back to a `candidate_type`-implied default action
(e.g. a bare status-control candidate defaults to `"update"`). An unknown
verb is never discarded.

## State transition detection

`StateTransitionDetector.detect(before, after)` is fully deterministic —
no LLM, no heurist*guessing* about meaning, only structural diffing:
URL change, fingerprint change, tab change, dialog open/close, form-field
progression, table row count/appearance/disappearance, **entity status
column changes** (`entity_status_changed`), **assignment/ownership column
changes** (`assignment_changed` — via `ASSIGNMENT_HINTS` extended locally
with "owner"/"owned by"/"ownership"), badge/counter changes, notification
appearance, success/failure alert-severity changes, enabled/disabled control
flips, and new network-resource activity. Every signal carries its own
`confidence` and `is_explicit` flag. **When no explicit state label exists
anywhere, a structural placeholder** (`"state before {verb}"` →
`"state after {verb}"`) **is used instead, always at lower confidence and
always marked non-explicit** — never presented as if it were a real
business-state name.

## Workflow reconstruction & anchoring

A workflow is identified primarily by its **primary entity**:
`f"entity:{entity_id}"`. Every step touching the same entity — create,
review, approve, status-change — belongs to **one** lifecycle workflow, per
the task's own illustrative example. This is the mechanism that prevents the
same real-world workflow being duplicated merely because it was observed on
a different page or in a different session (the entity anchor is stable
across both). Steps with no known entity fall back to a page-group anchor
(`f"page:{url_group}"`); an observation with **zero** concrete steps but a
real transition/prerequisite/branch still anchors to the known entity if one
exists, rather than being silently discarded.

Compatible steps connect via: shared entity, compatible state sequence,
shared control/form/dialog identifier, temporal/navigation ordering,
matching transitions, actor ownership. **A workflow candidate does not
require every actor or step to be known before being created** — an
incomplete workflow (unknown initiator, unresolved hand-off, missing
completion path) is still a real registry entry with explicit
`WorkflowGap`s attached, not something withheld until "complete."

## Multi-actor workflow reasoning

Hand-offs are detected from: `"assigned to"`/`"submitted by"`/`"approved
by"`/`"owned by"`/`"reviewed by"`/`"created by"` text patterns, the
co-presence of an assignment control with an approval control, and a
**permission mismatch check** against the Actor Registry (the current
session actor lacking a permission the observed control implies is needed).
A hand-off to a **named, already-known** actor resolves immediately via
participation attachment; a hand-off to an unnamed actor becomes an
unresolved `required_actor` prerequisite (`satisfied=False`) —
`WorkflowDescriptor.requires_another_actor()` checks exactly this
condition, not a step/actor-id mismatch (see
[Live findings](#live-findings-bugs-found-and-fixed-by-live-testing) in the
implementation history — every step's `actor_id` is uniformly the current
session's actor, so a naive set-difference check could never actually
trigger).

**Per the task's explicit scope, this milestone never automatically
switches actors.** Cross-role continuation is represented, not executed:
`WorkflowPrerequisite(type="required_actor", satisfied=False)`, an
unresolved step participant, and (from the gap analyzer)
`unresolved_actor_handoff` gaps are the concrete artifacts a future
milestone would act on.

## Prerequisite discovery

`WorkflowRelationshipBuilder.build_prerequisites()` currently detects:
`authentication` (from the `authenticated` flag), `required_actor` (from
unresolved hand-offs), and `required_assignment` (an approval control
co-existing with an assignment control on the same observation). Each
`WorkflowPrerequisite` carries `type`, `target`, `evidence`, `confidence`,
`satisfied`, `blocking_reason`. **Per the task's explicit scope, this
milestone does not attempt to solve/resolve prerequisites** — they are
recorded as evidence for a future planner capability, not acted on.

## Branch and failure-path discovery

`build_branches()` detects, from co-occurring controls or a table's
observed status-value set: `approve_vs_reject`, `save_vs_submit`,
`complete_vs_cancel`, `retry_vs_abandon`, `active_vs_archived`/
`assigned_vs_unassigned`. **Mutually exclusive branches are never merged
into one linear workflow** — each `WorkflowBranch` records its
`option_labels` distinctly.

## Workflow gaps

`WorkflowGapAnalyzer.analyze(registry, entity_registry, actor_registry)`
produces three families, all through `registry.add_gap()`:

- **Structural** (`_workflow_structural_gaps`): `unknown_initiating_actor`,
  `missing_creation_path`, `missing_completion_path`,
  `unresolved_actor_handoff`, `unknown_prerequisite`,
  `outcome_without_producer`, `mutation_without_observed_result`.
- **Entity-cross-referenced** (`_entity_gaps`, against
  `EntityRegistry.known_entities()`/`.relationships`):
  `status_without_changing_action` (an entity has a status field but no
  observed status-changing workflow step), `relationship_without_workflow`
  (a known entity relationship with no connecting workflow observed).
- **Actor-cross-referenced** (`_actor_gaps`, against
  `ActorRegistry.known_actors()`/`.granted_permission_ids()`/
  `.known_dashboards`): `permission_without_workflow` (an actor has a
  permission with no known workflow using it),
  `dashboard_without_source_workflow` (a KPI/summary reachable with no
  known workflow that produces its data).

Each `WorkflowGap` carries `gap_type`, `description`, `related_actor_ids`/
`related_entity_ids`/`related_step_ids`, `evidence`, `confidence`,
`severity`, `exploration_value`, `recommended_investigation_goal`. **Per the
task's explicit scope, gaps are recorded, not executed** — nothing in this
milestone turns a gap into an autonomous investigation action.

## Confidence model

`workflow_confidence.py`: `score_evidence()` weights **distinct evidence
kinds**, not raw counts (`SOURCE_KIND_WEIGHTS`, highest for
`before_after_page_state`/`before_after_entity_state` at 0.30, lowest for
`application_memory` at 0.05) — repeated evidence of the **same** kind is
deduped first by exact `(source_kind, observed_text, page_url)` tuple, then
decayed (`REPEAT_DECAY = 0.15`) for any further exact duplicates, so
**identical repeated observations never inflate confidence unboundedly**.
`workflow_richness_bonus()` adds an explicit bonus for having observed
steps, ≥2 steps, real transitions, ≥1 actor, cross-role participation,
outcomes, and entities — the same "richness, not just evidence-kind count"
principle Actor Discovery's own confidence model already established.
`contradiction_penalty()` penalizes a workflow whose transitions contain
both `(A,B)` and `(B,A)` for the same entity with no branch explaining the
reversal, or a directly `contradicted` status. `status_for()` combines
distinct-kind count, the 0.4 confirmed-confidence threshold, observed-step
presence, and `requires_another_actor()` to assign
candidate/partial/confirmed/contradicted.

## Registry & memory

`WorkflowMemory` merges by anchor (mirrors Entity/Actor Memory's
term-keyed-dict pattern): steps dedupe by `(control_id or form_id or
dialog_id, semantic_action or action_verb)`, with status only ever
*strengthening* on a repeat observation
(`inferred < unverified < blocked < partially_observed/contradicted <
observed < completed`) — never regressing, never duplicating. Actor/entity
participation, transitions, states, prerequisites, branches, triggers, and
outcomes all merge the same way. `finalize()` recomputes confidence/status,
appends the observing page to `source_pages`, and bumps
`observation_count`/`version`.

`WorkflowRegistry` exposes the full query API required: `known_workflows()`
(confirmed+partial), `incomplete_workflows()` (partial only),
`high_confidence_workflows()` (≥0.6), `low_confidence_workflows()` (<0.3),
`workflows_by_entity()`, `workflows_by_actor()`, `workflows_by_state()`,
`workflows_by_confidence()`, `cross_role_workflows()`,
`workflows_requiring_another_actor()`,
`workflows_with_unresolved_prerequisites()`,
`workflows_with_visible_outcomes_but_unknown_producers()`, plus
`add_gap()`/`workflow_gaps()`, `summary()`, `snapshot()`, `to_dict()`.

## Application Memory integration

`RunMemory` gains `workflow_registry: Any = field(default=None, repr=False)`
(same optional/degrading pattern as `entity_registry`/`actor_registry`) and
ten pass-through query methods: `known_workflows()`,
`incomplete_workflows()`, `high_confidence_workflows()`,
`low_confidence_workflows()`, `workflows_by_entity(entity_id)`,
`workflows_by_actor(actor_id)`, `cross_role_workflows()`,
`workflows_with_unresolved_prerequisites()`,
`workflows_with_visible_outcomes_but_unknown_producers()`,
`workflows_requiring_actor_switching()` (delegates to
`workflows_requiring_another_actor()`), `workflow_gaps()`,
`workflow_graph_snapshot()` — every one degrades to `[]`/`None` with no
registry attached. A guarded try/except block in `memory_snapshot()` folds
a compact `known`/`incomplete`/`cross_role`/`gap_count` summary in,
matching the entity/actor summary blocks already there.

## Planner integration (scoped down)

**This milestone exposes workflow intelligence to the Planner only through
the `RunMemory` query methods above** — mirroring exactly how entity/actor
discovery are exposed today. No change was made to
`FrontierBuilder`/`PriorityEngine` scoring internals, and no second
candidate-generation or ranking pipeline was introduced. This is a
deliberate scope decision, not an oversight: the task explicitly requires
"do not create a second candidate-generation pipeline" and "must not
introduce automatic role switching or unsafe workflow execution" — wiring
workflow-value into live candidate scoring is exactly the kind of change
that risks both, and is better done as its own reviewed step in a future
milestone once the query surface above has been exercised by real callers.

## Controller integration

Sequence, `app/agent/controller.py`: Observe → CanonicalPageModel → Entity
Registry → Actor Registry → (action executed) → re-observe →
`_run_workflow_engine(before_model, action, result)` → Workflow Registry →
gap generation. The entity/actor summaries produced during perception are
now captured (`self._last_entity_summary`/`self._last_actor_summary`,
previously discarded) so the workflow hook can read `current_actor_term`/
`primary_entity_term` from them without recomputing anything. Non-fatal,
isolated try/except, identical to every other engine hook.

## Observability

One `workflow_discovery.observation` trace event per executed action
(`app.utils.exploration_trace`): `url`, `state_fingerprint`,
`candidates_discovered`, `steps_added` (id/action/status/confidence per
step), `transitions_detected`, `handoffs_detected`, `branches_found`,
`prerequisites_attached`, `touched_workflows` (full summary dicts),
`gaps_generated`, and running `registry_totals`. Every step's `evidence`
list and every transition's `WorkflowEvidence` point at the literal observed
text/element/page that produced it — the trace answers why-workflow,
why-steps-connected, which-transition-was-observed-vs-structural, and
which-parts-remain-inferred without needing a separate audit pass.

## Testing

`tests/test_workflow_discovery.py` (50 tests) covers all nine required
categories: basic workflows (create/edit/archive/status-transition/
multi-step-form/list→detail→action), actor-aware workflows (single-actor,
cross-role via permission mismatch, assignment changes, unresolved
hand-off), entity relationships (single entity, ownership transfer, shared
entity across modules), state transitions (explicit, structural-only,
failed→blocked-not-observed, idempotent, repeated-merges), branches
(approve/reject, save/submit, complete/cancel), prerequisites
(authentication, wrong-actor, wrong-state-via-assignment), merging
(multi-page, cross-session), confidence (observed > inferred, repeated
duplicates don't inflate unboundedly, contradictory evidence reduces
confidence), and an AST-based application-neutrality contract test proving
no hardcoded business/entity/app/actor name literal exists in this
package's code — plus one live Playwright integration test against a routed
ticket-status-transition fixture. **6 of the 50 tests are direct
regressions for bugs live testing found** (see below); every one encodes
the exact structural shape that broke, not merely a rerun of the original
fixture.

## Live verification

Run with the identical, unmodified engine against all three applications
already used by prior milestones, via
`backend/scripts/workflow_live_capture.py` (mirrors
`live_acceptance_capture.py`'s harness: real Playwright browser,
`MockGemmaProvider` for the LLM only, no app-specific config):

- **ServiceFlow** (`http://127.0.0.1:5500`, authenticated as admin,
  50 actions / 11 pages): 4 workflow anchors reached (`job`, `customer`,
  `active job`, `serviceflow`), all `partial` status, real transitions
  (assignment changes, status changes), real prerequisites
  (`required_actor`, `authentication`).
- **SauceDemo** (`https://www.saucedemo.com/`, authenticated,
  40 actions / 8 pages): 2 workflow anchors (`cart`, `inventory`), both
  reached **`confirmed`** status (0.86 and 1.0 confidence), with real
  observed steps (add-to-cart, checkout, continue) and real transitions.
- **InsightBoard** (local static dashboard fixture, unauthenticated,
  28 actions / 5 pages): 5 workflow anchors, ranging `candidate` (0.0,
  a phantom brand-name entity — see limitations) through **`confirmed`**
  (1.0, a `report` entity workflow).

All three runs completed with **zero exceptions from the workflow
discovery hook** (non-fatal wiring proven, not just claimed) and produced
real, differentiated confidence per app — not a flat default.

## Live findings (bugs found and fixed by live testing)

1. **Candidates only built from the after-action page.** The very first
   full live run against ServiceFlow produced almost no steps at all,
   because the executed control (e.g. a "Submit" button) routinely no
   longer exists on the after-page in the common submit → redirect
   pattern, and candidates were built only from `after_model`. Fixed by
   `_merged_candidates()` (both models, deduped). This was the single
   highest-impact fix — 20 of 34 initial unit-test failures traced back to
   this exact gap in reasoning before live testing even began.
2. **Primary top-navigation links treated as entity workflow steps.**
   ServiceFlow's `<nav aria-label="Primary">` Dashboard/Customers/Jobs/
   Settings links were captured as `progression_button` candidates and
   attributed to whichever entity happened to be the current page's
   subject — appearing, duplicated, in **every** discovered entity's
   workflow (the same 4 links exist on every page). Fixed by cross-
   referencing `model.navigation_regions` item element-ids and excluding
   them from `_action_controls`. Regression:
   `test_primary_navigation_links_are_not_treated_as_entity_workflow_steps`.
3. **Form field labels treated as action verbs.** A live SauceDemo/
   ServiceFlow checkout/edit form turned `<input>`/`<select>` accessible
   names ("First Name", "Email*", "Timezone") into bogus "first"/"email*"/
   "timezone" steps. Fixed by excluding `tag in {"input", "select",
   "textarea", "label"}` (the `<label>` exclusion was a second pass, added
   after a follow-up SauceDemo run showed a checkout `<label>` leaking the
   same way) and `role in {"textbox", "combobox", "listbox"}` from
   `_action_controls`. Regressions:
   `test_form_field_labels_are_not_treated_as_action_controls`,
   `test_form_label_element_is_not_treated_as_an_action_control`.
4. **A plain navigation link's own display text treated as a verb.**
   ServiceFlow's dashboard "recent customers" widget renders each customer
   as a bare `<a>` link whose text is that customer's name ("Jordan Lee") —
   the "preserve unknown verbs" design turned that proper noun into a bogus
   "jordan" step. Fixed by excluding `tag == "a"` unless it explicitly
   carries `role == "button"` (a real action styled as a link is kept; a
   plain resource-navigation link is not). Regression:
   `test_plain_navigation_link_label_is_not_treated_as_a_verb`.
5. **Tab-switch controls treated as entity workflow steps.** A live
   InsightBoard run had "Overview"/"Performance"/"Activity"/"Team" tab
   controls captured the same way as #2 — a tab switch is a view
   selection, not an action on the current entity. Fixed by cross-
   referencing `model.tabs` (`TabGroup.tabs[].element_id`) the same way as
   navigation regions. Regression:
   `test_tab_switch_control_is_not_treated_as_an_entity_workflow_step`.
6. **KPI/stat figures treated as action verbs.** The same InsightBoard run
   had a clickable "128 Active Customers" stat card turn into a bogus "128"
   step. Fixed by rejecting any candidate whose first word starts with a
   digit — a leading digit is never a verb, in any application. Regression:
   `test_kpi_figure_label_is_not_treated_as_an_action_verb`.

Each fix was verified by re-running the **same, unmodified** live-capture
script against the app that surfaced it, confirming the specific noise
disappeared before moving to the next app.

## Known limitations

- **Residual generic-chrome noise remains.** Page-global controls like
  "Reset demo data", "Log out", and a per-row "Actions" menu-trigger button
  still appear as low-confidence `unverified` steps attributed to whichever
  entity is current. A word-level blacklist was tried and reverted: words
  like "reset"/"submit"/"cancel"/"save" are simultaneously legitimate
  workflow-progression verbs in `OPERATION_VERBS`/`WORKFLOW_EXTRA_VERBS` in
  other applications, and blacklisting them broke real test cases (5 of the
  50 tests failed when this was tried). The three exclusions that survived
  (nav-region membership, tab membership, tag/role structural checks) are
  all **structural**, not word-based, which is why they hold up across all
  three unrelated applications without any tuning per app.
- **A native-`<select>`-with-concatenated-option-text edge case.** SauceDemo's
  product-sort control still occasionally surfaces a "name" step (from
  "Name (A to Z) Name (Z to A) Price ..." — the joined text of all
  `<option>` children) despite the `tag == "select"` exclusion, suggesting
  the underlying accessible-name extraction in `PerceptionEngine` sometimes
  attributes a container's rolled-up text to a different element than the
  `<select>` itself. Not chased further — it is low severity (stays
  `unverified`, does not duplicate across entities) and fixing it would mean
  changing `PerceptionEngine`'s DOM extraction, out of scope here.
- **Upstream entity-term granularity is not this engine's problem to fix,
  but does show up in its output.** Two live findings trace to Entity
  Discovery, not Workflow Discovery: (a) ServiceFlow's own brand name
  ("ServiceFlow") and InsightBoard's own app name ("InsightBoard") were
  discovered as entities and therefore anchor phantom, near-empty
  `candidate`-status workflows; (b) "job" and "active job" were discovered
  as two distinct entity terms (likely from an "Active Jobs" section
  heading) rather than merged as aliases, producing two workflows that
  share almost all their (noisy) residual steps. Workflow Discovery
  correctly built one workflow per **entity_id it was given** — the
  duplication is upstream entity-term granularity, and is recorded here as
  a cross-engine gap for a future Entity Discovery pass, not fixed by
  reopening that already-completed, already-tested engine mid-milestone.
- **Prerequisite/branch coverage is intentionally narrow.** Only
  `authentication`, `required_actor`, and `required_assignment`
  prerequisites are actually detected; the other 7 `PREREQUISITE_TYPES`
  values exist in the schema for forward compatibility but nothing produces
  them yet. Branch detection is likewise limited to co-occurring-control
  and status-value-set heuristics — genuinely rare multi-branch flows
  (e.g. a 3-way approve/reject/escalate) collapse to whichever pairwise
  pattern matches first.
- **No automatic actor switching, prerequisite resolution, or gap
  execution** — explicitly out of scope per the task. `required_actor`
  prerequisites, `unresolved_actor_handoff` gaps, and every
  `recommended_investigation_goal` are representations for a future
  milestone to act on, not autonomous behavior in this one.
- **Cross-role workflow execution was never actually performed.** No live
  run in this milestone authenticated as more than one actor identity in
  the same session — hand-off *representation* (prerequisites, gaps,
  unresolved participants) is live-proven; hand-off *traversal* (Actor A
  creates something, Actor B is later observed acting on it, in one
  continuous run) is not, and this document does not claim it.
- **English-only linguistics and markup-dependent**, inherited from the
  shared entity/actor discovery primitives this engine reuses — same
  caveat already documented for both of those engines.

## Definition of done — status against the task's checklist

- Engine implemented, registry persisted in shared memory (`RunMemory.
  workflow_registry`) — **done**.
- Actors/entities attachable to workflows — **done**, live-proven (see
  live verification).
- Page/entity state transitions detected — **done**, live-proven.
- Multi-page workflow reconstruction — **done** (entity-anchored merging
  across `source_pages`), live-proven (ServiceFlow: workflows merged across
  11 pages).
- Cross-role hand-offs representable — **done** (prerequisites + gaps);
  **not** live-proven as an executed traversal (see limitations).
- Prerequisites/branches representable — **done**, for the subset of types
  actually detected (see limitations for the rest).
- Incomplete workflows produce explicit gaps — **done**, unit-tested;
  gap generation ran without error on all three live apps (gap *content*
  not manually re-verified against each app's ground truth beyond what is
  reported in [Live verification](#live-verification)).
- Planner can query workflow state — **done** via `RunMemory` pass-through
  methods; **not** wired into `FrontierBuilder`/`PriorityEngine` scoring
  (deliberate scope decision, see [Planner integration](#planner-integration-scoped-down)).
- No automatic actor switching — **true**, nothing in this package or its
  controller hook switches sessions.
- No app-specific names required — **true**, enforced by the AST contract
  test and demonstrated by three structurally unrelated live applications.
- No second frontier/ranking system — **true**, `FrontierBuilder`/
  `PriorityEngine` internals are untouched.
- Existing entity/actor discovery, perception, auth, and safety validator
  remain functional — **true**, full suite green throughout (706 passed /
  1 skipped / 0 failed, up from the 656/1/0 baseline recorded before this
  milestone — net +50 workflow-discovery tests, zero regressions).
- Every runtime bug found by live testing has a regression test — **true**,
  6 fixes / 6 regressions (see [Live findings](#live-findings-bugs-found-and-fixed-by-live-testing)).
- Complete suite passes — **true** (see above).

**Do not treat this as full workflow understanding of any application.**
What is proven: entity-anchored workflow reconstruction, deterministic
state-transition detection, evidence-based confidence, structural
noise-exclusion robust across three unrelated apps, and non-fatal
end-to-end wiring. What is not proven: cross-role traversal, prerequisite
resolution, branch completeness beyond the patterns implemented, or
freedom from all upstream entity-naming noise.

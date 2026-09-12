# Evidence-Driven Exploration — Live Acceptance Report

Live acceptance testing of the evidence-driven perception + priority
architecture ([Perception Engine](./PERCEPTION_ENGINE.md) →
[Canonical Page Model](./CANONICAL_PAGE_MODEL.md) →
[Visual Observation Policy](./VISUAL_OBSERVATION_POLICY.md) → FrontierBuilder
integration → PriorityEngine), against three structurally different
applications, with full per-iteration trace capture.

**Method.** `backend/scripts/live_acceptance_capture.py` runs a complete,
unmocked `AgentController` (real Chromium via Playwright, real perception,
real frontier/priority engine; the LLM is the deterministic `MockGemmaProvider`
— the same provider production uses when no live Gemma model is configured)
and records every structured trace event the system already emits
(`perception.observation`, `perception.visual_decision`, `iteration.plan`,
`iteration.stop_policy`) plus the final CanonicalPageModel, coverage record,
and stop reason. Raw captures: `evidence/_captures/final_saucedemo.json`,
`final_serviceflow.json`, `final_dashboard.json` (plus the pre-fix captures
`saucedemo.json`, `serviceflow.json`, `dashboard.json`/`dashboard2.json` that
the bug findings below cite).

**Targets:**

| App | Structure exercised | Result |
|---|---|---|
| SauceDemo (`https://www.saucedemo.com/`) | login form, product grid, non-semantic controls, footer social links, menu drawer | 8 pages, 19 actions, authenticated, stop=`action_budget_exhausted` |
| ServiceFlow (`http://127.0.0.1:5500`, local demo) | header nav SPA, dashboard, customers table + filters, jobs, settings form, login with real credentials | 8 pages, 14 actions, authenticated, stop=`action_budget_exhausted` |
| InsightBoard (`tests/fixtures/dashboard_demo/`, built for this test) | header-only nav, ARIA tablist, clickable cards, table with icon-only row actions, canvas chart, informational image, icon-only SVG header buttons, footer legal+social links | 5 pages, 24 actions, stop=`action_budget_exhausted` |

---

## 1. Implemented and runtime-proven

Each claim below is backed by specific trace evidence from the final captures.

**Perception (CanonicalPageModel) on every observation.** All three runs
emitted a `perception.observation` per observe cycle with real region,
element-count, and fingerprint data. Representative initial models:

- SauceDemo login: `regions=[form_region]`, 3 interactive elements, 1 form —
  correct for a page that is nothing but a login form.
- ServiceFlow customers: `regions=[header, primary_navigation, main_content,
  content_navigation, data_table]`, 18 elements, 1 table.
- InsightBoard index: `regions=[header, primary_navigation, main_content,
  card_group]`, 14 elements, 3 tabs, 1 image (classified `informational`),
  1 canvas.

**Header/sidebar/footer/main/nav/tab/form/table/image detection.** Live
regions observed across the runs include `header`, `primary_navigation`,
`content_navigation`, `main_content`, `form_region`, `data_table`,
`card_group`, `legal_region`, `utility_region`. Tabs (3), tables (with
row-action links), forms, and images were all detected and counted on the
pages that actually have them.

**Primary navigation outranks footer social links.** InsightBoard team page,
one iteration's actual scores: header nav items **85.1** ≫ footer legal links
**45.3** ≫ footer social links (Twitter/LinkedIn) **10.3**, with
`social_link_penalty` and `external_domain_penalty` both visible in the
losing candidates' negative-factor breakdowns. SauceDemo's footer social
links scored 10.3 vs 65–88 for in-app candidates on every iteration.

**Active workflows continue without interruption.** SauceDemo iteration 1:
`authenticate_with_credentials` selected at score **157.6**, top factor
`active_workflow_continuity=100.0`, and the login workflow ran fill→fill→submit
to completion (authenticated=True) before any exploration candidate was
touched. ServiceFlow authenticated the same way with real supplied
credentials. (Note: auth continuation across steps is `AuthenticationStrategy`'s
early-return path; the priority engine's `active_workflow_continuity` factor
demonstrably dominates the *initial* selection, and covers generic
form-workflow continuation by the same mechanism.)

**Tabs and subregions in the active module are explored.** InsightBoard:
all three tabs selected exactly once each (`select_tab`/`open_tab` at
iterations 9, 10, 12), then the run moved on to cards (`inspect_card`) and
table row actions (`open_table_row` × 5 distinct icon-only row buttons) —
no repeats after the signature fix (bug #1 below).

**Unknown icon-only controls preserved and safely investigated.**
InsightBoard's icon-only SVG header buttons were flagged by the visual policy
(`icon_only_control`, `unclassified_svg_control`, `missing_accessible_name`)
on every index observation, and the icon-only table row actions were
dispatched as safe clicks. The `inspect_unknown_component` (hover-only)
candidate path is unit-proven; in live runs the raw pass's multi-signal
detection claimed every element first (unknown_element_count=0 everywhere —
the classifier being thorough, so the fallback bucket stayed empty).

**Same URL, different state ⇒ different fingerprint.** SauceDemo product
pages produced two distinct fingerprints each for the same URL (e.g.
`inventory-item.html?id=0` → `13dabe76` and `03ce99be`: menu-drawer closed
vs. open — the drawer adds a visible `primary_navigation` region and 5
elements). Verified on 5 separate URLs in one run.

**External links verified without uncontrolled exploration.**
`verify_external_link` candidates dispatch as HOVER, never navigation
(`Planner._candidate_to_action`), and post-navigation scope enforcement
(`validate_navigation_result`) remains active. No run ever left its
authorized origin: every visited URL in all three captures is same-origin.

**Screenshots only when structural evidence is insufficient.** ServiceFlow —
a fully-labeled, semantically clean app — produced **0** visual-analysis
activations across 28 decisions (all `should_run=False`). SauceDemo and
InsightBoard, which genuinely contain icon-only/canvas/unlabeled elements,
activated with named reasons every time. The policy is demonstrably
evidence-gated, not screenshot-by-default.

**One action per iteration, then re-observe.** Every capture shows the
strict alternating pattern (two `perception.observation` events per
`iteration.plan` — one pre-plan, one post-execute) and
`tests/test_priority_engine.py` pins the executor's one-adapter-action-per-
`execute()` invariant.

**No application-specific labels.** All three apps — including the
never-seen-before InsightBoard — were explored by the same generic
region/candidate vocabulary. Contract tests
(`test_engine_never_produces_business_specific_region_names`, the
no-business-fields schema test) enforce this statically.

**No second candidate-ranking system.** `Planner._rank_elements` still
delegates to `FrontierBuilder._build_navigation_candidates` (pinned by
`test_frontier_builder_is_the_only_candidate_source_no_duplicate_ranking`);
`plan_by_priority` routes through the same `_select_auth_candidate` →
PriorityEngine path. The new score names (`business_relevance`, `novelty`,
`coverage_value`) are synced aliases of the pre-existing fields, not a
parallel score set.

**Auth + generic form workflows still work / no safety regression.** Both
auth paths (config-supplied credentials on ServiceFlow, vault-profile flow on
SauceDemo) completed live. `ActionValidator`/`SafetyPolicy` were untouched;
destructive/financial candidates are additionally hard-rejected by the
priority engine *before* dispatch (`safety_rejected=True` in the score
record), and the full 564-test suite — including all pre-existing safety
tests — passes.

**Explainable decision trace.** Every `iteration.plan` event now carries
`candidate_scores` (full per-factor breakdown with name/weight/contribution),
`active_goal`, `rejected_candidates` (with stale/safety reasons),
`selected_candidate_id`, `selection_reason` (derived from the winner's own
top factor), and `gemma_advisory_applied`.

---

## 2. Bugs found by live testing (fixed + regression-tested)

Live testing found three real, generalizable bugs that no hand-built fixture
had caught — each fixed at the root and pinned with a regression test:

1. **Tab selection got stuck in an infinite loop** (InsightBoard, pre-fix
   capture `dashboard.json`: the same `select_tab` candidate selected 16×
   in a row). Root cause: candidate attempt-tracking signatures hardcoded
   `action_type="click"`, but `select_tab` dispatches as `open_tab` (and
   `verify_external_link` as `hover`) — the recorded signature could never
   match the lookup, so `novelty` never decayed. Fix: compute the signature
   from `CANDIDATE_ACTION_HINT[candidate_type]`. Regression:
   `test_select_tab_attempt_tracking_uses_its_real_dispatched_action_type`.
   Post-fix capture (`final_dashboard.json`) shows each tab selected exactly
   once.
2. **External links inside a `<nav>` region earned a navigation-centrality
   bonus** (SauceDemo menu drawer's "About" → saucelabs.com scored 65.1,
   nearly equal to internal candidates, because +0.9 centrality nearly
   canceled the external penalty). Fix: `navigation_centrality` is 0 for any
   `verify_external_link` regardless of DOM region. Regression:
   `test_external_link_inside_a_nav_region_gets_no_navigation_centrality_bonus`.
3. **`possible_visual_defect` false-positives on normal UI patterns**
   (SauceDemo, every run: a styled `<select>` overlapping its own rendered
   value, and duplicate same-label product links sharing one box). Fix:
   benign-overlap filter (`_is_benign_overlap`) for native selects and
   identical-label pairs. Regressions:
   `test_overlap_with_identical_label_is_not_a_defect`,
   `test_overlapping_native_select_is_not_a_defect`. Post-fix, the trigger
   still fires on SauceDemo's genuinely duplicated sort-wrapper spans
   (differing labels — a real DOM peculiarity worth a look) and on nothing
   else.

Earlier phases of this same architecture were also live-debugged (relative
URLs misclassified as external; PageObserver/dom_extractor ID collisions;
`<button role="tab">` never classified as a tab; the fingerprint-scheme
mismatch that would have silently disabled the canonical-model path in
production) — all documented in [PERCEPTION_ENGINE.md](./PERCEPTION_ENGINE.md)
and the code comments at each fix site, each with its own regression test.

**Final suite: 564 passed, 1 skipped, 0 failed.**

---

## 3. Implemented but not live-proven

These paths are unit/integration-tested but no live run exercised them
end-to-end, so no runtime claim is made:

- **Visual model output merging.** The visual *policy* activated live
  (SauceDemo: 33 activations; InsightBoard: 44), and crops/prompt/parse/merge
  are unit-tested with scripted responses — but `MockGemmaProvider` returns
  empty observations, so no live run produced a populated
  `visual_evidence` list from a real multimodal model.
- **`inspect_unknown_component` dispatch in a live run.** Unit-proven
  (hover-only, preserved, safe); live pages' elements were all claimed by
  the multi-signal detector first, so the unknown bucket stayed empty.
- **`close_dialog` / `open_dropdown` / `open_context_menu` /
  `expand_accordion` / `expand_navigation_region` live dispatch.** All
  classification+dispatch paths are unit-tested; the three targets happened
  not to present an open modal/haspopup trigger at selection time within
  budget. (SauceDemo's burger menu is a plain button with no
  `aria-expanded`/`aria-haspopup`, so it is legitimately a generic
  `navigation_control`.)
- **Gemma advisory tie-break with a real LLM.** The near-tie detection and
  advisory reorder ran under the mock (deterministically no-op by design);
  no live run used a real Gemma model.
- **Prerequisite-resolution scoring** (`prerequisite_resolution_value`):
  exercised in unit tests; no live run had a prerequisite-gated goal.

## 4. Known limitations

- **Fingerprint sensitivity produces near-duplicate page states.** Each
  SauceDemo product page yields 2 fingerprints (drawer open/closed) — correct
  behavior, but each state currently regenerates near-identical candidates;
  cross-state dedup of equivalent candidates would use the budget better.
- **Goal churn.** `investigate_candidate_url` goals complete/rotate quickly,
  so the "active goal" shown in traces is often a just-promoted goal rather
  than a long-lived intent.
- **`current_module_completion_value` is coarse** (same-module = 1.0,
  other = 0.3, unknown = 0.0); it does not yet use per-module coverage
  percentages from the coverage model.
- **One tab-group simplification** (documented in PERCEPTION_ENGINE.md):
  multiple independent tablists on one page collapse into one `TabGroup`.
- **`RawObservation.lists` is captured but not surfaced** as a
  CanonicalPageModel collection (no `ListDescriptor` yet).
- **Region classification of SauceDemo's initial (drawer-closed) product
  page misses `primary_navigation`** because the drawer's `<nav>` is hidden
  until opened — structurally correct (it *isn't* visible), but it means
  centrality bonuses only apply after the drawer is first opened.
- The **third app is a purpose-built fixture** (needed to guarantee
  tabs/cards/row-action/canvas coverage in one page); it is served over real
  HTTP and explored with zero fixture-specific code, but it is not an
  independently-developed production app.

## 5. Failed acceptance criteria

None outright. Two criteria pass with the caveats already stated above:

- *"Unknown icon-only controls are preserved and safely investigated"* — the
  preservation pipeline is proven and icon-only controls were investigated
  live, but specifically via the `inspect_unknown_component` candidate type
  only in unit tests (live pages never left an element unclaimed).
- *"Screenshots are only used when structural evidence is insufficient"* —
  proven at the policy level (0 activations on the clean app). Full-page
  screenshots are still captured every iteration for *evidence archiving*
  (pre-existing behavior, unrelated to perception); the criterion was
  interpreted as governing visual *analysis*, which is what the policy gates.

## 6. Recommended next phase

1. **Run with a real multimodal Gemma model** to live-prove the visual
   evidence merge path and the advisory tie-break (the two biggest
   not-live-proven gaps).
2. **Cross-state candidate dedup**: treat candidates with identical
   (element identity, semantic operation) across near-identical states of
   the same URL as one, so drawer-open/closed variants don't double-generate.
3. **Feed per-module coverage percentages into
   `current_module_completion_value`** instead of the coarse three-tier value.
4. **Surface `RawObservation.lists`** as a first-class CanonicalPageModel
   collection.
5. **Per-tablist TabGroups** (split by DOM tablist container).
6. **Candidate-level state dedup for `verify_internal_link` on product
   grids**: N same-shaped detail links currently each cost one action;
   sampling 2–3 then generalizing would free budget for unexplored areas.

---

*This report deliberately does not claim universal page understanding. What
is claimed — and evidenced — is: deterministic structural perception that
held up unmodified across three differently-structured applications, an
evidence-first priority engine whose every decision is traceable to named
factors, and a visual layer that activates only on structural-evidence gaps.
All limits of that evidence are listed in sections 3–5.*

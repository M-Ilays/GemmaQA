# CRUD Surface Discovery

Application-neutral perception and hypothesis-generation for record-
management interfaces (list/grid pages, Add/Edit/Delete controls, create/
edit/search forms) — the direct answer to
[docs/debug/ORANGEHRM_CRUD_EXECUTION_GAP.md](debug/ORANGEHRM_CRUD_EXECUTION_GAP.md)'s
perception-layer findings (zero table detection on div-based grids, list
pages mislabelled `create_form`, no CRUD workflow inference).

**Scope discipline, stated up front:** every module described here is a
*discovery/classification* layer. Nothing in this document makes GemmaQA
execute a CRUD action it wasn't already capable of — `enable_autonomous_
investigation` and `allow_safe_test_data_creation` still gate execution
exactly as before (see the linked audit). What changes is *what GemmaQA
understands* about a page: whether it's looking at a record grid, what a
form is for, what an icon-only button does, and what a plausible create/
edit/delete workflow around them looks like — application-neutral,
structural evidence only, never a hardcoded selector or business word for
any specific target application.

---

## 1. Universal record-collection detection

**Module:** `app/perception/collection_extractor.py` (Python) +
`app/perception/dom_extractor.py`'s `RAW_EXTRACTION_SCRIPT` (the in-page JS
that produces the raw signals it consumes).

**Model:** `RecordCollection` (`app/perception/models.py`) — collection_id
(`stable_id`), `collection_type`, `columns`, `row_count`, `visible_rows`
(with a best-effort `identity_hint` per row), `row_actions`/`global_actions`,
pagination/search/filter-control flags, `structural_signals`, `confidence`,
`evidence`. Exposed on `CanonicalPageModel.collections` — a superset view
over the older, native-`<table>`-only `CanonicalPageModel.tables`, kept
unchanged for backward compatibility.

**Detected collection types**, purely from DOM structure — never a
framework name (React/Vue/Angular all produce the same observable shapes
below):

| `collection_type` | Detected via |
|---|---|
| `native_table` | `<table>` tag (unchanged from the pre-existing table extractor) |
| `aria_grid` | `[role="grid"]` container with `[role="row"]` children |
| `aria_treegrid` | `[role="treegrid"]` container with `[role="row"]` children |
| `div_row_group` | A container whose children share a class signature (≥3 repeated), stacked vertically |
| `card_group` | Same repeated-sibling signature, but wrapped/grid-laid-out with a substantial per-card footprint and at least one interactive descendant |

Per-row and global actions are linked structurally: a row's `row_action_ids`
come from `<button>/<a>/[role="button"]` elements found inside that row's
DOM subtree (mirroring the existing native-table row-action linkage); a
global action (an "Add"/"Create" toolbar button) is attached by **position**
— found above the collection's bounding box, within its horizontal span —
never by button text alone, so it stays framework/label-neutral. Search and
filter controls are attached the same way: an `<input>`/`<select>` above the
collection whose accessible name/placeholder matches generic search/filter
vocabulary.

---

## 2. Form intent classification

**Module:** `app/perception/form_intent_classifier.py`.
**Model:** `FormIntentClassification` — `intent` (one of `authentication`,
`search`, `filter`, `create`, `edit`, `delete_confirmation`, `bulk_action`,
`upload`, `settings`, `unknown`), `confidence`, `target_entity_hypothesis`,
`operation_hypothesis`, `evidence`, and `alternatives` (rejected-but-
plausible intents — **never silently dropped**).

A form is never classified `create` merely because it contains input
elements. Evidence considered, in priority order:

1. **Field-type composition** — a password field (+few total fields) is
   strong `authentication` evidence; a file field is `upload` evidence;
   a checkbox-majority is `bulk_action` evidence.
2. **Dialog context** — a near-empty form inside a dialog, paired with a
   delete/confirm submit verb or confirmation wording in the dialog text, is
   `delete_confirmation` evidence.
3. **Field-fill ratio** — mostly-filled fields are edit-shaped; mostly-empty
   fields are create-shaped (the fallback when nothing more specific fires).
4. **Label/placeholder/submit-verb vocabulary** — small field counts with
   search/filter-shaped wording (in the field's label, accessible name, *or*
   placeholder — all three are checked) or a resolved search/filter submit
   verb.
5. **URL/heading vocabulary** — generic terms like "settings"/
   "preferences"/"configuration" outrank the generic fill-ratio fallback.
6. **Collection co-location** — a small form sitting on the same page as a
   record collection gets a *weak* `search` alternative even with no label
   match, since that's the single most common real-world shape.

---

## 3. Action semantics

**Module:** `app/perception/action_semantics.py`.
**Model:** `ActionSemantics` — `semantic_action` (one of `add`, `create`,
`new`, `save`, `submit`, `update`, `edit`, `delete`, `remove`, `confirm`,
`cancel`, `search`, `filter`, `reset`, `view`, `open`, `next`, `previous`,
`more_actions`, `unknown`), `signals` (which evidence fired), `is_icon_only`,
`is_kebab_menu`, `is_floating_action_button`.

Signal priority: visible text → `aria-label` → `title` attribute →
icon-only + `aria-haspopup` (kebab/contextual menu, **regardless of whether
the trigger has a generic label like "Actions"** — live-verification against
a real target found a text-labelled `<button aria-haspopup="menu">Actions
</button>` row-actions trigger that an icon-only-only check would have
missed; see §7) → floating-action-button flag (icon-only + fixed/absolute
position + roughly-square footprint). An icon-only control with none of
these signals resolves honestly to `unknown` — never a guessed verb.

---

## 4. Field semantics

**Module:** `app/perception/form_extractor.py` (`_infer_field_kind`), new
`FormFieldDescriptor.field_kind` (`app/schemas.py`).

`field_kind` ∈ `{combobox, autocomplete, multi_select, date_picker,
radio_group, checkbox_group, file_upload, text, unknown}`, inferred from
`field_type`/`role`/`multiple`/`list` attribute/`aria-autocomplete` —
structural signals only:

- `file_upload` — `type="file"`
- `date_picker` — `type` in `{date, month, week, time, datetime-local}`
- `radio_group` — `type="radio"`
- `checkbox_group` — `type="checkbox"` **and** at least one sibling field
  shares the same `name` (a lone checkbox is not a "group")
- `multi_select` — `<select multiple>` or `role="listbox"`
- `autocomplete` — a `list` attribute (datalist) or `aria-autocomplete`
- `combobox` — `role="combobox"` with neither of the above
- `text` — the plain fallback for recognized text-like input types
- `unknown` — anything else, rather than a guess

Labels still use the existing fallback chain (`aria-label` → bound
`<label>` → `title` → `placeholder` → visible text → child `img[alt]` →
`name` attribute); when every one of those is genuinely absent, the label is
left empty, never replaced with a synthesized placeholder string.

---

## 5. CRUD workflow inference

**Package:** `app/intelligence/crud_discovery/` (schemas, candidate
builder, registry, engine) — same architectural pattern as `workflow_
discovery`/`dependency_discovery`: a `CRUDDiscoveryEngine.observe()` call
once per executed action, fed a before/after `CanonicalPageModel` pair from
the controller's post-action hook. **Discovery only — it never executes a
browser action**, exactly like every sibling intelligence engine.

**Model:** `CRUDWorkflowHypothesis` — `operation` (`create`/`edit`/
`delete`), `entity_hypothesis`, `actor_hypothesis`, `source_state`/
`target_state`, `required_controls`, `required_form_id`,
`expected_transition`, `verification_surface`, `cleanup_possibility`,
`confidence`, `evidence`, `safety_classification`
(`read_only`/`controlled_write`/`destructive`/`unknown`), and `status`.

**Two-phase confidence discipline** (`status`):

1. `entry_point` — a single observation: a collection has a control
   labelled add/edit/delete. Always low confidence (0.2–0.35). A control
   existing is not proof an operation works.
2. `supported` — a before/after pair corroborated it: clicking the Add
   control produced a page/dialog whose form classifies as `create`;
   clicking Edit produced a prefilled `edit`-classified form; clicking
   Delete produced **either** a confirmation dialog **or** an observed
   row-count decrease in the same collection.

**The task's explicit delete constraint is enforced structurally**: a
delete hypothesis can never advance past `entry_point` on icon/label
evidence alone (`crud_candidate_builder.build_fulfilled`, the `delete`
branch) — it requires at least one of the two corroborating signals above.
Verified by `test_delete_not_advanced_without_corroboration` in
`tests/test_crud_surface_discovery.py`.

Hypotheses are deduplicated by `(operation, entity, collection/form scope)`
— re-observing the same control across iterations upserts (merging
evidence, raising `observation_count`), never duplicates.

---

## 6. List ↔ form ↔ record relationship discovery (graph)

**Module:** `app/intelligence/knowledge_graph/crud_graph_projector.py`,
wired into `GraphSynchronizer.synchronize(crud_registry=...)` (a fifth
projector alongside entity/actor/workflow/dependency, reusing the same
deterministic `edge_id()`/`upsert_node`/`upsert_edge` primitives — no new
graph machinery).

New edge types (`app/intelligence/knowledge_graph/schemas.py`
`SUGGESTED_EDGE_TYPES`):

| Edge | Meaning | Gated on |
|---|---|---|
| `lists_entity` | collection → entity | any hypothesis referencing that entity/collection |
| `opens_create_form` | collection → form | `required_form_id` known |
| `creates_entity` | form/control → entity | `status ∈ {supported, executed, verified}`, `operation="create"` |
| `edits_entity` | form/control → entity | same, `operation="edit"` |
| `deletes_entity` | control → entity | same, `operation="delete"` — **never for a bare entry-point** |
| `returns_to_list` | form/control → collection | create/edit hypotheses whose expected transition ends back at the list |
| `verifies_in_collection` | form/control → collection | the hypothesis's `verification_surface` names that same collection |

CRUD projection never invents an entity node — it only links to one Entity
Discovery (or a prior CRUD observation) already named, deferring otherwise
via the existing `RelationshipResolver`/pending-reference mechanism.

---

## 7. Form lifecycle

`app/agent/form_lifecycle.py`'s `FormLifecycle` enum gained `CLASSIFIED`
(new) and `CANDIDATE_FOR_TESTING` (new) states — `discovered → classified →
inspected → candidate_for_testing → data_plan_created → ... → succeeded /
blocked / failed / exhausted`. `NOT_TESTED_REASONS` (new,
`app/agent/form_lifecycle.py`) enumerates exactly the task's requested set
(`unsafe`, `missing_data`, `unknown_intent`, `unsupported_control`,
`no_verification_strategy`, `actor_unavailable`, `write_disabled`,
`duplicate`, `no_runtime_action`, `other`).

`AppForm` (`app/application/models.py`) gained `intent`,
`intent_confidence`, `target_entity_hypothesis`, `operation_hypothesis`,
`intent_evidence`, `intent_alternatives`, and `not_tested_reason`.
`ApplicationStore` (`app/application/store.py`) gained `mark_form_
classified`/`mark_form_candidate_for_testing`/`mark_form_not_tested`/
`mark_form_verified`/`mark_form_blocked`/`mark_form_failed`. Wired from
`AgentController._run_perception_engine` (best-effort, non-fatal, matching
every other perception-derived hook) — every classified form on every
observed page now has its intent recorded, so "0 forms tested" is always
paired with *why*, never a silent gap.

---

## 8. Safety implications

- Every module in this document is **read-only perception/classification**.
  None of it changes `safe_mode`, `allow_controlled_writes`,
  `allow_safe_test_data_creation`, or `enable_autonomous_investigation`, and
  none of it proposes, validates, or executes a `BrowserAction`.
- A `CRUDWorkflowHypothesis`'s `safety_classification` is informational
  metadata for a human/downstream engine, not an enforcement mechanism —
  `destructive` (all delete hypotheses) is a strong signal to route through
  extra scrutiny before any future execution path is built on top of this,
  but this milestone adds no such execution path.
- The live-verification captures in this change (`backend/scripts/crud_
  discovery_live_capture.py`) never submit a create/edit form and never
  click a delete/edit control — they observe exactly one page (plus an
  optional login) and stop. No destructive operation was performed against
  any target during this work.

---

## 9. Limitations (found via live verification, not merely theorized)

- **Overlapping collection detection on complex real grids.** Against
  OrangeHRM's live PIM employee list, the ARIA-grid detector correctly
  found the real grid (`aria_grid`, 50 rows, 100 row actions), but the
  independent div-repeated-row heuristic also fired on smaller internal
  structures (filter-panel field groups, a 9-item sidebar-adjacent list)
  that are not, semantically, separate record collections. The `.closest()`
  ancestor exclusion prevents re-scanning a container already classified as
  a grid, but does not prevent a *different* repeated structure elsewhere
  on a complex page from independently qualifying. Net effect: some noise
  (extra low-value `RecordCollection` entries), not silent failure — but a
  consumer must be prepared to see more than one collection per page and
  should weight by `row_count`/`structural_signals` rather than assuming
  exactly one canonical grid.
- **Search/filter classification depends on generic vocabulary appearing in
  field labels.** OrangeHRM's own filter form (Employee Name, Employee Id,
  Employment Status, ...) does not use the words "search"/"filter" in any
  field label, so it was classified `create` (0.5) with `search` correctly
  surfaced as an alternative (0.35) rather than silently dropped — but the
  primary classification was wrong on this specific live page. Fixing this
  generically (without hardcoding OrangeHRM's field names) would need a
  stronger structural signal than label text — e.g. "this form's fields are
  all optional and its submit target does not change the collection's row
  count" — left for future work rather than special-cased here.
- **Row-level "add" actions are not yet turned into a CRUD hypothesis.**
  Live verification against SauceDemo found repeated per-row "Add to cart"
  buttons correctly classified as the `add` verb by `action_semantics.py`,
  but `crud_candidate_builder.py` only builds `create` hypotheses from
  *global* add controls, not per-row ones — a deliberate, conservative
  choice (a repeated per-row "add" reads more like a bulk/quick-add
  interaction than a distinct "create a new record" workflow), but it means
  that pattern currently produces no hypothesis at all rather than a
  (possibly wrong) one.
- **Modal-dialog form fields are only captured as a `FormDescriptor` if the
  dialog's markup uses a literal `<form>` tag** (see docs/debug/
  ORANGEHRM_CRUD_EXECUTION_GAP.md §6) — a bare `<div role="dialog">` with
  unwrapped inputs is still perceived (dialog detection itself is
  unaffected by this work), but its fields show up as generic interactive
  elements, not form fields, so form-intent classification never runs on
  them. Not addressed by this change.
- **Two DOM-observation pipelines still exist** (see the same audit,
  contributing cause). Everything in this document lives in the newer
  `app/perception/` pipeline, which — as documented before this work —
  feeds entity/actor/workflow/dependency/CRUD discovery and the Application
  Knowledge Graph, but does **not** drive the legacy `app/agent/explorer.py`
  page classification, frontier candidate scoring, or the `tables_
  discovered`/`create_form` counts a run's final report shows. This work
  makes GemmaQA *understand* record-management interfaces far better; it
  does not, by itself, change what the Planner/Frontier or the final report
  show for a given run — that remains gated by the fixes described in the
  linked audit's prioritised fix plan (§9 there).

---

## 10. Live verification evidence

Captured with `backend/scripts/crud_discovery_live_capture.py` (perception-
only — logs in if credentials are given, observes exactly one page, never
submits a create/edit/delete action):

| Target | Result |
|---|---|
| ServiceFlow (local demo app, `/customers`) | `native_table` collection, 5 rows, columns `[NAME, EMAIL, PHONE, STATUS, SERVICE, ACTIONS]`; search+filter controls attached; global "Add Customer" control correctly classified `add`; all 5 row "Actions" dropdown triggers correctly classified `more_actions` (a real `aria-haspopup="menu"` + visible-text-labelled kebab, the exact case that motivated the §3 fix); a `create` `CRUDWorkflowHypothesis` generated with `entity_hypothesis="Customers"`. |
| ServiceFlow (local demo app, `/customers/new`) | The Add Customer form correctly classified `create` (0.5, "most fields are empty"). |
| SauceDemo (`saucedemo.com/inventory.html`) | Product-tile grid correctly detected as `div_row_group` (6 rows); login form correctly classified `authentication`; per-row "Add to cart" buttons correctly classified `add` (see §9 limitation on why this doesn't yet produce a hypothesis). |
| OrangeHRM demo (PIM Employee List, the exact page the linked audit found undetectable) | **The real employee grid is now detected** (`aria_grid`, 50 rows, 100 row actions — 2 per row, edit+delete icon buttons); 2 `create` entry-point hypotheses generated with `entity_hypothesis="PIM"`; `tables_native` remains 0 (confirming the ARIA-grid path, not the native-table path, is what rescues this case). See §9 for the known noise/misclassification limitations found on this same page. |

Two real bugs were found and fixed during this verification (not merely
theorized): the kebab-menu detector originally required `is_icon_only`,
missing ServiceFlow's text-labelled "Actions" trigger (§3); and the
global-action position margin (120px) was too tight for ServiceFlow's real
page-header layout (widened to 240px after measuring the live page's actual
geometry, `app/perception/collection_extractor.py`).

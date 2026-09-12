# Submission Outcomes and Test-Data Validity

Why GemmaQA's writes were silently failing, and what now happens instead.

## The symptom

Run against a live contact-management application: GemmaQA registered an
account, reached the contact list, clicked "Add a New Contact", filled all
eleven fields, and clicked Submit. The run reported a completed create. No
contact existed. Read, update, and delete were never reached.

## What was actually happening

Six defects, in the order they compounded:

**1. One `address` semantic type served every address field.**
`app/agent/field_constraint_inference.py` collapsed street, city, zip, postal,
and country into a single pattern, and the generator produced a *street address*
for all of them — `City: "108 GemmaQA_TEST_..._Test Street"`,
`Postal Code: "110 GemmaQA_TEST_..._Test Street"`. Separately, `\bstate\b` in the
`status` pattern swallowed "State or Province" and filled it with `"Active"`.

**2. The safety validator prefixed every fill value.**
`app/safety/validator.py` prepended `GemmaQA_TEST_` to *any* fill, so the
application received `GemmaQA_TEST_2026-01-15` in a date field and
`GemmaQA_TEST_+1-555-0105` in a phone field capped at 15 characters. On retry it
prefixed again: `GemmaQA_TEST_106 GemmaQA_TEST_..._Test Street`. The marker's
purpose is to make created records findable for cleanup — but a write that fails
creates nothing to find.

**3. A submit was judged by whether the click worked.**
`GenericFormWorkflow.note_result(success=True)` meant "the button was pressed",
and set the workflow to `verified`. The server had answered `400 Bad Request`.

**4. The refusal taught it nothing.** It clicked Cancel, re-opened the form, and
prepared to submit identical data — until the action budget ran out.

**5. The observer never saw the error message.** The application rendered
`Contact validation failed: phone: Phone number is invalid, street1: ... longer
than the maximum allowed length (40)` into a `<span id="error">`. The alert
selector matched only `[role="alert"], .alert, .Alert`.

**6. Two unrelated loops wasted the remaining budget.** Canonical-model candidate
builders signed their candidates with the canonical fingerprint while
`remember_action` recorded the legacy one, so `has_seen_signature` never matched
and the same candidate was offered forever (ten consecutive screenshots of one
image). And `step_budget` granted one extra step per retry attempt, so a retry
ran out of steps mid-refill and died one action before it would have re-submitted.

## What happens now

```
submit → classify_submission(before, after)      app/agent/submission_outcome.py
           ├─ accepted            → workflow verified, write counted
           ├─ rejected_validation → learn, correct, retry (bounded)
           ├─ rejected_server     → application defect candidate
           └─ unknown             → submitted_unverified, never "verified"
```

`classify_submission` reasons from four generic signals: a failed *mutating*
request appearing after the action (4xx vs 5xx), whether the form is still on
screen, whether the location changed, and whether new validation text appeared.
Failures are matched by count per `(status, url)`, not by URL — a retry hits the
same endpoint, and deduping by URL made every refusal after the first invisible.

On a refusal the workflow **re-plans conservatively** rather than resubmitting:
`generate_conservative_value` produces the shortest, plainest valid-looking value
— digits-only phone numbers, short ASCII text, no run prefix. Which fields get
retried:

- **Attributed** — when the application's own message names a field (matched
  against that field's label tokens), only that field is re-planned.
- **Unattributed** — browsers do not expose response bodies, so this is common.
  Rather than re-typing every field (one action each; an eleven-field refill
  consumed a whole budget), only *risky-looking* values are retried: those
  carrying the run prefix, longer than 30 characters, or containing characters
  outside `[A-Za-z0-9 @._-]`. On the observed form that selected 4 fields of 11 —
  exactly the ones the backend had refused.

Bounded by `max_attempts`; after that the workflow fails and the refusals are
reported as evidence rather than discarded.

## Judging bug vs. not a bug

- **5xx** — the application failed. `is_application_defect_candidate` is True.
- **4xx** — the application *refused*. That is a defect only if the data was
  valid, which GemmaQA cannot claim until a conservative retry has also been
  refused. So a first 4xx is recorded as a refusal, not filed as a bug.
- **unknown** — proven neither way, and reported as neither.

## Honest reporting

`FinalReport.submission_outcome_summary` distinguishes writes the application
**accepted** from writes it **refused** from writes that are **unproven**,
with HTTP status, the application's own message, and the signals behind each
verdict. Before this, a refused write and a completed one looked identical.

## Design rules worth keeping

- **The click succeeding and the application accepting are different facts.**
  Only the second one is progress.
- **A traceability marker must never invalidate the value it marks.**
  Format-constrained values (dates, phones, numbers, emails, URLs, postal codes)
  are exempt; the exemption is shape-based, not field-name-based.
- **Retry differently or not at all.** Re-submitting data the application
  already refused cannot succeed and costs budget that read/update/delete need.
- **Say what you could not attribute.** `rejection_not_attributable_to_a_specific_field`
  is a signal in the report, not a silent fallback.

## Tests

`tests/test_submission_outcome.py` — 53 tests, network-free, grouped A–G:
address-family semantic types · conservative retry values · outcome
classification · field attribution · learn-instead-of-repeat · the test-data
marker not corrupting values · honest reporting.

## Reaching the record (the route to update and delete)

With create working, `update`/`delete` were still at **zero discovered** — not
zero executed. Their controls live inside a record, and the run could not open
one. Three separate causes:

1. **Native table rows were given no element id.** The ARIA-grid and div-group
   row paths in `dom_extractor` minted one; the `<table>` path did not. A row
   with no id cannot be clicked, so a record list offered no way in.
2. **Clickability has no DOM trace in a modern framework.** The observed
   application's rows navigate on click while exposing no `onclick`, no `role`,
   no `tabindex`, and no pointer cursor — the handler is attached in JavaScript.
   So `CollectionRow.activation_basis` is now graded: `observed` when a DOM
   signal proves it, `structural_hypothesis` when the row carries cells and no
   controls of its own, `none` when its own buttons are the real actions.
   Absence of a signal is not evidence of absence. Acting on a hypothesis is a
   read-only click, and a wrong one self-corrects — no transition follows and
   the candidate is recorded as attempted. Speculative rows are capped at
   `MAX_HYPOTHESIS_ROWS_PER_COLLECTION` so a large grid cannot become a large
   number of guesses.
3. **Nothing linked a row to the record the run created.**
   `_build_record_row_candidates` matches row cells against the Temporary Record
   Registry's `generated_identity` (substring both ways, since lists reformat and
   truncate) and ranks that row above the rest — opening our own record both
   verifies the create and is the only route to its update/delete controls, and
   it is safe to mutate because the registry owns it.

### Abandoning work the run paid to reach

`active_workflow_continuity` (weight 100) only fires once a workflow is *in
flight*. Between "the form was inspected" and "the workflow started" there was no
continuity signal, so a Cancel button's `navigation_centrality` (22) outscored
`workflow_value` (18). Seen twice in one run: after inspecting the create form it
clicked Cancel and re-opened the same form two actions later; and after clicking
"Edit Contact" it immediately clicked Cancel, abandoning the update it had spent
the whole run reaching.

`flow_abandonment_penalty` (34) fires on a candidate that leaves a page while the
frontier is still offering form work there. A penalty, not a hard rejection — a
genuine dead end must still be escapable.

*This reversed an assertion in `test_execution_gap_closure.py` that documented the
old ordering in-place as "PRE-EXISTING (if imperfect)". It was an acknowledged
imperfection, and it was doing real damage.*

### Verified live

```
25. submit                            → accepted (record created)
26. inspect_table                       (list now has a row)
27. click el_008  open_record_row_el_008 → opened its OWN created record
28. inspect_form form_005                (record detail)
29. click el_002  "Edit Contact"       → reached the update form
30-39. fill …                          → filling it, no Cancel
```

`/contactDetails` appears in `pages_visited` for the first time.

## The record lifecycle

`TemporaryRecordRegistry` has always defined the whole lifecycle —
`created → verified → updated → cleanup_requested → delete_action_validated →
deleted → absence_verified` — and enforced its transitions. Nothing ever
advanced a record past `verified`, because **three separate components assumed a
record's update and delete controls live in the COLLECTION that lists it.** On
most applications they live on the record's own detail page.

| Component | Assumption | Now |
|---|---|---|
| `crud_candidate_builder` | a CRUD control hangs off a `RecordCollection` | also reads a *record-detail state* (no collection + per-record `action_semantics`), so `edit`/`delete` become **discovered** |
| `infer_form_purpose` | any form with fields is a create | a form arriving **prefilled** is `safe_test_data_update` |
| `cleanup_planner` | delete is a collection row action | falls back to a detail-page `delete` control, behind a strict identity guard |

### An update is not a create

A create fills every field; an update asks *"does changing this one thing
persist?"*. `_plan_single_field_update` changes exactly one field and records its
before/after — **2 actions instead of 12** on an eleven-field form, which is the
difference between finishing the cycle and running out of budget. The field
chosen already has a value (so before/after is meaningful) and is never an
identity or format-constrained one (changing a unique email tests uniqueness, not
updating; a broken format is not an update failure). If nothing is safely
changeable the workflow reports `blocked`, rather than mutating a field whose
format it would probably break.

### Two deliberate restrictions

- **Only records this run created are updated.** A prefilled form belonging to
  pre-existing application data is left alone — `_has_updatable_owned_record`
  requires a registry entry in state `verified`.
- **A record is updated once.** Observed live: after an accepted update the edit
  form was still offered, and the run edited the same record three times in six
  actions. The registry's own `verified → updated` transition is the authority,
  read rather than tracked separately — a create form and the edit form for the
  record it created can carry the *same* per-observation form id.

### Deleting is guarded by identity, not by intent

A list row proves which record a row-level delete belongs to. A detail page's
single Delete button proves nothing on its own, so `_page_shows_identity`
requires the record's own identity value to be visible on the page — in its text,
a heading, or a field value. When it cannot be confirmed the record stays
pending. Cleanup still runs entirely through `ActionValidator` (destructive
actions disabled unless the run opts in) and the registry's `run_id` check, so
GemmaQA can only ever delete what it made.

### Verified live

```
CRUD coverage    create 1/1 · read 1/1 · update 2 discovered / 1 executed · delete 1 discovered
record state     {"updated": 1}          ← advanced past `verified` for the first time
submissions      accepted 2 · refused 1
```

```
27. open_record_row     → its own created record
29. "Edit Contact"      → update form
30. fill (one field)    ┐
31. submit              ┘ accepted → registry verified → updated
32. "Edit Contact"      → no update offered (already updated)
33. Cancel              → correctly leaves instead of re-editing
```

## Known limitations

- Field attribution needs the application to render its validation message in the
  page. When it does not, retry falls back to the shape heuristic.
- **Delete is discovered but usually not executed.** `_run_cleanup_pass` runs
  once at the end of the run against **whatever page is currently open** and does
  not navigate to find the record — a limitation its own docstring has always
  stated. A run that ends mid-create leaves the record `pending_cleanup` with
  manual instructions in the report. Delete is now reachable and classified; it
  is not yet reliably *performed*. Making cleanup navigate to the record is the
  obvious next step and was not attempted here.
- **Run-to-run behaviour is more stable but not deterministic.** The update-once
  rule removed the repeat-edit loop, and preferring an owned unfinished record
  removed most of the create-again drift. Runs still differ in what they do with
  leftover budget after the cycle completes.
- One fill still costs one action. A wide form is expensive, and a create plus an
  update is ~26 actions on an eleven-field form. Batching fills would change the
  one-action-per-observation contract and was not attempted here.

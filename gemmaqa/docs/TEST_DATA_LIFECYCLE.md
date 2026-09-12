# Safe Test-Data and Temporary-Record Lifecycle

Application-neutral test-data generation and a run-scoped registry that
tracks every record GemmaQA itself creates, so it can be verified, safely
updated, and — only when explicitly permitted — cleaned up again. Nothing
in this system is specific to any target application: it works entirely off
the structural signals already produced by the Universal Page Perception
Engine (see [CANONICAL_PAGE_MODEL.md](CANONICAL_PAGE_MODEL.md) and
[CRUD_SURFACE_DISCOVERY.md](CRUD_SURFACE_DISCOVERY.md)).

## Modules

| Module | Responsibility |
| --- | --- |
| `app/agent/test_data_schemas.py` | Closed-vocabulary schemas: `FieldConstraint`, `TestDataValue`, and every enum (semantic types, data categories, validity intent, uniqueness strategy, generation source, sensitivity, cleanup status). |
| `app/agent/field_constraint_inference.py` | `infer_semantic_type()` / `infer_constraint()` — structural-signal-first classification of a field's semantic type and shape constraints, every populated attribute backed by an evidence string. |
| `app/agent/test_data_generator.py` | `generate_valid_value()` / `generate_boundary_values()` / `generate_negative_values()` / `generate_duplicate_probe()` — the concrete value producers. |
| `app/agent/file_upload_fixtures.py` | The fixed set of repository-controlled safe files GemmaQA is ever allowed to upload. |
| `app/agent/temporary_record_registry.py` | `TemporaryRecordRegistry` — the run-scoped store of every record GemmaQA created, its lifecycle state, and the delete-eligibility gate. |
| `app/agent/creation_verifier.py` | `verify_creation()` / `verify_update()` / `verify_deletion()` — independent evidence checks; a successful click alone is never treated as a successful create/update/delete. |
| `app/agent/cleanup_planner.py` | `plan_cleanup_action()` — turns a pending registry entry into the next `BrowserAction` needed to delete it, using only structural signals (`RecordCollection.row_actions`, `ActionSemantics`, `DialogDescriptor`). |

## Data-generation rules

- **Semantic types** (`FIELD_SEMANTIC_TYPES`): first/middle/last name, username,
  email, phone number, employee identifier, free text, description, date,
  date range, status, role, dropdown option, autocomplete selection,
  boolean, number, decimal, url, address, file upload, unknown.
- **Identifiable prefix**: every generated value that ISN'T a human name
  extends the existing `app.safety.policies.TEST_DATA_PREFIX`
  (`"GemmaQA_TEST_"`) with a run fragment and a timestamp fragment —
  `GemmaQA_TEST_<run-fragment>_<timestamp-fragment>_...` — so
  `ActionValidator`'s existing `value.startswith(TEST_DATA_PREFIX)` check
  keeps recognising every value this module produces, with no second,
  incompatible marker introduced.
- **Human names are the deliberate exception**: a `GemmaQA_TEST_...`-prefixed
  first name would itself look like a data-quality bug in a screenshot or
  demo recording. Name fields instead draw from a small fixed pool of
  ordinary-looking synthetic names (`_FIRST_NAMES`/`_MIDDLE_NAMES`/
  `_LAST_NAMES` in `test_data_generator.py`) and are tagged
  `sensitive_classification="synthetic_pii_like"`. The run/record linkage
  lives entirely in `TestDataValue.run_id`/`temporary_record_id`, never in
  the visible string.
- **File uploads** use ONLY the fixed files under
  `app/agent/fixtures/safe_uploads/` — nothing is generated on the fly and
  nothing is ever sourced from the target application or user input.
- **Determinism**: every generator accepts an optional `seed`; the same
  `(run_id, field_semantic_type, seed, at)` always produces the same value.
  Without an explicit seed, generation is still deterministic per-process
  (an incrementing counter), never `datetime.now()`/`random`-based.
- **Negative values are always safe**: `generate_negative_values()` produces
  shape-invalid data only (empty/below-min/above-max/wrong-format/bad-range/
  unsupported-but-harmless characters) — never an injection payload
  (no `<script>`, no SQL breakout, no shell metacharacters used
  destructively). Its job is to confirm the application's OWN validation
  rejects bad input, not to attempt an exploit.

## Constraint inference

`infer_constraint()` reads, where present: the `required`/`aria-required`
indicator, `minlength`/`maxlength`, `pattern`, `min`/`max`/`step` (numeric),
the raw `min`/`max` string for date inputs (`min_value_raw`/`max_value_raw`
— HTML date inputs carry these as ISO date strings, which never parse as
the float-only `min_value`/`max_value`), input type, placeholder,
accessible description, nearby help text, existing option values, dependent
field keys, and observed rejection messages. Every populated attribute is
recorded in `FieldConstraint.evidence`; `confidence` starts at 0.3 and rises
with each directly-observed HTML constraint attribute — a label-text-only
semantic-type guess never receives the same confidence as an observed
`required`/`minlength`/`pattern` attribute.

## Temporary Record Registry and cleanup lifecycle

States: `created` → `verified` → (optionally) `updated` → `cleanup_requested`
→ `delete_action_validated` → `deleted` → `absence_verified`, plus
`cleanup_failed` and the terminal `manual_cleanup_required` failure path.
Transitions are enforced by an explicit allow-list
(`_ALLOWED_TRANSITIONS`) — no code path can skip states.

**Hard safety gate**: `eligible_for_cleanup()` returns `True` only if the
entry's `run_id` matches the CURRENT run, unless the caller explicitly
passes `allow_cross_run_cleanup=True`. There is no "purge everything"
operation anywhere in this module.

**Verification before registration**: `AgentController._verify_and_register_
temporary_record` only registers a record after `creation_verifier.
verify_creation()` finds independent evidence (toast, redirect, record
detail, list row, or a stable identifier) — a form workflow whose submit
action merely didn't error (`GenericFormWorkflow.state == "verified"`) is
left unregistered, and therefore is never a cleanup candidate, unless real
evidence was found.

**Cleanup execution**: `AgentController._run_cleanup_pass` runs once, at
the end of a run, over every record `records_pending_cleanup()` returns.
For each eligible entry it calls `cleanup_planner.plan_cleanup_action()`
against whatever page is currently open, and — exactly like every other
action in the system — validates the resulting `BrowserAction` through
`ActionValidator` and executes it through the same `ActionExecutor` before
advancing the registry. `ActionValidator`'s own `destructive_actions_
disabled` gate (true by default) means nothing here is ever destructive
unless the run was already configured with `allow_destructive_actions=
True`; this pass adds no new authority.

**Bounded failure**: `mark_cleanup_failed()` never retries indefinitely —
after `MAX_CLEANUP_ATTEMPTS` (3) the entry moves to
`manual_cleanup_required` and `manual_cleanup_report()` returns a
human-readable instruction naming the record's type, generated identity,
and `temporary_record_id` so it can be located and removed manually in the
target application's own UI.

## Verification requirements

- **Creation**: `verify_creation()` looks for a success toast, a URL
  redirect, the expected value appearing in page text (record detail), a
  matching row in a `RecordCollection` (list row / search result), or a
  URL-embedded stable identifier. A bare successful click with none of
  these returns `verified=False` — "do not treat a successful click as
  successful creation" is enforced directly by this function's default
  behaviour, not by convention.
- **Update**: `verify_update()` requires the field's value to have actually
  changed AND match the expected value — an unchanged value never counts
  as a verified update even if the submit action succeeded.
- **Deletion**: `verify_deletion()` requires either a decreased collection
  row count or the identity string's absence from observed page text.

## Known limitations

- **Cleanup is opportunistic, not exhaustive**: `_run_cleanup_pass` acts
  against whatever page is open when the run ends. It does not navigate
  back to a record's list page to find a delete control that isn't
  currently visible — such records are left `verified`/`updated` (pending)
  with their identity intact for a future run or manual cleanup, never
  silently dropped.
- **File-upload execution is not yet wired end-to-end**: the generator can
  produce a valid, safe fixture path for a `file_upload` field
  (`generation_source="fixture_file"`), but there is no dedicated
  `ActionType`/executor support in this codebase for actually dispatching a
  `set_input_files`-style browser action yet. `GenericFormWorkflow` will
  plan the value; submitting it end-to-end requires a follow-up change to
  `ActionExecutor`/`BrowserAdapter`, out of scope for this change.
- **Very short `max_length` constraints can truncate the uniqueness
  suffix**: the identifiable-prefix scheme appends a run/timestamp fragment
  after the value; a field whose observed `max_length` is shorter than the
  full prefix will have that suffix clipped from the end, the same
  trade-off every prefix-tagged semantic type accepts under a tight
  `max_length`. Human-name fields are unaffected (no prefix is embedded).
- **Absence verification only checks currently-loaded collections**: if a
  target application doesn't refresh its list view immediately after a
  delete, `mark_absence_verified()` is not called until a later observation
  shows the row gone; the record stays in the non-terminal `deleted` state
  in the meantime rather than being incorrectly marked absent.

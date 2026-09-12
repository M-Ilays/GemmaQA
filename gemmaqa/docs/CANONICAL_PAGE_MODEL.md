# Canonical Page Model

An application-neutral schema layer representing everything GemmaQA can explicitly
observe on the current browser page. Implements recommendation #1/#2 of
[`PAGE_PERCEPTION_AUDIT.md`](./PAGE_PERCEPTION_AUDIT.md).

**Status: schema layer only.** Nothing in this change reads or writes this model at
runtime yet. `Planner`/`FrontierBuilder` behavior is unchanged; no LLM call was added;
no hypothesis/inference logic was added. `CanonicalPageModel.from_page_state()` is a
deterministic, mechanical adapter over data the observer already extracts — the next
integration step (not part of this change) is having `PageObserver` populate the model
directly and having consumers read from it.

## Where the code lives

| Concern | Location |
|---|---|
| Extended existing schemas (backward compatible) | `backend/app/schemas.py` — `InteractiveElement`, `FormDescriptor`, `TableDescriptor` |
| New additive companion schemas | `backend/app/schemas.py` — `FormFieldDescriptor`, `TableColumnDescriptor`, `TableRowDescriptor` |
| New Canonical Page Model schemas | `backend/app/perception/models.py` |
| Tests | `tests/test_canonical_page_model.py` (50 tests) |

## Design decisions

**Reuse over duplication.** Three of the requested schema names — `InteractiveElement`,
`FormDescriptor`, `TableDescriptor` — already existed in `app/schemas.py` with the
right purpose. Rather than create parallel duplicates, they were **extended in place**
with new, optional, defaulted fields. `CanonicalPageModel.forms`,
`CanonicalPageModel.tables`, and `CanonicalPageModel.interactive_elements` are typed
against these exact same classes (verified by
`test_canonical_page_model_reuses_existing_schemas_not_duplicates`), so a `FormDescriptor`
built by the existing observer today is *already* a valid element of a
`CanonicalPageModel.forms` list with zero conversion.

**Backward compatibility.** Every field added to the three extended schemas has a safe
default (`None`, `1.0`, `"dom"`, `"observed"`, `[]`, `{}`). No existing field was
renamed, retyped, or removed. The full pre-existing test suite (342 tests) passes
unchanged; three tests explicitly assert the extended schemas still construct from only
their original fields.

**One shared field vocabulary.** Every descriptor inherits from `PerceivedElementBase`
(in `app/perception/models.py`), which carries the full common field list the task
specified — `stable_id`, `element_id`, `parent_region_id`, `source`, `text`,
`accessible_name`, `accessible_description`, `dom_tag`, `aria_role`, `attributes`,
`is_visible`, `is_enabled`, `is_expanded`, `is_selected`, `is_checked`, `is_required`,
`url`/`is_external_url`, `bounding_box`, `confidence`, `evidence`, `status`,
`fingerprint_contribution` — so "where applicable" is satisfied uniformly instead of
copy-pasted per class. A subclass that doesn't need a field (e.g. a `TextBlockDescriptor`
has no meaningful `is_checked`) simply leaves it at its default; that's intentional,
not an oversight.

The three *extended existing* schemas (`InteractiveElement`, `FormDescriptor`,
`TableDescriptor`) do **not** inherit `PerceivedElementBase` — they gained the same
fields directly, using a plain `float` for `confidence` and `list[str]` for `evidence`
(rather than the richer `ConfidenceScore`/`EvidenceReference` objects) specifically to
avoid a dependency from `app/schemas.py` onto the new `app/perception` package. The
richer objects are reserved for the new, perception-exclusive descriptors.

**No business-specific fields.** Every field name across all 24 schemas is
application-neutral. A dedicated test
(`test_canonical_page_model_has_no_business_specific_fields`) asserts none of
`customer`/`invoice`/`product`/`job`/`contact` appear in any `CanonicalPageModel` field
name, and no schema anywhere in this change encodes a business concept — only
structural/generic ones (`region_type` is one of `header/nav/main/aside/footer/unknown`,
never e.g. `customer_list`).

## Schema reference

### Shared building blocks

- **`EvidenceReference`** — a pointer to supporting evidence (`screenshot` /
  `dom_snapshot` / `network` / `console` / `manual`), never inline binary data.
- **`ConfidenceScore`** — `value` (clamped `0.0`-`1.0`) + `basis`
  (`direct_observation` / `heuristic` / `inferred` / `llm_hypothesis`) + `notes`. A
  confidence is never a bare float with no stated justification.
- **`PerceivedElementBase`** — the common field set described above; every new
  descriptor below extends it.

### Structural / layout

- **`PageRegion`** — a markup/ARIA landmark (`region_type` ∈
  `{header, nav, main, aside, footer, unknown}`), with `child_region_ids` for nesting.
- **`VisualRegion`** — a layout-inferred cluster from bounding-box geometry alone
  (`layout_hint` ∈ `{top, bottom, left, right, center}`), distinct from `PageRegion`.
- **`HeadingDescriptor`** — `level` 1-6.
- **`TextBlockDescriptor`** — `block_type` ∈
  `{paragraph, summary, description, label, caption, other}`.

### Navigation

- **`NavigationItem`** / **`NavigationRegion`** — a landmark plus its items;
  `NavigationItem.is_current` is where `aria-current` would land once the observer
  captures it.
- **`BreadcrumbDescriptor`** — `ordinal` + `target_url`.
- **`PaginationDescriptor`** — `current_page`/`total_pages`/`item_ids` (nullable —
  today's flat `pagination_controls` text can't populate these; see Known Gaps).
- **`LinkDescriptor`** — `target_url` + `is_external` (computed via the existing
  `app.application.url_normalize.same_origin`, not a heuristic guess).

### Tabs

- **`TabDescriptor`** / **`TabGroup`** — `TabGroup.selected_tab_id` is where
  `aria-selected` would land once the observer captures it (currently always `None` —
  see Known Gaps).

### Media

- **`ImageDescriptor`** — `alt_text`, `surrounding_text`, `src`.

### Overlays

- **`DialogDescriptor`** — `dialog_type` ∈ `{dialog, modal, drawer, popover}`,
  `contained_element_ids` (links overlay content to the elements inside it — not
  populated yet, since today's flat `dialogs`/`modals` text lists carry no such link).
- **`AlertDescriptor`** — `severity` ∈ `{info, success, warning, error}`.

### Network + unknowns

- **`NetworkEvidence`** — method/status/resource_type/failed/failure_text/timing_ms.
- **`UnknownComponent`** — an element detected as *something* but not classifiable.
  Its existence at all is the point (audit §3's "no fallback bucket" gap) — not yet
  populated because the observer doesn't detect non-semantic clickables yet.

### Forms / tables (extended existing + new companions)

- **`FormFieldDescriptor`** *(new, in `app/schemas.py`)* — a richer, additive
  companion to the existing `FormField`. `FormDescriptor.field_descriptors` carries
  it; `FormDescriptor.fields` (the original `list[FormField]`) is untouched.
- **`TableColumnDescriptor`** / **`TableRowDescriptor`** *(new, in `app/schemas.py`)* —
  `TableRowDescriptor.row_action_element_ids` is the direct fix for the audit's
  "row actions are never linked to their row" finding. `TableDescriptor.columns`/`.rows`
  are additive alongside the original `headers`/`sample_rows`.

### Fingerprint

- **`PageStateDescriptor`** — makes the page-state fingerprint's inputs explicit
  (`fingerprint` + `contributing_fields`) instead of an opaque hash alone.

### The model itself

- **`CanonicalPageModel`** — `page_id`, `url` (validated absolute), `domain` (derived
  from `url` if not given), `title`, `state_fingerprint`, `page_state`, `regions`,
  `navigation_regions`, `headings`, `text_blocks`, `breadcrumbs`, `pagination`, `links`,
  `tabs`, `forms`, `tables`, `images`, `dialogs`, `alerts`, `visual_regions`,
  `interactive_elements`, `unknown_components`, `network_evidence`,
  `screenshot_reference`, `observed_at`.

## Validation

- `ConfidenceScore.value` clamped to `[0, 1]`.
- `EvidenceReference.kind`, `PerceivedElementBase.source`/`.status`, and every
  controlled-vocabulary field (`region_type`, `dialog_type`, `severity`, `block_type`,
  `layout_hint`) reject values outside their fixed generic set.
- `CanonicalPageModel.url` must be non-empty and absolute (scheme + host).
- `CanonicalPageModel.domain` is derived from `url` via a `model_validator` when not
  supplied explicitly.
- A `model_validator` rejects duplicate `stable_id` values across every nested
  collection in one `CanonicalPageModel` — no two objects in the same page snapshot
  can silently collide.

## Serialization

- `to_dict()` — JSON-safe dict (`model_dump(mode="json")`; datetimes become ISO
  strings).
- `to_json(indent=None)` — JSON string.
- `from_dict(data)` / `from_json(text)` — classmethods, round-trip verified in tests.

## The `from_page_state` adapter

`CanonicalPageModel.from_page_state(page_state)` deterministically builds a model from
an existing `PageState` — no LLM call, no hypothesis, no guessed data. Every mapping is
mechanical:

| CanonicalPageModel field | Built from |
|---|---|
| `headings` | `PageState.headings`, `level` fixed at `1` (real level isn't preserved by the current extraction — documented, not guessed) |
| `text_blocks` | `PageState.visible_text_summary` as one `block_type="summary"` block |
| `breadcrumbs` | `PageState.breadcrumbs`, `ordinal` = list index |
| `pagination` | `PageState.pagination_controls` joined into one descriptor |
| `links` | `PageState.interactive_elements` filtered to `category=="link"`/`tag=="a"`; `is_external` via `same_origin()` |
| `navigation_regions` | `PageState.navigation_items` wrapped as one `NavigationRegion` |
| `tabs` | `PageState.tabs` wrapped as one `TabGroup` (no selection state — not captured today) |
| `forms` / `tables` / `interactive_elements` | **passed through verbatim** — same objects, same classes |
| `dialogs` | `PageState.dialogs` (`dialog_type="dialog"`) + `.modals` (`dialog_type="modal"`) |
| `alerts` | `PageState.alerts` (`severity="error"`) + `.toasts` (`severity="info"`) |
| `network_evidence` | `PageState.network_entries` |
| `screenshot_reference` | `PageState.screenshot_path` |
| `page_state` | `PageState.state_fingerprint`, with `contributing_fields` matching `compute_fingerprint()`'s actual inputs exactly |
| `images`, `regions`, `visual_regions`, `unknown_components` | **always empty** — the current observer doesn't extract these; left empty rather than fabricated |

## Known gaps (inherited from the current observer, not fixed by this change)

Per the audit, these fields exist in the schema and are ready to receive data, but
nothing populates them yet because `OBSERVE_SCRIPT` (`app/browser/observer.py`)
doesn't extract the underlying signal:

- `PageRegion`/`VisualRegion` (no landmark/layout extraction yet)
- `ImageDescriptor` (no image inventory extraction yet)
- `UnknownComponent` (no non-semantic-clickable detection yet)
- `TabGroup.selected_tab_id`, `NavigationItem.is_current` (no `aria-selected`/
  `aria-current` extraction yet)
- `DialogDescriptor.contained_element_ids`, `TableRowDescriptor.row_action_element_ids`
  (no element-to-container linkage extraction yet)
- `PaginationDescriptor.current_page`/`.total_pages` (today's extraction is a flat
  label list with no numeric page state)

Closing these requires changing `OBSERVE_SCRIPT` and `PageObserver` — explicitly out of
scope for this change (schema layer only).

## Guardrails honored

- **No Planner behavior change** — `app/agent/planner.py` and `app/agent/frontier.py`
  were not touched.
- **No hypothesis logic** — `from_page_state()` contains only direct field mapping and
  one existing, already-used same-origin comparison; nothing is inferred or guessed.
- **No LLM-returnable selectors/actions** — this module makes no LLM call and has no
  request/response schema an LLM could populate; every field here is either built by
  the deterministic adapter or constructed directly in code/tests.

## Test coverage

`tests/test_canonical_page_model.py` — 50 tests: backward compatibility of the three
extended schemas, `ConfidenceScore`/`EvidenceReference` validation, every
controlled-vocabulary rejection case, `CanonicalPageModel` url/domain/duplicate-id
validation, the "no business-specific field names" contract check, the "reuses,
doesn't duplicate" type-identity check, serialization round-trips, and the full
`from_page_state` adapter (link classification, tab/nav wrapping, dialog/alert
severity mapping, network mapping, screenshot mapping, and the "gaps stay empty,
never fabricated" checks).

**Full suite after this change: 392 passed, 1 skipped, 0 failed** (up from 342 before
this change).

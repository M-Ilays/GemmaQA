# Universal Page Perception Engine

Converts a raw browser observation into the [Canonical Page Model](./CANONICAL_PAGE_MODEL.md).
Implements the integration step that document explicitly deferred. Builds on
[`PAGE_PERCEPTION_AUDIT.md`](./PAGE_PERCEPTION_AUDIT.md)'s findings — most of the specific
gaps it named are now closed.

**Status:** implemented and wired into the observation loop (`app/agent/controller.py`),
running best-effort/non-fatal alongside the existing `PageObserver` on every
observation. `Planner`/`FrontierBuilder` still read `PageState` exactly as before —
this is an additive, parallel data source available for tracing/reporting/future
consumption, not yet a replacement for the existing dispatch path.

## Architecture

```
app/perception/
    dom_extractor.py            one JS pass -> RawObservation (+ headings/text/links)
    accessibility_extractor.py  raw ARIA facts -> AccessibilityInfo + parent-child map
    interactive_detector.py     multi-signal interactive classification -> InteractiveElement
    region_classifier.py        landmark + synthesized regions -> PageRegion / VisualRegion
    navigation_extractor.py     nav/breadcrumbs/pagination/tabs -> typed descriptors
    form_extractor.py           raw forms -> FormDescriptor + FormFieldDescriptor
    table_extractor.py          raw tables -> TableDescriptor + columns/rows (+ row actions)
    image_extractor.py          raw images -> ImageDescriptor
    dialog_extractor.py         raw dialogs/alerts -> DialogDescriptor / AlertDescriptor
    state_builder.py            richer page-state fingerprint
    evidence_merger.py          dedup/merge shared utility
    unknown_detector.py         fallback bucket for unclassified elements
    engine.py                   PerceptionEngine orchestrator
    models.py                  (pre-existing) CanonicalPageModel + descriptors
```

One `page.evaluate()` round-trip (`dom_extractor.RAW_EXTRACTION_SCRIPT`) collects every
raw fact; every other module is pure, deterministic Python over that shared
`RawObservation` — the DOM is only walked once per observation.

## Pipeline (`PerceptionEngine._build_from_raw`)

1. `dom_extractor.extract_raw()` — the JS pass.
2. `accessibility_extractor.build_accessibility_index()` — role/name/description/
   expanded/selected/checked/disabled/required/heading-level per element, with
   parent-child linkage via the nearest landmark region.
3. `dom_extractor.extract_headings/text_blocks/links()`.
4. `interactive_detector.detect_interactive_elements()` — the multi-signal classifier
   (see below), deduped via `evidence_merger`.
5. `navigation_extractor` — nav regions, breadcrumbs, pagination, tabs (with real
   selected-tab state).
6. `form_extractor` / `table_extractor` / `image_extractor` / `dialog_extractor`.
7. `region_classifier` — landmark classification + synthesized form/table/dialog/
   card-group/utility regions + geometry-only visual regions, merged/deduped.
8. `unknown_detector.detect_unknown_components()` — anything visible the raw pass
   noticed but no other step claimed.
9. `state_builder.build_page_state()` — the richer fingerprint.
10. Assembled into one `CanonicalPageModel`; a structured trace event is emitted
    (`app.utils.exploration_trace`, reused rather than duplicated).

## Interactive detection — more than native buttons/links

`interactive_detector.py` combines, per element (`_detect_signals`):

| Signal | Source |
|---|---|
| `native_tag` | `a/button/input/select/textarea/summary` |
| `aria_role` | `role="button"/"link"/"tab"/...` |
| `tabindex` | `tabindex >= 0` |
| `click_handler_attribute` | inline `onclick` attribute/property |
| `pointer_style` | computed `cursor: pointer` on a non-native element — the direct fix for the audit's "non-semantic `<div onClick>` is invisible" finding |
| `expandable_control` | `aria-expanded` present (any value) |
| `custom_dropdown_trigger` | `aria-haspopup` present |
| `table_row_action` | the element's id appears in some table row's `row_action_ids` |
| `navigable_card` | large (≥80×80) pointer-cursor element with no native/ARIA classification — additive to `pointer_style`, not exclusive with it |

`dom_extractor`'s raw pass itself sweeps generic `div/span/li/article/section`
elements for `cursor: pointer` (capped at 60, only ≥24×24px) specifically so these
signals have something to fire on beyond the traditional `interactiveSelector` —
without this sweep, a framework-rendered clickable `<div>` with no ARIA role and no
`onclick` attribute would never even reach `RawObservation.elements`.

Live-verified: on SauceDemo's inventory page, the sort-order control (a
`<span class="select_container">`-wrapped custom dropdown with no native `<select>`
semantics) is now captured as a `pointer_style`-sourced `InteractiveElement` where the
old observer saw nothing.

## Region classification — generic, structural, never business vocabulary

`region_classifier.py` assigns `PageRegion.region_type` from `PageRegion.REGION_TYPES`
(defined in `perception/models.py`, expanded from the original 6-value set to include
`primary_navigation`, `secondary_navigation`, `content_navigation`, `sidebar`,
`main_content`, `workflow_region`, `form_region`, `data_table`, `card_group`,
`dialog`, `utility_region`, `legal_region`, `unknown_region`) using only structural
signals:

- `<header>`/`role="banner"` → `header`; `<main>`/`role="main"` → `main_content`;
  `<aside>`/`role="complementary"` → `sidebar`.
- `<nav>` position: near the top of the viewport → `primary_navigation`; narrow and
  left-positioned → `secondary_navigation`; otherwise → `content_navigation`.
- `<footer>`/`role="contentinfo"`: defaults to `footer`; reclassified to
  `legal_region` **only** as a low-confidence (0.5) guess when its links average
  under 20 characters (a purely structural "short link row" signal, never a word
  match) — deliberately marked uncertain rather than asserted.
- Forms/tables/dialogs get a synthesized region each (`form_region`/`data_table`/
  `dialog`) even on a page with no markup landmarks at all.
- `card_group`: wraps every `navigable_card`-flagged element into one region.
- `utility_region`: a cluster (≥2) of icon-only controls outside any landmark.
- `VisualRegion` (a separate, geometry-only concept from `PageRegion`): every visible
  element bucketed into `top`/`bottom`/`left`/`right`/`center` by bounding-box
  position — a fallback signal for pages with no semantic landmarks at all.

## State building — beyond url/title/headings/controls/text

`state_builder.build_page_state()` produces a `PageStateDescriptor` incorporating,
beyond the existing `compute_fingerprint()`'s inputs: selected tab id(s), open dialog
id(s), expanded-element id(s) (`aria-expanded`), active filters (checked
checkbox/radio controls — a structural definition, no filter-vocabulary matching),
pagination state (`current_page/total_pages`), search state (the value of a native
`type="search"` field), the set of visible region types, and an explicitly-injected
`known_role` (never inferred by this module itself — the caller supplies it, keeping
this deterministic and free of its own auth logic). Determinism is unit-tested
directly: identical input always produces an identical fingerprint, and each state
dimension is verified to actually change the fingerprint when it changes.

## Evidence merging

`evidence_merger.py` provides `combine_evidence`/`merge_confidence`/
`merge_duplicate_elements`, polymorphic over both evidence shapes in this codebase:
the rich `EvidenceReference`/`ConfidenceScore` objects (native to the new
perception-only descriptors) and the plain `list[str]`/`float` used on the
schemas.py-extended `InteractiveElement`/`FormDescriptor`/`TableDescriptor` (kept
simple there specifically to avoid a dependency from `app/schemas.py` onto
`app/perception`). `interactive_detector` uses it to collapse an element matched by
multiple detection rules (e.g. a `<button role="button">` matching both `native_tag`
and `aria_role`) into one `InteractiveElement` with combined evidence rather than two
disconnected records; `engine.py` uses it to merge landmark/structural/card/utility
regions that happen to share a `stable_id`.

## Unknown-component detection

`unknown_detector.detect_unknown_components()` is handed every extractor's claimed
`element_id`/`stable_id` set (`PerceptionEngine._claimed_ids`) and flags anything
visible in `RawObservation.elements` that nothing else classified — with a stated
`detection_reason` (e.g. `"tabindex=-1"`, `"cursor:pointer"`). Nothing that made it
into the raw pass at all is ever silently dropped.

## Integration into the observation loop

`app/agent/controller.py`'s single `_observe()` closure (the one and only place
observation happens, called at run start and after every action) now also calls
`PerceptionEngine.observe()`/`.observe_adapter()` — best-effort, wrapped in a
try/except that logs and continues with `PageState` alone on failure, never
propagating. Because every trigger the task named (initial load, navigation, modal
open, tab change, dropdown/accordion expansion, form submission, any meaningful
state change) is just some action followed by this same post-action observe, no
separate wiring per trigger type was needed — one integration point covers all of
them. Result is stored on `RunMemory.canonical_page_model` (new field, `Any`-typed to
avoid a memory.py -> perception dependency) for availability without changing what
`Planner`/`FrontierBuilder` read.

### BrowserAdapter boundary

The direct-Playwright path (`adapter.native_page`) gets the full, rich JS extraction.
Any other adapter (MCP) degrades to the already-existing, already-tested
`CanonicalPageModel.from_page_state()` adapter over whatever generic `PageState` that
adapter can already produce — this respects the adapter boundary rather than reaching
into internals an MCP-style adapter doesn't expose.

## Guardrails honored

- **Deterministic** — no LLM call anywhere in this module; every classification is a
  fixed rule over the raw observation. Unit-tested directly
  (`test_engine_is_deterministic_for_identical_raw_input`).
- **No raw full-page HTML sent anywhere** — the JS pass returns only structured
  facts (attributes, computed styles, bounding boxes), never `outerHTML`/`innerHTML`.
- **No browser actions from this layer** — every extractor is read-only; `engine.py`
  never calls anything on `ActionExecutor`/`BrowserAdapter` beyond observation.
- **No business-specific vocabulary** — `region_type`, `dialog_type`, `block_type`,
  etc. are all structural; verified by
  `test_engine_never_produces_business_specific_region_names`.

## A real bug this build surfaced and fixed

Live-testing against a real agent run (not just hand-built fixtures) surfaced two real
bugs, both fixed and regression-tested:

1. **Relative URLs misclassified as external.** `same_origin()`/`origin_of()` only
   understand absolute URLs — an unresolved relative `href="/cart"` compared against
   the page's origin as `""` vs. the real origin, always losing. Fixed by resolving
   every href via `urljoin(page_url, href)` before the origin comparison, in all three
   places link locality is computed (`dom_extractor`, `navigation_extractor`,
   `CanonicalPageModel.from_page_state`).
2. **Overly strict stable_id uniqueness.** The original validator rejected the same
   `stable_id` appearing in two different collections — but a clickable
   `<h4><a>...</a></h4>` is legitimately both a `HeadingDescriptor` and a
   `LinkDescriptor` sharing one DOM identity. Fixed to check uniqueness only *within*
   each collection, not across all of them.

## Tests

`tests/test_perception_engine.py` — 77 tests covering every module (extraction
correctness, controlled-vocabulary classification, determinism, the relative-URL
fix, duplicate-signal collapsing, row-action linkage, tab-selection state,
fingerprint sensitivity to each new state dimension) plus end-to-end
`PerceptionEngine` pipeline tests. Combined with the existing 393
`test_canonical_page_model.py` + prior suite, full repository suite: **470 passed, 1
skipped, 0 failed.**

Live-verified against `https://www.saucedemo.com/` both standalone (direct Playwright)
and through a full real agent run via the API — zero perception failures, correct
region/heading/link/interactive-element extraction, and structured trace events
emitted on every observation.

## Selective visual observation (follow-up)

A separate, optional visual layer was added on top of this engine —
see [`VISUAL_OBSERVATION_POLICY.md`](./VISUAL_OBSERVATION_POLICY.md). It does
not change anything described above: the deterministic pipeline this document
describes is unmodified and remains fully deterministic on its own.

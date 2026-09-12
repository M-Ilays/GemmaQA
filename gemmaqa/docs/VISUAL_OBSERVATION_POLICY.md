# Visual Observation Policy

Extends the [Universal Page Perception Engine](./PERCEPTION_ENGINE.md) with a
**selective** visual layer: a bounded, policy-gated call to a multimodal model
that adds `VisualEvidence` to the [Canonical Page Model](./CANONICAL_PAGE_MODEL.md)
when — and only when — deterministic DOM/accessibility extraction alone left
something ambiguous.

**Status:** implemented, wired into `PerceptionEngine`/`app/agent/controller.py`,
off by default (see [Wiring](#wiring--defaults) below).

## Core rule: screenshot analysis is never mandatory

The deterministic pipeline (`dom_extractor` → `accessibility_extractor` →
`interactive_detector` → ... → `CanonicalPageModel`) is completely unchanged
and remains 100% deterministic — every existing perception test still passes
unmodified. The visual layer is a separate, additive step that:

1. Only runs if a `visual_provider` (a `GemmaProvider`) was configured on the
   `PerceptionEngine` at all.
2. Even then, only calls the model if `VisualObservationPolicy.evaluate()`
   found at least one of 8 specific trigger conditions.
3. Never replaces or overwrites deterministic evidence — it is always merged
   alongside it (see [Confidence merging](#confidence-merging)).

A page with no ambiguity produces `CanonicalPageModel.visual_evidence == []`
and zero model calls, exactly like today.

## Architecture

```
app/perception/
    visual_policy.py      VisualObservationPolicy — decides IF/WHAT (no model call)
    screenshot_utils.py    crop_bounding_box() — local Pillow crop, no model call
    visual_analyzer.py     VisualAnalyzer — assembles evidence, ONE model call
    image_extractor.py     + classify_image_semantic() — heuristic image typing
    evidence_merger.py     + merge_visual_evidence() — confidence/evidence merge
    engine.py              + PerceptionEngine._maybe_apply_visual()
app/gemma/
    prompts.py             + VISUAL_ANALYSIS_SYSTEM_PROMPT / build_visual_analysis_prompt
    parser.py               + parse_visual_analysis() — validates against known ids
    base.py                + GemmaProvider.analyze_visual_elements()
```

## Pipeline

For every observation where a `visual_provider` is configured:

1. **Image typing (heuristic, no model call).** `image_extractor.classify_image_semantic()`
   classifies every extracted `<img>` into exactly one of the 8 required
   categories — `decorative | informational | interactive | document | chart |
   avatar | logo | unknown` — from DOM facts alone (alt text, `src`, class
   name, container, bounding box). This happens for **every** image, on every
   page, always (it's cheap and deterministic); it feeds trigger condition 5
   below and decides which images are even eligible for deeper analysis.
2. **Policy evaluation (no model call).** `VisualObservationPolicy.evaluate()`
   inspects the already-built `RawObservation` + the deterministic
   `interactive_elements` / `images` / `unknown_components` and returns a
   `VisualObservationDecision(should_run, reasons, targets, needs_full_page)`.
3. **If `should_run` is `False`, stop here.** Nothing else runs.
4. **Cropping (local, no model call).** For each target with a bounding box,
   `screenshot_utils.crop_bounding_box()` crops that region out of the
   already-captured full-page screenshot (12px padding), saved next to it
   under a `crops/` subdirectory. A crop failure (missing Pillow, corrupt
   image, zero-size box) degrades to using the full screenshot for that
   target — never fatal.
5. **One bounded model call.** `VisualAnalyzer.analyze()` assembles, per
   target: the crop (or full screenshot), existing DOM evidence, existing
   accessibility evidence, and nearby text — then calls
   `GemmaProvider.analyze_visual_elements()` **once** for up to
   `MAX_TARGETS_PER_CALL` (12) targets, never one call per element.
6. **Structured, validated parsing.** `parse_visual_analysis()` accepts only
   `{"observations": [...]}`; any `element_id` not in the exact set of
   targets sent in *this* call is dropped and logged, never trusted.
   Confidence is clamped to `[0, 1]`; an unrecognized `visual_semantic_type`
   is coerced to `"unknown"` rather than rejected outright.
7. **Merge.** `evidence_merger.merge_visual_evidence()` attaches each
   `VisualEvidence` to `CanonicalPageModel.visual_evidence`, and — for
   whichever interactive element / image / unknown component shares its
   `element_id` — merges confidence and appends an evidence note (see below).
8. **Trace.** A `perception.visual_decision` trace event records
   `should_run`, `reasons`, `target_count`, `needs_full_page` for every
   observation (even when `should_run` is `False`), and
   `perception.observation`'s `element_counts` now includes
   `visual_evidence`.

Any exception anywhere in steps 4–7 is caught in `PerceptionEngine._maybe_apply_visual()`
and logged; the deterministic model is returned unchanged. A visual-analysis
failure can never break the observation loop.

## The 8 trigger conditions

`VisualObservationPolicy.evaluate()` (`app/perception/visual_policy.py`) checks all
of the following; any one firing sets `should_run = True` and contributes its
own targets:

| # | Condition | Signal used | Targets |
|---|---|---|---|
| 1 | Important visible controls have no accessible name | `InteractiveElement.is_visible` with no `accessible_name`/`visible_text`/`text`/`aria_label` | the element |
| 2 | DOM contains icon-only controls | `RawObservation.elements[].looks_icon_only` (existing signal from `dom_extractor`) | the element |
| 3 | Canvas elements are present | `RawObservation.canvases` (new raw bucket — a `<canvas>`'s pixel content is opaque by construction) | the canvas |
| 4 | SVG controls cannot be semantically classified | `has_inline_svg` (new raw flag) with no `role` and no accessible name/text | the element |
| 5 | An image/scanned document appears informational | `ImageDescriptor.visual_semantic_type` in `{informational, document, chart, interactive, unknown}` (i.e. not `decorative`/`avatar`/`logo` — see step 1 above) | the image |
| 6 | Visual layout or grouping is needed | `len(unknown_components) >= 3` (a large unclassified residue suggests page-level ambiguity, not one element) | none (sets `needs_full_page`) |
| 7 | A possible visual defect is detected | two distinct visible interactive elements whose bounding boxes overlap with IoU ≥ 0.6 (ordinary DOM nesting — an icon inside a button — has LOW IoU since the child is much smaller than the parent, so this does not fire on normal containment) | both elements (sets `needs_full_page`) |
| 8 | Unknown interactive components remain after DOM+accessibility analysis | `unknown_detector`'s existing `UnknownComponent` list | each unknown component |

Multiple conditions firing on the *same* element merge into one `VisualTarget`
with multiple `reasons` — never duplicate targets.

## Image classification (deterministic, first pass)

`image_extractor.classify_image_semantic()` — order matters, most specific
signal first:

1. `alt`/`src`/class containing `logo` → **logo**
2. `avatar`/`profile`/`headshot`/`user-photo` hint + small (≤160px) + roughly
   square → **avatar**
3. Explicitly empty `alt=""` (the standard HTML decorative signal — **not**
   the same as a *missing* `alt`, which is just unlabeled) or `role="presentation"/"none"` → **decorative**
4. `chart`/`graph`/`plot`/`sparkline` hint (alt, class, or surrounding text) → **chart**
5. `.pdf` source, or `document`/`scan`/`receipt`/`contract`/`statement` hint → **document**
6. Wrapped in a link/button (`in_interactive_container`) → **interactive**
7. Tiny (≤32px) with no other signal → **unknown** (almost certainly a glyph, not content)
8. A descriptive alt (≥2 words) or substantial surrounding text (≥40 chars) → **informational**
9. Otherwise → **unknown**

Only `informational | document | chart | interactive | unknown` are ever
escalated to the visual model (`image_extractor.MEANINGFUL_IMAGE_TYPES`) —
`decorative`, `avatar`, and `logo` already carry enough evidence on their own.

## Schemas added (`app/perception/models.py`)

- **`IMAGE_SEMANTIC_TYPES`** — exactly the 8 required categories.
- **`VISUAL_ELEMENT_SEMANTIC_TYPES`** — superset for `VisualEvidence`, adding
  `icon_button`, `canvas_widget`, `layout_group`, `possible_defect` for
  non-image targets (a bare SVG icon, a canvas, an ambiguous layout region, a
  suspected rendering defect).
- **`ImageDescriptor.visual_semantic_type`** — the heuristic classification above.
- **`VisualEvidence`** — one validated visual-model observation about one
  existing `element_id`: `visual_semantic_type`, `description`, `confidence`
  (`ConfidenceScore`), `evidence` (`list[str]`), `further_interaction_recommended`,
  `trigger_reasons`, `screenshot_reference`, `source="visual"`. No selector
  field exists on this type — it cannot carry an action by construction.
- **`CanonicalPageModel.visual_evidence: list[VisualEvidence]`** — always
  empty unless the policy triggered.
- `SOURCE_KINDS` gains `"visual"` (additive; existing values unchanged).

## Confidence merging

`evidence_merger.merge_visual_evidence(model, visual_evidence)`:

- Every `VisualEvidence` is appended to `model.visual_evidence` verbatim —
  nothing is ever silently dropped, even if no other descriptor recognizes
  its `element_id`.
- For whichever `InteractiveElement` / `ImageDescriptor` / `UnknownComponent`
  shares that `element_id`, confidence is **merged**, never overwritten,
  preserving whichever shape the field already uses:
  - `InteractiveElement.confidence` is a plain `float` (schemas.py) →
    `max(existing, visual_value)`.
  - `ImageDescriptor`/`UnknownComponent.confidence` is a `ConfidenceScore`
    (perception-native) → `evidence_merger.merge_confidence()`, which keeps
    the higher-confidence value's basis and notes that basis the other
    signal too.
- A short evidence note (`"visual:<visual_semantic_type>"` for plain-list
  fields, or an `EvidenceReference(kind="screenshot", ...)` for rich fields)
  is appended alongside whatever DOM/accessibility evidence the element
  already had — never replacing it.

## What the visual model receives / returns

**Input** (`build_visual_analysis_prompt` in `app/gemma/prompts.py`):
screenshot or crop path(s), the list of target `element_id`s with their
`bounding_box` and trigger `reasons`, existing DOM evidence, existing
accessibility evidence, and nearby text — keyed by `element_id`, never raw
full-page HTML.

**Output** — strict JSON only, one entry per target:

```json
{
  "observations": [
    {
      "element_id": "el_004",
      "visual_semantic_type": "icon_button",
      "description": "A trash-can icon button in the table row's action column",
      "confidence": 0.8,
      "evidence": ["visible trash-can glyph", "positioned in the actions column"],
      "further_interaction_recommended": true
    }
  ]
}
```

## Guardrails honored

- **No selectors, no clicks, no actions.** `VisualEvidence` has no selector
  field; `VISUAL_ANALYSIS_SYSTEM_PROMPT` explicitly forbids selectors/actions;
  `VisualAnalyzer`/`PerceptionEngine` never call `ActionExecutor`/`BrowserAdapter`.
- **Never overrides safety policy.** The visual layer runs entirely inside
  the perception/observation step, before `Planner`/`SafetyPolicy` ever see
  anything; it cannot bypass action validation because it never proposes an
  action.
- **Never replaces deterministic extraction.** `observe()`/`observe_adapter()`
  build the full deterministic `CanonicalPageModel` first; the visual step
  only adds to it afterward, and is skipped entirely with zero behavior
  change when no `visual_provider` is configured.
- **Never invents page content without evidence.** `parse_visual_analysis()`
  drops any `element_id` outside the exact set of targets sent in that call;
  `known_element_ids` in `VisualAnalyzer.analyze()` is computed from the
  *actual* (post-cap) targets sent, not a broader "everything on the page"
  set, so the model cannot reference an element it was never shown evidence
  for.
- **Screenshot analysis is never mandatory.** See [Core rule](#core-rule-screenshot-analysis-is-never-mandatory) above.

## Wiring & defaults

`app/agent/controller.py` constructs
`PerceptionEngine(visual_provider=self.gemma if self.settings.effective_gemma_supports_images else None)`.
`effective_gemma_supports_images` defaults to `False`, so **by default nothing
changes**: the visual layer is only ever wired in when a multimodal-capable
provider/model is explicitly configured, and even then every individual call
is still gated by the policy. This reuses the *same* `GemmaProvider` already
used for action selection — no separate model stack.

## Tests

`tests/test_visual_observation.py` (47 tests):

- **Image classification** — all 8 categories, plus the `extract_images()`
  integration.
- **`VisualObservationPolicy`** — each of the 8 trigger conditions
  individually (including the "does NOT trigger" negative cases —
  decorative images, native-tag SVG controls, nested icon-in-button not
  flagged as a defect), target/reason deduplication, and the `_bbox_iou`
  helper directly (identical / disjoint / nested boxes).
- **`screenshot_utils.crop_bounding_box`** — real Pillow crop + saved-size
  assertion, missing file, zero-size box, and risky-filename skip.
- **`parse_visual_analysis`** — valid parse, invented `element_id` dropped,
  invalid `visual_semantic_type` coerced to `unknown`, confidence clamped,
  malformed JSON / missing key degrade to `[]`.
- **`VisualAnalyzer`** — mocked-provider (`MockGemmaProvider`) end-to-end
  call, skip-when-policy-says-no (provider never called), and the
  per-call target cap.
- **`evidence_merger.merge_visual_evidence`** — confidence-shape preservation
  for both the plain-`float` (`InteractiveElement`) and `ConfidenceScore`
  (`ImageDescriptor`) cases, evidence appended alongside existing evidence,
  and unmatched-`element_id` evidence still recorded (never silently
  dropped).
- **`PerceptionEngine` wiring** — visual disabled by default (no
  `visual_provider` → `visual_evidence == []`), visual runs and merges when
  configured and the policy triggers, and a visual-analysis failure is
  non-fatal (`RuntimeError` from the provider degrades to the deterministic
  model, no exception propagates).
- **One live Playwright page** (`TestLivePageVisualPolicy`, real Chromium via
  `playwright.async_api`, route-intercepted so no real network call is
  made): a page with an icon-only `<button><svg>...</svg></button>`, an
  `<img>` with a descriptive alt (classified `informational`/`chart`), a
  `<canvas>`, and a poorly-labelled pointer-cursor custom `<div>` control —
  verifies the policy fires end-to-end through `PerceptionEngine.observe()`
  against a real browser page, not just hand-built fixtures. Skips
  gracefully if Chromium isn't available in the environment.

Combined with the existing suite: **517 passed, 1 skipped, 0 failed** (full
`pytest tests/`).

Pillow was added as a new dependency (`backend/requirements.txt`) — it was
already used optionally by `app.gemma.images` but was not actually installed
in this environment; installing it lets cropping (a task requirement) be
genuinely exercised rather than always degrading to the full screenshot.

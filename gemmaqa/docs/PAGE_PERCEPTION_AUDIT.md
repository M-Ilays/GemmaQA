# Page Perception Audit

**Scope:** How GemmaQA observes and represents an opened browser page — traced end to end from `BrowserAdapter`/Playwright through the observer, planner, frontier, application store, memory, coverage, and reporting layers. Audit only; no production code was modified.

**Verification performed:**
- Full existing test suite: `342 passed, 1 skipped, 0 failed`.
- Live run against `https://www.saucedemo.com/` (`run_id=9c0b7202-7728-46d1-ba30-b2e9634777bd`), inspecting the raw stored `PageState` JSON directly from the database for `inventory.html` and `cart.html`.

---

## 1. Current observation architecture

```
Playwright Page
   │  page.evaluate(OBSERVE_SCRIPT)          — single in-page JS extraction
   ▼
PageObserver.observe() / observe_adapter()    app/browser/observer.py
   │  + ConsoleMonitor.snapshot()             app/browser/console_monitor.py
   │  + NetworkMonitor.snapshot()             app/browser/network_monitor.py
   │  + fingerprint_page_state()              app/browser/fingerprint.py
   ▼
PageState (pydantic)                          app/schemas.py
   │
   ├──► FrontierBuilder.build()               app/agent/frontier.py   (candidate generation)
   ├──► Planner.next_action()                 app/agent/planner.py    (LLM prompt + dispatch)
   ├──► RunMemory.remember_page()             app/agent/memory.py     (ephemeral run-scoped state)
   │         │
   │         ▼
   │    ApplicationStore.observe_page()       app/application/store.py (canonical cross-run model)
   │         │
   │         ▼
   │    compute_coverage()                    app/application/coverage.py
   │         │
   │         ▼
   │    ReportBuilder                         app/reporting/report_builder.py
   ▼
Executor/Explorer/Documenter consume PageState directly for classification, evidence, and docs.
```

Everything funnels through **one** extraction point: a single `page.evaluate()` call (`OBSERVE_SCRIPT`, `app/browser/observer.py:35-321`) that returns one JSON blob, normalized into a `PageState` in `PageObserver.build_page_state()`. There is no second, competing extraction path — that part of the architecture is clean. The weaknesses are entirely in *what* that one script looks for and *how much* of what it finds survives into the canonical model.

---

## 2. Exact files, classes and functions involved

| Layer | File | Key symbols |
|---|---|---|
| Extraction | `app/browser/observer.py` | `OBSERVE_SCRIPT`, `normalize_element()`, `PageObserver.observe/observe_adapter/build_page_state` |
| Locators | `app/browser/locators.py` | `choose_locator_strategy()`, `LocatorRegistry` |
| Fingerprint | `app/browser/fingerprint.py` | `compute_fingerprint()`, `element_signature()`, `fingerprint_page_state()` |
| Console | `app/browser/console_monitor.py` | `ConsoleMonitor` |
| Network | `app/browser/network_monitor.py` | `NetworkMonitor`, `sanitize_network_url()` |
| Evidence | `app/browser/evidence.py` | screenshot capture, linked via `AppPage.screenshot_evidence_id` |
| Schema | `app/schemas.py:228-341` | `InteractiveElement`, `FormField`, `FormDescriptor`, `TableDescriptor`, `PageState`, `ConsoleEntry`, `NetworkEntry` |
| Frontier | `app/agent/frontier.py` | `FrontierBuilder.build()`, `_build_navigation_candidates()`, `infer_semantic_operation()` |
| Planner | `app/agent/planner.py` | `Planner.next_action()`, `_build_full_frontier()` |
| Memory | `app/agent/memory.py` | `RunMemory.remember_page()` (~line 168) |
| Application model | `app/application/store.py` | `ApplicationStore.observe_page()`, `ensure_module()`, `upsert_form()` |
| Canonical schema | `app/application/models.py` | `AppPage`, `AppForm`, `AppFormField`, `AppModule` — **no `AppTable`** |
| Classification | `app/agent/explorer.py` | `Explorer.classify()`, `module_name_from_url()` |
| Purpose inference | `app/application/purpose.py` | `infer_application_purpose()` |
| Coverage | `app/application/coverage.py` | `compute_coverage()`, `_build_dimensions()` |
| Reporting | `app/reporting/report_builder.py` | `ReportBuilder` |

---

## 3. Per-capability classification

Legend: **1** Fully implemented and used · **2** Implemented but incomplete · **3** Implemented but disconnected · **4** Placeholder only · **5** Missing

| Capability | Class | Evidence |
|---|---|---|
| Page title and URL | **1** | `document.title`, `page.url` → `PageState.title/url`. Read by `Explorer`, `ApplicationStore`, `FrontierBuilder`. Live-verified (`title: "Swag Labs"`). |
| Visible text | **2** | `document.body.innerText`, capped at 2500 chars in-page then re-truncated to `settings.observe_text_max_chars` (default 800) in `build_page_state`. Fed to the LLM prompt only (`gemma/context_sanitizer.py`); **not read anywhere in `FrontierBuilder`** — grep of `frontier.py` for `page.` attributes shows no `visible_text_summary` access. |
| Headings and descriptions | **2** | Only `h1,h2,h3` collected (`observer.py:237-238`), capped 20. **No meta description, no `<p>` summary.** Live-verified: SauceDemo's `inventory.html` and `cart.html` both return `headings: []` — the app's "Products" title is a styled `<span>`, not a semantic heading, so this real target yields nothing. Only the *first* heading is even kept downstream (`AppPage.heading`, `store.py:175`). |
| Links and destination URLs | **1** | Bare `a` selector (not `a[href]`) plus `href` attribute captured; reaches `FrontierBuilder` (`navigation_control`/`open_url` candidates) and `RunMemory.remember_page()`'s `unexplored_urls` walk. |
| Buttons | **1** | `categoryOf()` tags tag/role/type combinations as `'button'`; used throughout frontier priority scoring. |
| Forms and form fields | **2** | `<form>` + descendant `input/select/textarea/button` captured (`observer.py:174-217`), capped 10 forms. Reaches `FrontierBuilder` (`inspect_form`, `start_form_workflow`) and `ApplicationStore.upsert_form()`. **Field-level `checked` state for checkboxes/radios inside a form is never captured** (the `fields.push(...)` object at `observer.py:197-207` has no `checked` key, unlike the top-level `elements` array) — a checkbox inside a form is only fully described via its duplicate entry in `interactive_elements`, correlated by `element_id`, not by the form representation itself. |
| Field labels, types, required, disabled | **2** | `label`, `field_type`, `required`, `disabled` captured on the ephemeral `FormField` (`schemas.py:258-267`). But the **canonical** `AppFormField` (`application/models.py:78-85`) drops `disabled`, `options`, and `current_value` entirely — persisted form-field state loses disabled/options/value across the run's cross-page model. |
| Tables, columns, rows, row actions | **2 / 3** | `headers`, `row_count`, and only the **first 3 sample rows** captured as raw strings (`observer.py:219-235`). **Row actions are never linked to their row** — no per-row action-button association exists anywhere. **No `AppTable` model exists** (confirmed via `grep '^class App' application/models.py`) — tables are tracked only in ephemeral `RunMemory.tables`/`known_table_ids`, never in the canonical `ApplicationModel`, so they vanish once a run ends and don't feed purpose/coverage the way forms do. |
| Tabs and selected tabs | **2 / 3** | Tab elements are captured twice, inconsistently: (a) a flat `tabs: list[str]` of labels (`observer.py:248-249`), which only feeds `RunMemory.known_tabs` (`memory.py:303-306`) for novelty tracking; (b) individual `interactive_elements` with `category=='tab'`, which is what `FrontierBuilder` actually scores (`frontier.py:734-737`, checked against `memory.known_tabs` by label-text match, not element identity). **No `aria-selected` is ever read** — there is no way to know which tab is currently active, so the frontier cannot distinguish "already on this tab" from "never opened this tab" except via the same known-tabs label heuristic. |
| Navigation landmarks and navigation items | **2 / 3** | `navigation_items` = flat text of `nav a, [role="navigation"] a, header a` (`observer.py:244-246`), no href, no landmark-type distinction. Live-verified populated (`['All Items', 'About', 'Logout', 'Reset App State']`). **Never read by `FrontierBuilder`** (absent from its attribute grep) — candidates for these same links are instead independently regenerated from `interactive_elements`, making `navigation_items` effectively a write-only, cosmetic field consumed only by `purpose.py`'s keyword blob. |
| Header, sidebar, footer, main-content regions | **5** | No `<header>`, `<footer>`, `<aside>`, `<main>`, or `role="banner"/"contentinfo"/"complementary"/"main"` queries exist anywhere in `OBSERVE_SCRIPT`. `header a` is used only to *harvest link text* for `navigation_items`, not to represent the header as a region. No sidebar/footer/main concept exists in `PageState` at all. |
| Dropdowns, accordions, expandable controls | **2** | `select`/`combobox`/`listbox` map to category `'select'`; `summary` (native `<details>` disclosure) is in the interactive selector. But **`aria-expanded` is never read** anywhere in `OBSERVE_SCRIPT` — an accordion's open/closed state is invisible; category `'select'` elements expose `available_options` but a custom (non-`<select>`) dropdown built from `div`/`ul` has no signal at all unless it happens to carry `role="listbox"`/`combobox`. |
| Dialogs, modals and alerts | **2** | Flat text lists only (`dialogs`, `modals`, `toasts`, `alerts` — `observer.py:251-273`), capped 8-10 each, filtered by a JS `isVisible()` check. Elements *inside* a dialog are not linked to it — they appear a second time, independently, in the flat `interactive_elements` list. `RunMemory.remember_page()` folds `modals`/`dialogs` into `known_modals` for novelty; `FrontierBuilder` only checks `page.dialogs or page.modals` as a boolean gate to boost "open/view/details" buttons (`frontier.py:738-741`), never their content. |
| Images, alt text and surrounding text | **3 / 5** | `<img alt>` is read **only** as a fallback inside `childImageAlt()` for computing one icon-only control's accessible name (`observer.py:69-78`) — there is no general image inventory, no "list of images + alt + surrounding text" data point on `PageState` at all. |
| SVG and icon-only controls | **2** | Icon-only controls fall through the `accessibleName()` chain (`aria-label → label → title → placeholder → text → child-img-alt → name`); if none apply (a bare inline `<svg>` with no `aria-label`), `accessible_name` resolves to `''`. No SVG-specific extraction (no `<title>`/`<desc>` inside SVG read). |
| ARIA roles and accessible names | **2** | `role` attribute captured verbatim; `accessible_name` is a reasonable but partial approximation of the real accessible-name algorithm (no `aria-describedby`, no computation for elements whose role is implicit-only beyond the fixed `categoryOf()` tag/type map). |
| Expanded, selected, checked, disabled states | **2 / 5** | `disabled`/`is_enabled` fully captured (native `disabled` + `aria-disabled`). `checked` captured **only** for native `type=checkbox`/`type=radio` (`observer.py:164`) — `aria-checked` (custom switches/toggles) is never read. **`aria-expanded` and `aria-selected` are read nowhere in `OBSERVE_SCRIPT`** — confirmed by a full-text search of the extraction script; these three words never appear. |
| Bounding boxes and element positions | **2** | Captured per element (`getBoundingClientRect()` → `bounding_box`), stored on `InteractiveElement.bounding_box`. **Never read downstream** — no reference to `bounding_box` in `frontier.py`, `planner.py`, or `application/store.py` (only used for the in-script `isVisible()` width/height!=0 check, discarded from any post-extraction logic). |
| Non-standard clickable elements | **2 / 5** | `[onclick]` (inline attribute) and a generic `'a'` catch-all are included, which covers React-Router-style anchors and legacy inline handlers. **A `<div>`/`<span>` made clickable via `addEventListener` with no `role="button"`/`onclick` attribute is invisible** — this is the common modern-SPA "fake button" pattern and is not detected by any selector or fallback bucket. There is no generic "unknown clickable" category at all; anything outside `interactiveSelector` simply never enters `elements`. |
| Screenshots | **1** | Captured via `app/browser/evidence.py`, linked canonically through `AppPage.screenshot_evidence_id` and `PageState.screenshot_path`; used by the LLM (multimodal prompt), the report, and the UI. |
| Network requests and responses | **2** | `NetworkMonitor` only records **failed** requests (status ≥ 400 or `requestfailed`) — confirmed in `network_monitor.py:80-119`; no successful request/response is ever recorded, and no response **body** is captured, only method/url/status/timing. Live-verified: a real 401 to a Backtrace analytics beacon was captured correctly. |
| Page-state fingerprints | **1** | `compute_fingerprint()` hashes url + title + headings + sorted visible-control signatures + modals + dialogs + a text snippet, with number/counter normalization to avoid badge-driven false fingerprint changes. Solid and actively used for dedup/loop-detection throughout `RunMemory`. |
| Unknown interactive components | **5** | No fallback/"unknown" bucket exists. An element that doesn't match `interactiveSelector` is not represented in any form — not as a stub, not as a count, not as a coverage "unknown" line item (distinct from the `CoverageDimension.unknown` field added in a prior phase, which counts unvisited *pages/candidates*, not unrecognized *elements*). |

---

## 4. What data is currently extracted

Everything listed in `OBSERVE_SCRIPT`'s return object (`observer.py:300-319`): title, headings (h1-3 only), breadcrumbs, navigation_items, visible_text (2500-char cap), interactive_elements (≤120, with tag/role/type/name/id/testid/text/aria_label/accessible_name/label/href/placeholder/visibility/enabled/required/disabled/checked/current_value/available_options/category/bounding_box), forms (≤10, with per-field name/type/label/required/placeholder/options/disabled/value), tables (≤8, headers + row_count + 3 sample rows), tabs (label strings), dialogs/modals/toasts/alerts (label strings), pagination_controls, search_fields, filter_controls, disabled_controls, required_fields.

Plus, outside the script: console entries/errors, network entries/failures, and the screenshot path.

## 5. What data reaches FrontierBuilder

Only: `dialogs`, `forms`, `headings`, `interactive_elements`, `modals`, `page_id`, `state_fingerprint`, `tables`, `url` (confirmed by exhaustive attribute grep of `frontier.py`). Everything else extracted — `title`, `visible_text_summary`, `breadcrumbs`, `navigation_items`, `tabs`, `toasts`, `alerts`, `pagination_controls`, `search_fields`, `filter_controls`, `disabled_controls`, `required_fields`, `console_*`, `network_*`, `screenshot_path`, `classification` — never reaches candidate generation directly.

## 6. What data reaches Planner

`forms`, `headings`, `interactive_elements`, `screenshot_path` (for the multimodal prompt), `state_fingerprint`, `url`. `Planner` otherwise delegates to `FrontierBuilder` for everything else.

## 7. What is stored in ApplicationStore

Per page (`AppPage`): `canonical_url`, `normalized_path`, `title`, **first heading only**, `page_type`, `module_id`, `fingerprint`, `visit_count`, `exploration_status`, `screenshot_evidence_id`. Per form (`AppForm`/`AppFormField`): `form_name`, `method`, `action`, `lifecycle_state`, and per-field `label`/`input_type`/`required` — **not** `disabled`, `options`, or `current_value`. Modules (`AppModule`) with `parent_module_id` hierarchy. Nav edges (`AppNavigationEdge`). **Nothing for tables** — no canonical table model exists at all. Nothing for tabs, dialogs, breadcrumbs, navigation_items, images, ARIA states, or bounding boxes.

## 8. What is discarded

Bounding boxes (captured, never read again after the in-script visibility check). Full visible text (truncated twice, never reaches the frontier). All headings beyond the first (dropped at the `ApplicationStore` boundary). Form field `disabled`/`options`/`current_value` (dropped at the same boundary). Table content entirely (never leaves `RunMemory`). Successful network requests (network monitor only records failures). `bounding_box`/`title` on `InteractiveElement` (schema field exists, never populated by the observer — see below).

## 9. Existing hardcoded application-specific assumptions

- `app/application/purpose.py:52-116` — `infer_application_purpose()` still returns the literal string *"A contact management application that allows users to register, authenticate, and manage personal contacts."* from **four separate branches**, gated on `"contact"` keyword matches in titles/paths/host. This was flagged in the Phase 1 audit but only the `canonical_module_key()` special-case in `store.py` was removed at the time — this file was not touched and the hardcoding is still fully live.
- `app/agent/explorer.py:74-79` (`module_name_from_url`) — still hardcodes `addcontact`/`add-contact`/`contactlist`/`contact-list`/`contacts` → `"Contacts"`, and `adduser`/`signup`/`register` → `"Sign Up"`. This feeds `page_state.classification.module_guess`, which flows straight into `ApplicationStore.ensure_module()` (`store.py:183`), so the Contact-List-specific naming still reaches the canonical module system through this path even though the *other* hardcoded copy was removed earlier.
- `app/agent/frontier.py:26-44` (`MODULE_HINTS`) — mixes generic CRM-shaped nouns (`customers`, `orders`, `products`, `users`, `projects`) with app-specific vocabulary (`contact`, `contacts`, `add contact`) in one shared priority-boost list.

## 10. Existing generic UI heuristics

Word-boundary hint matching (`_word_hint_match`) to avoid substring false positives (e.g. "Backpack" ≠ "Back"). Accessible-name fallback chain (`aria-label → label → title → placeholder → text → child-img-alt → name`). Category inference from tag/role/type (`categoryOf`). Locator-strategy priority chain (`testid → id → role+name → label → placeholder → link/button text → CSS → nth`). Page-state fingerprinting with counter/badge normalization. URL-hierarchy-based module grouping (`canonical_module_key`, `infer_module_hierarchy`).

## 11. Runtime weaknesses (live-verified where noted)

1. **Heading-dependent classification loses on real apps.** SauceDemo — a widely-used QA target — has zero `h1/h2/h3` anywhere; both pages checked live returned `headings: []`. Any downstream logic that leans on headings (purpose inference, `AppPage.heading`) gets nothing from this app.
2. **Tabs are tracked through two uncorrelated paths** (flat label list vs. individual elements), joined only by string-matching label text, with no selected/active state at all.
3. **No table persistence** means table-heavy modules (a common enterprise-app pattern) leave literally no canonical trace once a run ends, unlike every other structural feature.
4. **Non-semantic clickable elements (`<div onClick>` with no ARIA role) are invisible.** This is a plausible root cause for a previously-observed live gap (a "Jobs" list in the ServiceFlow app producing no reachable per-job detail candidates).
5. **Form field disabled/options/value state doesn't survive into the canonical model**, so any report or downstream reasoning built from `ApplicationStore` alone (not the ephemeral per-run `PageState`) is working from a strictly poorer picture of each form than what was actually observed.
6. **Bounding boxes are extracted and then never used** — dead weight in every page payload with no current downstream consumer, and a missed opportunity for layout-based region inference (header/sidebar/footer) that nothing else currently attempts at all.
7. **Two still-live hardcoded Contact-List special cases** (§9) mean purpose/module inference is not actually generic yet, despite that being an explicit goal of prior work on this codebase.

## 12. Recommended integration points for a Universal Page Perception Engine

1. **Replace the single monolithic `OBSERVE_SCRIPT` with a layered extraction pipeline** inside `app/browser/observer.py`, adding: a landmark pass (`header/nav/main/aside/footer` + matching ARIA roles, producing a `regions` structure), an ARIA-state pass (`aria-expanded`, `aria-selected`, `aria-checked`, `aria-current`), and an "unknown clickable" fallback bucket (any element with a non-empty computed cursor/pointer style or an attached click listener detectable via a lightweight heuristic, tagged `category="unknown"` rather than silently dropped).
2. **Extend `InteractiveElement`/`PageState` in `app/schemas.py`** with `aria_expanded`, `aria_selected`, `aria_checked`, `region` (which landmark it lives in), and populate the already-existing but dead `title`/`bounding_box` fields consistently.
3. **Add an `AppTable` model to `app/application/models.py`** mirroring `AppForm`, wired through `ApplicationStore.upsert_table()`, so tables get the same cross-run persistence, lifecycle tracking, and gap-detection treatment forms already receive in `app/application/gaps.py`.
4. **Fix the two remaining hardcoded-purpose sites** (`purpose.py`, `explorer.py::module_name_from_url`) using the same evidence-based, no-hardcoded-name approach already applied to `canonical_module_key()`.
5. **Feed `navigation_items`, `breadcrumbs`, and `tabs` into `FrontierBuilder` directly** (currently generated candidates are re-derived independently from `interactive_elements`, making these fields largely cosmetic) so landmark/tab state actually participates in candidate scoring rather than only feeding `RunMemory` novelty sets.
6. **Widen `NetworkMonitor` to optionally record successful same-origin API calls** (not just failures), gated behind a size/perf budget, so response-shape-driven page understanding (e.g. detecting a data-grid backed by a JSON API) becomes possible.

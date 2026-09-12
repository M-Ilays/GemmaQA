# Adaptive Application Understanding Engine

**Package:** `backend/app/intelligence/adaptive_understanding/`
**Status:** Implemented, wired into the observation loop unconditionally, live-verified.
**Verified baseline:** 1347 passed, 1 skipped, 0 failed (was 1252 before this engine; 95 new tests).

---

## 1. Why this engine exists

GemmaQA previously made one silent assumption on every single observation:

```
navigate  ->  DOMContentLoaded  ->  the page is ready
```

That assumption is false for a large and growing share of real applications, and it fails **silently** — producing a structurally valid but empty observation rather than an error.

The failure was observed live. A run against a real application captured:

```
page_type            : authentication
forms                : 0
interactive_elements : 0
```

Navigation had taken 19 seconds on a loaded server; the DOM was scraped **149 ms** after load fired, before the application had rendered anything. GemmaQA correctly concluded it could not act, and stopped with `authentication_required` after 0 actions. Nothing was broken — the system simply reasoned from an observation that contained nothing, and had no way to know that.

**The generalisation:** an application that has served its shell but rendered nothing is *observably* distinct from one that is ready. It has no interactive controls, no content, sometimes a progress indicator, and it changes between two consecutive looks. None of those facts require knowing *why*.

That is what this engine detects — and it is why the fix is not a wait, a retry count, or a framework check.

---

## 2. What it answers

For every observation:

| Question | Answered by |
|---|---|
| Is this application ready to reason about? | `ReadinessAssessment` (score + the signals behind it) |
| Is it still changing? | `StabilityAssessment` (two-sample comparison) |
| What am I looking at? | `ApplicationStateAssessment` (ranked, evidence-backed hypotheses) |
| How much do I trust this observation? | `ObservationConfidence` (+ what is missing, + what to do next) |
| Should I look again before planning? | `decision` ∈ `accept` / `reobserve` / `accept_degraded` |
| Did the application contradict what I understood? | `UnderstandingContradiction` (5 typed kinds) |
| What did that action actually do? | `ObservedStateTransition` (`state A --action--> state B`) |

---

## 3. Why this is framework-independent

This is the load-bearing design claim, so it is stated precisely and enforced by tests rather than asserted in prose.

### 3.1 Nothing names a framework

No file in the package mentions React, Vue, Angular, Svelte, htmx, or any rendering technology. Enforced by `TestNeutrality.test_no_framework_vocabulary_in_engine_logic`, which AST-walks every non-docstring string constant in the package.

### 3.2 Every signal is a human observation or a web standard

| Signal | What it actually is |
|---|---|
| `no_interactive_elements`, `empty_document`, `no_content` | "Is there anything on screen a person could use or read?" |
| `busy_region_present` | `aria-busy="true"` — a **W3C ARIA** attribute any conforming application may set |
| `progress_indicator_present` | `role="progressbar"` / `role="status"` — **W3C ARIA** roles |
| `repeated_empty_containers` | Several same-shaped containers with no text — detected by *shape*, never by CSS class name (class names like `.skeleton` are one team's convention; "repeated empty boxes where content belongs" is universal) |
| `pending_network_activity`, `failed_network_activity` | Requests in flight or failed |
| `dom_still_changing`, `dom_settled`, `content_growing` | Two consecutive observations compared |

Reading `aria-busy` is reading a **published standard**, the same category of thing as reading an HTTP status code. It is not framework detection.

### 3.3 DOM mutation is detected without a MutationObserver

Rather than injecting an observer or hooking a lifecycle, the engine takes two samples of what a tester would see and compares them (`observation_delta.compare`). Two samples that agree are evidence of quiescence; two that disagree are evidence of ongoing work. This works identically regardless of what is doing the mutating — or whether anything is, in the JavaScript sense, "mutating" at all.

### 3.4 State classification never reads the URL

`state_classifier.py` does not consult the path, query string, or host. This is enforced **structurally**, not just behaviourally:

- `test_state_classifier_never_reads_the_url` AST-walks the module and fails on any `.url` attribute access, any `"url"` string key, or any `urlparse`/`urllib` import.
- `test_classification_is_invariant_to_the_url` classifies the same structure served from five deliberately misleading URLs (including `/dashboard` and `/auth/login`) and asserts a single identical result.

Three reasons this matters:

1. **URLs lie.** A single-page application can present a login screen, a dashboard, and a modal at one URL. A route named `/create` can render a permission denial.
2. **URLs are application-specific.** Matching `/add`, `/new`, or `/edit` encodes one team's naming convention as though it were a property of software in general. When an application renames a route, URL-based classification degrades silently — the exact failure mode that matters most for software under active development.
3. **Evidence is checkable.** "A password field is present" can be shown to a human and agreed with. "The URL contains `/auth`" cannot be — it is a guess about someone else's naming.

**Note on the existing classifier.** `app/agent/explorer.py:classify_deterministic` remains URL- and title-driven and is unchanged, for backward compatibility. It even contains target-application vocabulary (`addcontact`, `adduser`, `contact`). This engine does not replace it; it supplies an independent, evidence-based second opinion. Consolidating the two is future work, noted honestly in §10.

---

## 4. Why there are no fixed waits

The instruction was explicit, and the distinction is real:

- **Rejected:** `sleep(5)` then assume ready. The number is a guess about someone else's infrastructure, wrong in both directions, and unfalsifiable.
- **Implemented:** observe → judge the evidence → look again *only because the evidence is weak* → stop as soon as it is not.

The engine itself contains **no timing primitives at all** — `test_engine_never_sleeps_or_sets_a_timeout` AST-walks the package and fails on `sleep`, `wait_for_timeout`, `import time`, or `asyncio.sleep`. Every one of those would be a way to smuggle an assumption back in.

Two concessions to physics are stated openly rather than hidden:

1. **Two samples cannot be taken at the same instant.** An interval between them is unavoidable. It is owned by the *controller*, not the engine, and it never decides readiness — it only spaces samples apart.
2. **A permanently-animating screen must not stall a run.** After `MAX_OBSERVATION_PASSES` (6) the engine returns `accept_degraded` and records the low confidence honestly, rather than pretending the screen was ready or looping forever.

The interval grows geometrically (400 ms → 4 s cap, ~10 s cumulative). This is a policy about *sampling*, not about readiness, and it exists because application startup cost varies by orders of magnitude between deployments — a flat interval is either too short for a cold, contended environment or wasteful for a warm local one. **A page that is already rendered exits on pass 1 having waited zero milliseconds** (verified live: 17 assessments, 0 re-observations on a healthy application).

---

## 5. Architecture

```
observation (CanonicalPageModel + PageState)
   |
   +-> readiness_signals.extract_signals()      single-observation evidence
   +-> observation_delta.compare()               two-observation evidence (stability)
   |
   +-> readiness_estimator.estimate_readiness()      -> ReadinessAssessment
   +-> state_classifier.classify()                    -> ApplicationStateAssessment
   +-> readiness_estimator.estimate_observation_confidence()
   +-> readiness_estimator.decide()                   -> accept | reobserve | accept_degraded
   |
   +-> understanding_contradictions.*            re-verify prior belief
   +-> understanding_memory                       record (idempotent, versioned)
   +-> understanding_query_engine                 read API
```

| File | Responsibility |
|---|---|
| `schemas.py` | All data structures + 5 closed vocabularies (17 signal kinds, 23 application states, 3 decisions, 5 contradiction types, 4 confidence bases) |
| `readiness_signals.py` | Single-observation evidence extraction |
| `observation_delta.py` | Two-observation comparison; DOM mutation without a MutationObserver |
| `readiness_estimator.py` | Weighted signal combination, confidence, the decision, and the sampling-interval policy |
| `state_classifier.py` | Evidence-only state classification (never the URL) |
| `understanding_contradictions.py` | 5 typed contradiction detectors |
| `understanding_memory.py` | Idempotent, pass-scoped store |
| `understanding_query_engine.py` | Read API |
| `adaptive_understanding_engine.py` | Orchestrator (`assess`, `note_transition`, `note_navigation_result`) |

**Reuse over reinvention.** The engine deliberately mirrors existing conventions rather than inventing parallel ones: the weighted-signal scoring shape of `goal_priority.py`/`strategy_scoring.py`; the `begin_pass()`/`end_pass()` idempotent versioning of `KnowledgeGraphMemory`; the deterministic-id discipline established after the Goal Generation milestone; and the `GraphContradiction` shape (deterministic id, description, evidence, confidence, status) for contradictions.

---

## 6. Application states

23 states, all inferred structurally:

`initializing`, `loading`, `interactive`, `partially_interactive`, `authentication`, `dashboard`, `collection`, `detail`, `create`, `edit`, `wizard`, `modal`, `confirmation`, `validation_failure`, `permission_denied`, `backend_failure`, `frontend_failure`, `empty_state`, `no_data`, `feature_disabled`, `coming_soon`, `placeholder`, `unknown`.

Selected rules and the evidence behind them:

| State | Evidence |
|---|---|
| `authentication` | A `type="password"` input — an HTML standard, present only where credentials are collected |
| `backend_failure` | An HTTP 5xx on this screen (RFC 9110 semantics) |
| `permission_denied` | An HTTP 401/403 |
| `initializing` | No controls, no headings, no text — the shell exists, its content does not |
| `collection` / `empty_state` | A record collection with rows / with zero rows |
| `edit` vs `create` | A form whose fields already carry values vs. one with empty fields |
| `modal` / `confirmation` | An open dialog / an open dialog with few controls and no data entry |
| `validation_failure` | Error-severity alerts on a screen containing a form |
| `feature_disabled` | Every control on the screen is disabled |

**Ranked, not forced.** A dialog containing a confirmation prompt is legitimately both `modal` and `confirmation`; the engine reports both with confidences rather than collapsing to one. **`unknown` is a finding, not a failure** — it carries `unknown_reason` and is counted separately in statistics as something worth investigating.

**Not-yet-rendered wins.** An empty document is classified `initializing`, never `empty_state`. Mistaking an unrendered page for a legitimately-empty dataset would be a serious, misleading error, and is explicitly tested against.

---

## 7. Contradictions: prior knowledge is a hypothesis

Applications under active development change underneath the tester. Rather than overwriting an earlier belief, the engine raises a typed contradiction recording **both** observations:

| Type | Raised when |
|---|---|
| `state_contradiction` | The same structural fingerprint is now understood as a different state (only when *both* classifications were confident — a low-confidence guess changing its mind is learning, not a contradiction) |
| `navigation_contradiction` | A destination reachable earlier in this run no longer is |
| `permission_contradiction` | The same actor's access to the same surface changed |
| `validation_contradiction` | Validation appeared or disappeared for the same form |
| `behaviour_contradiction` | The same action from the same state now leads somewhere different |

Contradiction ids are deterministic, so the same disagreement observed twice is one finding, not two.

---

## 8. Integration

- **Controller.** `_observe()` became an evidence-gated loop; `_observe_once()` retains the original single-pass behaviour. The engine is attached **unconditionally** (`self.memory.understanding_engine = AdaptiveApplicationUnderstandingEngine()`) — it replaces an assumption every run previously made silently, so there is nothing to opt into. `_record_state_transition()` runs after `_run_autonomous_investigation()` in the post-action hook chain, wrapped in the same non-fatal try/except discipline as every other intelligence hook.
- **RunMemory.** New `understanding_engine` field plus 8 query passthroughs (`latest_understanding`, `understanding_history`, `understanding_statistics`, `understanding_contradictions`, `observed_state_transitions`, `known_application_states`, `low_confidence_observations`, `unknown_state_observations`), all degrading to `None`/`[]` when unattached, and an `adaptive_understanding` block in `memory_snapshot()`.
- **Tracing.** Emits `adaptive_understanding.assessment` and `adaptive_understanding.contradiction` events under `GEMMAQA_EXPLORATION_TRACE`.
- **Unchanged.** No existing engine, the Planner, the Safety Validator, `BrowserAdapter`, or `ActionExecutor` was modified. This engine never navigates, clicks, waits, mutates state, or calls a model.

---

## 9. Testing and verification

**95 tests** in `tests/test_adaptive_understanding.py` across 11 groups. Full suite: **1347 passed, 1 skipped, 0 failed** (from 1252 — no regressions).

The neutrality tests (group K) are the load-bearing ones, since they enforce the architectural claims structurally so they cannot regress unnoticed: no framework vocabulary, no business vocabulary, no URL access in the classifier, URL-invariant classification and readiness, and no timing primitives anywhere in the package.

**Live verification.** A probe served a page whose shell contains no controls and no text, injecting a login form after a delay — reproducing the observed failure shape without depending on any particular technology:

| | Result |
|---|---|
| Single-shot observation (previous behaviour) | 0 controls, 0 forms → would have planned from an empty page |
| Adaptive observation (new behaviour) | passes 1–3 empty → pass 4 found **3 controls, 1 form**, classified `authentication`, decision `accept` |
| Already-rendered page | `accept` on pass 1, **0 re-observations** — zero overhead |

**End-to-end** against the bundled ServiceFlow demo through the real controller: 17 assessments, all `accept`, **0 re-observations**, states `collection` (15) and `edit` (2) inferred structurally, 3 behaviour transitions learned (`collection --click--> edit`, `edit --click--> collection`, `collection --click--> collection` ×5), 0 contradictions.

### Two real defects found by live verification

Both were in this engine's own first implementation, found only by running it against a browser — recorded here because they are instructive:

1. **Settled emptiness read as readiness.** Two identical observations of a blank page fired the positive `dom_settled` signal, so an application that never rendered scored *progressively higher* the longer it failed to render (0.00 → 0.37) — precisely backwards. Fixed by `_suppress_settled_emptiness`: while any emptiness signal holds, quiescence argues *against* readiness. Regression tests: `test_settled_emptiness_is_never_read_as_readiness`, `test_settled_non_emptiness_still_counts_as_readiness`.
2. **Observation window too short.** A flat 400 ms interval × 4 passes closed the window at ~1.5 s — the probe rendered at 1.8 s, so the engine gave up moments before content arrived. Fixed with geometric backoff (400 ms → 4 s cap, 6 passes, ~10 s cumulative). Regression tests: `test_reobservation_interval_grows_geometrically_and_is_capped`, `test_cumulative_observation_window_accommodates_a_slow_application`.

---

## 10. Limitations

Stated plainly:

- **The cumulative observation window is ~10 seconds.** An application slower than that (the failing target measured **19 s** navigation, and was outright unreachable in a later probe) will still be accepted as `accept_degraded` with honest low confidence rather than waited out indefinitely. This is a deliberate bound: unbounded waiting is not a behaviour a QA tool should have. Raising `MAX_OBSERVATION_PASSES`/`REOBSERVATION_MAX_INTERVAL_MS` is a one-line policy change if a deployment genuinely needs it.
- **Two classifiers now coexist.** The legacy URL-driven `explorer.classify_deterministic` still populates `PageState.classification` and still feeds the frontier/report path; this engine's evidence-based classification is recorded alongside it but does not yet override it. Consolidating them is a natural next step and was deliberately not attempted here, to avoid changing existing behaviour in the same change that introduced the engine.
- **Contradiction detection is per-run.** GemmaQA has no cross-run persistence, so "prior knowledge" means "earlier in this run". Cross-run behaviour-change detection — arguably the most valuable form for software under active development — requires persistence that does not exist yet.
- **Placeholder-scaffolding detection is conservative.** It requires ≥3 repeated *fully* empty rows in a detected collection. Text-bearing shimmer placeholders will not be recognised as placeholders.
- **`wizard`, `coming_soon`, `placeholder`, `dashboard`, `detail`, `no_data`** exist in the vocabulary but have weak or no dedicated rules yet; screens of those kinds will currently classify as a more generic neighbour (`interactive`, `collection`, `empty_state`). The vocabulary was defined up-front so adding a rule later is a single-point change rather than a schema migration.
- **Readiness thresholds are fixed constants** (`READY_THRESHOLD = 0.6`), tuned against the applications available here, not learned.

---

## 11. Why this improves every future application automatically

Nothing in this engine is specific to the application that exposed the problem. The behaviour it added is:

> *Before reasoning about a screen, check whether there is actually anything there — and if the evidence is weak, look again instead of guessing.*

That is a property of careful observation, not of any particular application, framework, or URL scheme. Any application that renders asynchronously, starts slowly, shows a progress indicator, returns a 5xx, refuses access with a 403, presents an empty dataset, or changes its behaviour mid-run now gets handled — without a line of code naming any of them.

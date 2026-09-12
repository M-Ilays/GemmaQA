# Model Context Pipeline

How GemmaQA decides what an AI model sees, and why.

This document describes the pipeline as built. Its companion,
[MODEL_CONTEXT_AUDIT.md](MODEL_CONTEXT_AUDIT.md), records the read-only audit
that preceded it and remains the historical record of what the code looked like
before — every finding referenced below (`J-1`, `J-4`, `J-6`, `J-8`, `question 20`)
is defined there.

---

## 1. The pipeline

```
ActionGenerationRequest                      app/gemma/base.py
  (planner fills: page, memory, objective, canonical model, budgets)
        │
        ▼
build_model_context()                        app/gemma/model_context.py
  aggregate engine outputs + provenance      ← reuses RunMemory query API
        │                                       and memory_snapshot()
        ▼
project_page()                               app/gemma/page_projection.py
  canonical perception, legacy fallback, or mixed
        │
        ▼
retrieve_for_targets() / _technical_ / _transition_    app/gemma/evidence_retrieval.py
  bounded, sanitized, stable-id evidence     ← reuses perception primitives
        │
        ▼
project_model_context()   app/intelligence/knowledge_graph/graph_context_projector.py
  bounded selection, relevance ranking, deduplication
        │
        ▼
render_action_prompt()                       app/gemma/prompts.py
  centralized templates + section-priority reduction (app/gemma/context_budget.py)
        │
        ▼
GemmaProvider._generate()                    app/gemma/base.py  ← the single funnel
        │
        ▼
parse_and_validate_action() + validate_evidence_ids()    app/gemma/parser.py
```

Everything above the funnel is preparation; `prepare_generation_request()` in
`app/gemma/base.py` is the one method that runs all of it, and **every provider
calls the same one**.

### What each stage is responsible for

| Stage | Module | Responsibility | Not responsible for |
|---|---|---|---|
| Aggregation | `app/gemma/model_context.py` | Gather a superset, attach provenance | Deciding what fits |
| Page projection | `app/gemma/page_projection.py` | Bounded page view, source marking, control dedup | Evidence, ranking |
| Evidence retrieval | `app/gemma/evidence_retrieval.py` | Per-target bounded evidence, stable ids, sanitization | Choosing targets |
| Projection | `graph_context_projector.py` | Relevance ranking, dedup, bounding, projection metadata | Prompt text |
| Rendering | `app/gemma/prompts.py` | Prompt templates, section priorities | Which facts exist |
| Reduction | `app/gemma/context_budget.py` | Fit the budget without breaking structure | Judging relevance |
| Validation | `app/gemma/parser.py` | Reject invented ids, actions, risks | Building context |

The separation between *aggregation* and *projection* is deliberate: without it,
"add a field" silently becomes "grow every prompt".

---

## 2. Normalized context schema

`ModelContext` (`app/gemma/model_context.py`) is the typed aggregation boundary.
`project_model_context()` turns it into the section-oriented structure a prompt
renders:

```jsonc
{
  "task_context": {
    "operator_testing_objective": "…" | null,
    "objective_status": "not_provided_by_operator",   // only when absent
    "active_goal": {…}, "active_scenario": {…}, "current_step": {…},
    "qa_strategy": {…}, "investigation_state": {…}, "mode": "exploration"
  },
  "page_context": {
    "source": "canonical" | "legacy_fallback" | "mixed",
    "url": "…", "title": "…", "state_fingerprint": "…", "observation_version": "…",
    "controls": [ { "element_id", "role", "accessible_name", "semantic_action", … } ],
    "forms": [ { "form_id", "fields": […], "intent", "intent_confidence" } ],
    "collections": [ { "collection_id", "collection_type", "columns", "row_count", … } ],
    "dialogs": […], "alerts": […], "unknown_regions": […], "visual_evidence": […],
    "semantic_regions": […], "navigation": […], "headings": […],
    "readiness": { "application_state", "readiness_score", "observation_confidence" }
  },
  "engine_context": {
    "unresolved_contradictions": [ { "contradiction_type", "description" } ],
    "coverage_gaps": [ { "gap_id", "gap_type", "description", "exploration_value", "risk" } ],
    "workflow_hypotheses": [ { "name", "status", "confidence" } ],
    "diagnostics": { "knowledge_graph": {…}, "qa_strategy": {…}, … }
  },
  "graph_context": { "nodes": […], "relationships": […], "summary": {…} },
  "technical_evidence": {
    "console_error_evidence_ids": […], "network_failure_evidence_ids": […],
    "screenshot_evidence_id": "…"
  },
  "application_memory": { "authenticated": false, "credential_profile_available": true, … },
  "constraints": { "safe_mode", "authorized_domain", "allowed_actions",
                   "remaining_action_budget", "remaining_page_budget" },
  "evidence_registry": [ { "evidence_id", "kind", "summary", "observation_version" } ],
  "recent_actions": […], "visited_states": […], "unexplored_navigation": […],
  "blocked_reasons": […],
  "context_provenance": { "<section>": { "producer", "source_ref", "confidence" } },
  "projection_metadata": {
    "included_sections": […], "reduced_sections": […], "omitted_sections": […],
    "estimated_tokens": 0, "page_source": "…", "engine_outputs_considered": 0,
    "graph_nodes_projected": 0, "graph_edges_projected": 0,
    "gaps_included": 0, "evidence_items": 0
  }
}
```

---

## 3. Field provenance

Every attributed section carries a `ContextProvenance`: which component produced
it, which object it came from, its confidence where the producer supplies one, and
the observation version. A value with no traceable origin cannot be audited after
the fact, which is why this exists at all.

| Section | Producer | Read via |
|---|---|---|
| `operator_testing_objective` | operator → `RunConfiguration.testing_objective` | `controller.plan_context` → `Planner` |
| `constraints` | `SafetyPolicy` / `RunConfiguration` | `ActionGenerationRequest` |
| `budgets` | `RunMemory` | `ActionGenerationRequest` |
| `active_goal` | `GoalGenerationEngine` / `ExplorationGoal` | `memory_snapshot()` |
| `active_scenario` | `ScenarioPlanningEngine` | `RunMemory.scenario_summary()` |
| `qa_strategy` | `QAStrategyEngine` | `RunMemory.strategy_summary()` |
| `investigation_state` | `AutonomousInvestigationEngine` | `RunMemory.current_investigation()` |
| `readiness` | `AdaptiveApplicationUnderstandingEngine` | `RunMemory.latest_understanding()` |
| `contradictions` | same | `RunMemory.understanding_contradictions()` |
| `coverage_gaps` | graph / workflow / dependency / scenario gap analyzers | their `*_gaps()` methods |
| `workflow_hypotheses` | `WorkflowDiscoveryEngine` | `incomplete_workflows()`, `low_confidence_workflows()` |
| `graph_context` | `KnowledgeGraph` | `GraphContextProjector.context_for_node()` |
| `page_context` | `PerceptionEngine` (canonical) or `PageObserver` (legacy) | `project_page()` |
| `recent_actions` | `ActionExecutor` | `RunMemory.previous_actions_payload()` |
| `evidence_registry` | `PerceptionEngine`, `AccessibilityExtractor`, `PageObserver`, `EvidenceCollector` | `app/gemma/evidence_retrieval.py` |
| `application_memory` | `RunMemory` + `CredentialVault.public_flags()` | `memory_snapshot()` |

Nothing here recomputes an engine's business logic. If a value is not already
computed somewhere, it does not belong in the aggregate.

---

## 4. GraphContextProjector responsibilities

`GraphContextProjector` was already the only bounded, secret-free,
evidence-summarizing projector in the repository (audit section F, item 3) — and
was never wired to a prompt. Rather than build a competing component beside it,
the existing one grew the responsibility.

It now does two things:

1. **`context_for_node(...)`** — unchanged. A bounded graph slice around one
   focus node, for the Goal Generation and Scenario Planning engines.
2. **`project_model_context(context)`** — new. Turns a `ModelContext` aggregate
   into the bounded, ranked, deduplicated section structure above.

Rules it follows:

- **Relevance, not recency.** `_relevance()` ranks by exploration value,
  confidence, and risk. A record explicitly below `LOW_CONFIDENCE_THRESHOLD`
  is pushed to the tail rather than dropped: "we are unsure about X" is itself
  actionable for a QA agent.
- **Deduplicate.** `_dedupe()` removes repeated facts by identity fields. Two
  records describing the same thing waste budget and invite the model to treat
  one fact as corroboration of itself.
- **Exact gap detail, never a bare count.** The audit's J-8 finding was that the
  model was told `"gap_count": 7` and never the seven gaps.
- **Bounded.** `MODEL_MAX_GRAPH_NODES`, `MODEL_MAX_GRAPH_EDGES`,
  `MODEL_MAX_GAPS`, `MODEL_MAX_CONTRADICTIONS`, `MODEL_MAX_WORKFLOW_HYPOTHESES`.
- **Say the graph exists even when no slice is shown.** With no focus node it
  emits a size summary, so "no relationships shown" is not read as "none exist".
- **Structured data only.** No prose, no prompt phrasing. Rendering stays in
  `app/gemma/prompts.py`.

---

## 5. Token-budget policy

**The authority is `Settings.max_prompt_chars` (default 24000).** Every existing
caller already honoured it, so it stays the limit.

**No tokenizer ships with this project.** `estimate_tokens()` uses
`CHARS_PER_TOKEN = 3.2`, a documented conservative approximation: real tokenizers
average roughly 4 chars/token on prose and fewer on punctuation-dense JSON, so 3.2
deliberately *over*-estimates. An over-estimate spends budget that was not needed;
an under-estimate silently overruns the model's context window. It is used for
reporting and headroom, never as the authoritative limit.

### Reduction is structural, not textual

The audit's highest-severity finding (J-1) was that `clamp_prompt` cut the **end**
of the serialized string, and the end of the action payload was where the page
observation and the anti-prompt-injection reminder lived. On any large page the
model lost the controls it was required to choose from, lost the security
reminder, and received JSON that stopped mid-structure.

`reduce_sections()` **reduces the object graph and serializes once.** A payload
built this way is well-formed by construction — no code path can emit a
half-written object, because the serializer only ever sees a complete object.

Sections are reduced by declared importance, never by document position:

| Preserve first | | Reduce first |
|---|---|---|
| 1 security reminder *(protected)* | | 9 older action history |
| 2 operator testing objective | | 10 known pages / visited states |
| 3 task context (goal, scenario, step) | | 11 low-confidence hypotheses |
| 4 safety constraints | | 12 verbose engine diagnostics |
| 5 current state and readiness | | 13 redundant page prose |
| 6 actionable controls | | 14 graph context |
| 7 evidence registry | | 15 low-relevance evidence summaries |
| 8 failures and contradictions | | |

Three passes, in order:

1. **Shrink and drop the reducible tier** — lists halve, long strings summarize,
   then a section that cannot shrink further is dropped.
2. **Shrink the preserve tier** — last resort, never dropping it.
3. **Drop whole sections in reverse-priority order** — the floor. Shrinking alone
   bottoms out with every list at one item; without this pass the reducer stopped
   there and quietly overran the budget. *(Found by a live probe, not by review.)*

Halving rather than removing keeps the *shape* visible: five of forty recent
actions still says "there is history here", where an absent key says "there is
none".

### The irreducible floor

`action_prompt_floor_chars()` states the smallest possible prompt: the
response-schema contract plus the security reminder. Without the schema the model
cannot produce parseable output; without the reminder the injection defence is
gone. Neither is a size trade-off. A budget below the floor cannot be honoured, so
the floor is stated rather than discovered as a silent overrun. The default budget
is roughly 25× it.

### `known_element_ids` is derived after reduction

It lists ids whose controls actually survived. Listing an id whose control was
reduced away would invite the model to choose something it cannot see described.
Validation in `parse_and_validate_action` still accepts the full observed set (a
superset), so reduction can never block a legitimate control.

---

## 6. Canonical versus legacy page projection

`CanonicalPageModel` — accessibility roles, collections, form intents, action
semantics, visual evidence, unknown components — was stored on `RunMemory` and
consumed by eleven engines while every prompt saw the thinner legacy `PageState`
(audit J-4). `project_page()` is the missing path.

- **Never blindly serialized.** The canonical model is a superset view built for
  engines. Every field here is a bounded projection with explicit caps, so a
  500-control page and a 5-control page produce predictable prompt sizes.
- **Source is always stated**: `canonical`, `legacy_fallback`, or `mixed`.
  Silently degrading between them is exactly the invisible behaviour change the
  audit was written to prevent.
- **Stale models are rejected, not trusted.** A canonical model describing a
  different URL is discarded with `canonical_rejected_reason: "url_mismatch"`. A
  stale model is worse than none: it is confidently wrong about which controls
  exist.
- **Controls are deduplicated by `element_id`.** A control present in both
  sources appears exactly once, described by the richer canonical source.
- **`mixed` is honest about blending.** Console errors have no canonical
  equivalent and always come from `PageState`; taking anything from the legacy
  source marks the projection `mixed`.
- **No selectors, ever.** Controls are named by `element_id`; resolving one to a
  live element is the runtime's job. Keeping selectors out of model context is
  what stops a hallucinated selector from reaching the browser.

---

## 7. Targeted evidence retrieval

Built on `app/perception/evidence_primitives.py` — the three functions the
visual-analysis call already used, now shared rather than duplicated
(`visual_analyzer` re-exports them under their original private names, so nothing
that imported them changed).

- **Never the whole DOM.** Retrieval is per-target and size-capped. There is no
  "give me the page" mode, by construction.
- **Sanitize before the model.** `sanitize_html_fragment()` removes scripts,
  styles, comments, event handlers, `data-*` payloads, and credential-shaped
  attributes, while keeping what a QA reasoner needs: `role`, `type`, `name`,
  `label`, `for`, `required`, `disabled`, `checked`, and all `aria-*`. Truncation
  is at a tag boundary, never mid-tag.
- **Structured evidence where no HTML is retained.** GemmaQA does not keep raw
  HTML after observation (by design — audit question 8), so the honest bounded
  equivalent of "the subtree for this control" is its structured description.
  `sanitize_html_fragment()` exists for callers that *do* hold a fragment.
- **Refuse unknown or stale targets.** Retrieval for an element the current
  observation does not contain returns nothing and logs it — never an
  empty-but-plausible record.

Supported kinds: `dom_subtree`, `accessibility_subtree`, `screenshot_reference`,
`network_failure`, `console_error`, `form_component`, `graph_evidence`,
`transition_evidence`.

**One pass only.** The interfaces are shaped so a future model turn could request
more evidence, but no recursive model loop is introduced — an unbounded
evidence-request loop is a larger safety question than this milestone should
decide. See §11.

---

## 8. Evidence registry and validation

Audit J-6: model-invented `evidence_ids` were accepted unvalidated, written onto
`Defect`, and exported to CSV — while the correct drop-and-log pattern already
existed one file over in `parse_visual_analysis`.

`EvidenceRegistry` is built **per model call** and does two jobs that are the same
job from both directions: tell the model which references exist, and reject any
reference it returns that does not.

**Evidence ids are deterministic and content-derived**
(`stable_evidence_id(kind, payload)` → `ev_<kind>_<sha256[:12]>`). Never
`new_id()`: a random id would make every re-observation look like fresh evidence,
and would make staleness undetectable. Because ids derive from content, an id from
an earlier observation provably is not in the current registry.

`validate_evidence_ids()` splits returned ids into accepted and rejected:
deduplicated with order preserved, unknown ids dropped with a structured warning,
rejected ids recorded on the registry for debugging. Applied to
`parse_and_validate_action` (action citations) and `parse_bug_analysis` (defect
citations — the one place a model reference gets persisted).

With no registry supplied, ids pass through unchanged, so older callers behave
exactly as before.

Element references are validated the same way, and always were: unknown
`element_id` is rejected in action parsing and dropped in visual parsing; `goal_id`
and `candidate_id` are filtered to the input set.

---

## 9. Provider parity

Audit question 20 recorded that `MockGemmaProvider.generate_action` short-circuited
before any prompt existed, so under the default `GEMMA_PROVIDER=mock` the
aggregation, projection, sanitization, prompt-building, reduction, parsing, and
validation stages were never exercised in CI. A regression in any of them was
invisible.

The mock is **not** made to reason semantically — it is a heuristic provider and
stays one. But it no longer skips the infrastructure:

1. It calls `prepare_generation_request()`, exercising the full normalized path.
2. It makes its deterministic decision as before.
3. It serializes that decision as model output and runs it through
   `parse_and_validate_action` with the real registry and known-id set.

If its own decision fails validation, the deterministic action wins and the
mismatch is logged — this provider's decisions are already safety-bounded by
construction, and degrading them would be a real regression.

`prepare_generation_request` is defined once on `GemmaProvider` and overridden by
nobody; a test asserts that. Transport formatting differs between an HTTP endpoint
and a local pipeline; context meaning does not.

---

## 10. Sensitive-data handling

Four independent layers, none of which relies on the others:

1. **Element level** — `context_sanitizer._is_sensitive_field()` treats every
   `password` and `hidden` input as sensitive, and
   `sanitize_element_for_model()` sets `current_value = None` even for
   *non*-sensitive fields. No free-text field value ever reaches a model.
2. **Key level** — `sanitize_dict()` masks sensitive key names, now including
   payment and government identifiers (`card_number`, `cvv`, `iban`,
   `account_number`, `routing_number`, `ssn`, `passport`, `tax_id`, …).
   *Added in this work: a test proved card data reached serialized context.*
3. **Value level** — `sanitize_text()` redacts `Bearer …`, `Basic …`,
   `api_key=…`, `password=…`, `sessionid=…`, `cookie=…`, plus
   **Luhn-validated payment card numbers**. The Luhn check is what keeps this
   precise: the same 13–19-digit shape is also an ordinary order id, and masking
   every long number would strip real observations a QA agent needs.
4. **Fragment level** — `sanitize_html_fragment()` (§7).

Credentials reach the model only as capability metadata:

```json
{ "credential_profile_available": true, "actor_role": "admin" }
```

Actual secret retrieval stays inside the executor/authentication boundary. Also:
`previous_actions_payload()` omits each action's `value`, so a typed password never
appears in history; `sanitize_url()` strips userinfo and masks sensitive query
params; screenshots with credential-shaped filenames are skipped
(`images.should_skip_screenshot`).

Reports carry counts, names, and flags only — never prompt text, raw DOM,
credentials, or evidence payloads.

---

## 11. Extension points

**Model-requested evidence.** `retrieve_for_targets(registry, target_element_ids,
…)` already takes an arbitrary target list and refuses unknown references, so a
second turn driven by the model's own request is a matter of choosing the targets
rather than building new machinery. Two things must come with it: a hard cap on
turns (modelled on `MAX_OBSERVATION_PASSES` in the adaptive-observation loop) and
a rule that the second turn sends *more* context than the first — the existing
correction retry sends less, which is the opposite of what an evidence request
needs.

**Other model-call categories.** `generate_test_scenarios`, `analyze_page`,
`generate_final_report`, and the two ranking calls still assemble their own
thinner contexts. Migrating them means constructing a `ModelContext` and adding a
`render_*_prompt` beside `render_action_prompt`; the aggregation, projection,
evidence, and reduction stages are already category-agnostic.

**A real tokenizer.** Replace the body of `estimate_tokens()`. Every caller reads
it through that one function.

---

## 12. Reporting

`FinalReport.model_context_summary` (from `RunMemory.model_context_summary()`):

| Field | Meaning |
|---|---|
| `planning_iterations_recorded` | Planning iterations observed |
| `decision_path_counts` | `model` vs `deterministic_frontier` |
| `model_calls_recorded` | Iterations where the model chose the action |
| `page_source_counts` | canonical / legacy_fallback / mixed |
| `sections_reduced_counts`, `sections_omitted_counts` | What the model did not see |
| `invalid_evidence_ids_rejected` | Model-invented references dropped |
| `reduction_applied_count` | Iterations that exceeded budget |
| `latest` | Tokens, prompt chars, evidence items, gaps, graph nodes/edges, engine outputs, provider, whether real prompt construction ran |

Not every iteration reaches the model: GemmaQA plans deterministically from the
unified frontier whenever it can, and asks the model when no deterministic
candidate dispatches. **Both outcomes are recorded**, so a reader can see how many
decisions were model-driven instead of assuming.

`FinalReport.testing_objective` carries the operator's objective verbatim, or
`null` when none was given.

---

## 13. Tests

`tests/test_model_context_pipeline.py`, 70 tests, all network-free:

| Group | Covers |
|---|---|
| A | Real prompt path exercised under a deterministic provider |
| B | Structured reduction: valid JSON, reminder preserved, priority order, determinism, convergence, the documented floor |
| C | Operator objective end to end; missing vs empty |
| D | Canonical projection, legacy fallback, stale rejection, control dedup, bounding |
| E | Relevance ranking, dedup, exact gap detail, provenance |
| F | Evidence registry; all-valid / mixed / all-invented / duplicate / stale ids |
| G | Sensitive data: nine secret classes, capability metadata, HTML sanitization, DOM bounding |
| H | Provider parity; the default mock no longer bypasses the pipeline |
| I | Architectural invariants: single funnel, centralized templates, no second context stack, parsing compatibility |
| J | Reporting metadata is present, bounded, and safe |
| K | End-to-end: real browser, real controller, real report |

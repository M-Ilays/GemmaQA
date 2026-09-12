# Autonomous Entity Discovery Engine

GemmaQA automatically discovers an application's business entities (whatever
they turn out to be — Customer, Invoice, Shipment, Driver, ...) from
structural evidence alone. The engine ships **zero application-specific
vocabulary**: no entity name it can ever report exists as a string literal
anywhere in its code (enforced by an AST-walking contract test — see
[Testing](#testing)). Every name comes from the target application's own
observed text.

**Status:** implemented, wired into the observation loop
(`app/agent/controller.py`), best-effort/non-fatal, live-verified against
three real applications.

## Architecture

```
app/intelligence/
    __init__.py
    entity_discovery/
        schemas.py                     EntityRecord, EntityEvidence, EntityOperation,
                                        EntityRelationship, EntityCandidate
        entity_candidate_builder.py     CanonicalPageModel -> raw candidate terms
        entity_classifier.py            candidates -> per-term findings (evidence
                                         grouping, operations, related structures, states)
        entity_relationship_builder.py  URL nesting / cross-entity columns & fields
                                         -> observed relationship findings
        entity_confidence.py            corroboration-based scoring + status rules
        entity_memory.py                merge findings into persistent EntityRecords
        entity_registry.py              the queryable catalog (Planner-facing)
        entity_discovery_engine.py      orchestrator + tracing
```

`app/intelligence/` sits **on top of** the Perception Engine and
ApplicationStore — it reads their already-typed output
(`CanonicalPageModel`); it never touches the browser, never invents a
selector or action, and never calls an LLM. Same guardrails as the
Perception Engine, one layer up.

## Pipeline (`EntityDiscoveryEngine.observe`)

Called once per observation (same choke point the Perception Engine uses),
given the CanonicalPageModel that observation just produced:

1. **`EntityCandidateBuilder.build()`** — walks every CanonicalPageModel
   collection and extracts raw candidate terms with their evidence:
   navigation items, breadcrumbs, tabs, headings, the (app-name-suffix-split)
   page title, URL path segments, table column headers, form field
   labels/names, dialog text, and button/link labels (verb-stripped: "Add
   Customer" contributes the noun "customer"; a bare "Export" contributes no
   term but is remembered for operation inference). Network evidence
   contributes API path segments.
2. **`EntityClassifier.classify()`** — groups candidates by normalized term,
   determines the page's own subject entity (the term with the most
   evidence from page-identity sources: URL/breadcrumb/heading/title/nav),
   attaches that page's tables/forms to the subject entity, infers
   operations (see below), and collects `known_states` from any
   status/state-like table column.
3. **`EntityRelationshipBuilder.build()`** — records observed structural
   relationships (see [Relationships](#relationship-discovery)).
4. **`EntityMemory.merge_discovery()` / `.merge_relationships()`** — merges
   this page's findings into the persistent registry (see
   [Memory](#memory--merging)).
5. **Trace** — one `entity_discovery.observation` event per observation (see
   [Tracing](#tracing)).

## Sources of evidence

| Source | `EntityEvidence.source_kind` |
|---|---|
| Navigation items (header, sidebar — wherever `CanonicalPageModel` found them) | `navigation_item` |
| Breadcrumbs | `breadcrumb` |
| Tabs | `tab` |
| Headings | `heading` |
| Page title (split on `-`, `|`, `:`, `·`, `–`, `—`, `>` to isolate the subject from an app-name suffix) | `page_title` |
| URL path segments (id-like segments and file extensions dropped) | `url_segment` |
| Table column headers | `table_column` |
| Tables (attached to the page's subject entity) | `table_region` |
| Form field labels/names | `form_field` |
| Forms (attached to the page's subject entity) | `form_region` |
| Dialog text | `dialog` |
| Button labels (verb-stripped) | `button_label` |
| Link labels | `link_label` |
| ARIA labels (when they differ from the visible label) | `aria_label` |
| API endpoint URLs (from network evidence) | `api_endpoint` |
| Network responses (HTTP method → operation) | `network_response` |
| Prior observations already in the registry | `memory` |

Cards are covered structurally — a card's own heading/label flows through
the same `heading`/`button_label` paths (a card is not a distinct DOM
concept in `CanonicalPageModel`, per the Perception Engine's model). Dialogs,
tabs, and breadcrumbs are each their own first-class collection already.

## Entity record fields

Every `EntityRecord` (`schemas.py`) carries: `entity_id`, `canonical_name`,
`aliases`, `confidence`, `status`, `evidence` (list of `EntityEvidence`),
`discovered_in` (distinct source kinds), `operations` (list of
`EntityOperation`, each with its own evidence + confidence),
`related_pages`, `related_forms`, `related_tables`, `related_workflows`
(reserved for future workflow-phase population), `known_states`,
`relationships` (list of `EntityRelationship`), `first_seen`/`last_seen`
(timestamps) and `first_seen_iteration`/`last_seen_iteration`.

## Operations — inferred, never hardcoded

`OPERATION_VERBS` (`entity_candidate_builder.py`) is a **generic English UI
verb lexicon** ("add"/"create"/"register" → `create`, "export" → `export`,
...) plus `HTTP_METHOD_OPERATIONS` (POST → `create`, GET → `view`, PUT/PATCH
→ `edit`, DELETE → `delete`). Nothing in either table is specific to any
entity — the same lexicon produces `create` whether it's attached to
"customer", "shipment", or a word this codebase has never seen.

An operation is attached to an entity in exactly two ways:

1. A UI control's label starts with a recognized verb: "Add Customer" →
   `create` on **customer**. A **bare** verb ("Export", "Search") with no
   noun is attributed to the **page's own subject entity**.
2. A network call's HTTP method + URL: `POST /api/customers` → `create` on
   **customer**.

`NON_ENTITY_OPERATIONS` (`cancel`/`submit`/`save`/`apply`/`reset`/`refresh`)
are recognized as verbs — so they still strip correctly out of a label like
"Apply filters" — but are **never recorded as an entity operation**: every
form in every application has a Save button, and recording it would report
the same meaningless capability for every entity discovered (a real,
live-verified false signal — see [Live findings](#live-findings)).

## Entity Registry

The Planner-facing catalog (`entity_registry.py`):

```
Entity: customer
  Operations: create, search
  Pages:      https://.../customers
  Evidence:   navigation_item, heading, url_segment, table_region, form_region
  States:     Active, Prospect
```

Query methods (also exposed on `RunMemory`, see below):

- `known_entities()` — confirmed + incomplete (things the run positively knows exist).
- `unknown_entities()` — candidate terms seen but not yet corroborated.
- `incomplete_entities()` — confirmed entities with no operations or no
  related pages discovered yet.
- `entities_requiring_exploration()` — incomplete entities first, then
  candidates: the most promising next-exploration targets.

## Confidence & status

`entity_confidence.py` scores an entity from the **distinct kinds** of
evidence corroborating it, not raw repetition — `SOURCE_KIND_WEIGHTS` grades
navigation/breadcrumb/API/URL/table-region evidence highest (applications
name their nav, routes, and tables after their entities) down to bare
visible text (weakest). Repeating the **same** source kind adds only a small
increment (diminishing returns), so twenty mentions in body text never
outweigh one navigation item + one table.

Status lifecycle (`status_for`):
- **`candidate`** — fewer than 2 distinct source kinds, or confidence below
  the confirmation threshold.
- **`incomplete`** — confirmed, but no operations or no related pages yet
  (exactly what `entities_requiring_exploration()` should point more
  exploration at).
- **`confirmed`** — corroborated and has at least one operation and one
  related page.

## Relationship discovery

Deliberately narrow per the task's explicit scope — **records observed
relationships only, no workflow reconstruction**:

1. **URL nesting**: `/customers/{id}/orders` → `customer owns order` +
   `order belongs_to customer`.
2. **Cross-entity table column**: a column on one entity's page named after
   *another* known entity ("Assigned Driver" on the Jobs page) →
   `job assigned_to driver` (the generic word "assign" in the column label
   selects `assigned_to`; otherwise `references`).
3. **Cross-entity form field**: same rule, for a form field label/name.

A relationship is only ever recorded between two terms that **both** already
resolve to known entities — it can never invent a third, phantom entity.

## Memory & merging

`entity_memory.py` is the mutable store behind the registry:

- **Exact-term merge**: the same normalized term always merges into the same
  record (evidence/aliases/operations/states are deduped and accumulated,
  never duplicated).
- **Weak multiword fold-in**: a multiword term with fewer than 2 distinct
  evidence kinds, whose **head noun** already exists as an entity, folds into
  that entity as an alias ("Enterprise Customer" seen once → alias of
  **customer**) instead of becoming a spurious second entity. A
  strongly-corroborated multiword term stays its own entity ("purchase
  order" can be genuinely distinct from "order").
- **Confidence evolves**: recomputed from the full merged evidence set on
  every observation — re-observing the *same* page never inflates it (exact
  evidence is deduped by `(source_kind, observed_text, page_url)`); a
  **new** kind of corroboration raises it.
- `first_seen`/`first_seen_iteration` set once; `last_seen`/
  `last_seen_iteration` updated every merge.

## Planner integration

`RunMemory` (`app/agent/memory.py`) gained `entity_registry` plus four
query methods that thinly delegate to it and degrade to `[]` when no
registry is present (no None-guards needed by callers):
`known_entities()`, `unknown_entities()`, `incomplete_entities()`,
`entities_requiring_exploration()`. A compact entity summary (`known` /
`incomplete` / `candidates` name lists — never raw evidence) is also folded
into `memory_snapshot()`, the same payload already sent to the LLM's
planning context.

`app/agent/controller.py` instantiates one `EntityDiscoveryEngine` per run
(`self.memory.entity_registry = entity_engine.registry`) and calls
`entity_engine.observe(model, iteration=...)` right after the Perception
Engine builds each observation's `CanonicalPageModel` — best-effort, wrapped
in its own try/except so an entity-discovery failure can never break
perception, planning, or execution.

## Tracing

One `entity_discovery.observation` event per observation
(`app.utils.exploration_trace`, reused): `page_context_entity` (this page's
own subject), `created`/`updated`/`alias_merged` term lists, `new_relationships`,
and per-touched-entity detail — `status`, `confidence`, `discovered_in`,
`operations`, `aliases`, and a `why` list (`"navigation_item: 'Customers'"`,
...) naming the actual observed text behind the discovery — plus running
registry totals.

## Testing

`tests/test_entity_discovery.py` (44 tests): normalization (singularization,
verb/noun splitting, URL segment filtering), candidate extraction from every
evidence source, single-observation classification (confirmation,
operations, states, related structures, "no operations without evidence"),
relationship discovery (URL nesting, assigned-column, no-phantom-entities),
memory merging (repeat-observation stability, new-evidence-kind confidence
increase, weak-term fold-in, first/last-seen tracking), confidence unit
behavior, registry queries (including `RunMemory`'s pass-through and
no-registry degradation), **two structurally unrelated synthetic
applications** (a field-service CRM: customer/job/technician: and a
logistics app: shipment/vehicle/route) proven with the *same* code, an
AST-based contract test asserting no business-word string literal exists
anywhere in the package's actual code (docstrings/comments excluded), and one
live Playwright integration test.

**Live-verified** against all three applications used in
[`EVIDENCE_DRIVEN_EXPLORATION_REPORT.md`](./EVIDENCE_DRIVEN_EXPLORATION_REPORT.md)
with the identical, unmodified engine:

- **ServiceFlow**: `customer` and `job` both confirmed at confidence 1.00,
  with real observed states (`Active`/`Prospect`, `In Progress`/`Scheduled`)
  and real relationships (`job references customer`,
  `job assigned_to assignee`).
- **InsightBoard**: `report` (with `Pending`/`Ready` states) and `team`
  confirmed/incomplete; tab labels ("Performance", "Activity") correctly
  surfaced only as low-confidence candidates (single-source evidence).
- **SauceDemo**: `cart` confirmed; `inventory` a candidate (SauceDemo's
  product grid uses no semantic nav/table markup for its items — see
  [Known limitations](#known-limitations)).

## Live findings (bugs found and fixed by live testing)

1. **Form chrome verbs recorded as entity operations.** Every form has
   Save/Apply/Cancel/Reset controls; without a filter, live testing showed
   these attributed to whatever entity happened to be the page's subject —
   the same meaningless operation set on every entity. Fixed with
   `NON_ENTITY_OPERATIONS` (verbs still strip from labels, never become
   recorded operations). Regression:
   `test_form_chrome_verbs_are_not_recorded_as_entity_operations`.
2. **Demo/sample-environment words treated as entities.** "ServiceFlow Demo"
   in a page title produced a 0.51-confidence `demo` entity live. Fixed by
   adding demo/sample/test/sandbox-style words to the universal chrome
   stopword list. Regression: `test_demo_environment_words_are_not_entities`.
3. **`"contact"` was originally in the generic stopword list** — caught by
   this project's own no-business-vocabulary contract test during
   development, since "Contact" is chrome in some apps (a "Contact Us" link)
   but a genuine CRM entity in others. Removed; such ambiguous words now
   simply start as low-confidence candidates like anything else.

## Known limitations

- **Markup-dependent, like the Perception Engine it builds on.** A page with
  no semantic navigation/table/heading structure (SauceDemo's product grid)
  yields fewer, weaker candidates — `inventory` stayed a candidate in the
  live run above. This mirrors the Perception Engine's own documented
  region-detection limitation.
- **English-only linguistics.** Singularization, verb recognition, and the
  stopword list are English-UI-specific; a non-English application would need
  a parallel lexicon, not a code change to the pipeline itself.
- **One page = one subject entity.** `page_context_term` picks a single
  "this page is about X" term; a page genuinely covering two entities
  equally (rare) would still attribute tables/forms/operations to only one.
- **`related_workflows` is not yet populated** — reserved for a future phase
  once workflow reconstruction (explicitly out of scope here) exists.
- **No cross-run persistence yet** — the registry lives on `RunMemory` for
  the duration of one run; it is not (yet) saved to `ApplicationStore` for
  reuse across separate runs against the same application.

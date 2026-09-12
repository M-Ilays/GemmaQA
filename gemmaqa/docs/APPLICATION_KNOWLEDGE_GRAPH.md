# Application Knowledge Graph

## Purpose

The Application Knowledge Graph is a **semantic projection layer** that unifies the
knowledge already discovered by four independent engines — Entity Discovery, Actor
Discovery, Workflow Discovery, and Business Dependency Discovery — into one evidence-backed,
queryable, in-process graph.

It is the semantic memory layer that future Goal Generation, Scenario Planning, QA
Strategy, and Autonomous Investigation engines will use to answer questions such as:

- Which actors interact with an entity, and which actor can perform a given operation?
- Which workflows involve an entity, and which actor performs each step?
- Which state transition follows an action, and which prerequisites block a workflow?
- Which workflows produce a derived output, and which entity states contribute to a KPI?
- Which actors consume a report or dashboard?
- Which permissions have no known workflow? Which visible output has no known producer?
- Which relationships are observed, inferred, contradicted, stale, or unresolved?
- Which parts of the application are isolated or weakly understood?

**This milestone does not generate goals, plan scenarios, select QA strategies, switch
actors automatically, or execute autonomous investigations.** It only builds and exposes
the graph those future engines will consume.

## Scope discipline

Per its commissioning specification, this package explicitly does **not**:

- Rediscover anything from raw page observations — every node/edge traces back to an
  `EntityRegistry` / `ActorRegistry` / `WorkflowRegistry` / `DependencyRegistry` record.
- Replace or bypass those registries — they remain authoritative; the graph is read-only
  with respect to them.
- Convert `inferred` or `candidate` relationships into `observed` ones.
- Run open-ended LLM inference or derive arbitrary business rules from graph proximity —
  exactly 7 fixed, explainable inference rules are implemented (see below).
- Execute any browser action — the graph is purely an in-memory reasoning structure.
- Use an external graph database (Neo4j, ArangoDB, JanusGraph, RDF stores). The graph is
  plain in-process Python (dicts of Pydantic models plus adjacency indices).

## Architecture

Package: `backend/app/intelligence/knowledge_graph/`

| File | Responsibility |
|---|---|
| `schemas.py` | All graph schemas (`KnowledgeNode`, `KnowledgeEdge`, gaps, contradictions, queries, snapshots, versions...) |
| `graph_node_factory.py` | Deterministic node-id formatters |
| `graph_edge_factory.py` | Deterministic edge-id formatter + evidence conversion |
| `graph_confidence.py` | Confidence derivation for observed/inferred edges + status-vocabulary mapping |
| `knowledge_graph_memory.py` | The raw store: nodes, edges, adjacency indices, idempotent upsert, pass-scoped versioning |
| `relationship_resolver.py` | Resolves a name/alias/hint to a node, or defers as a `PendingReference` |
| `graph_synchronizer.py` | Projects the four source registries into nodes/edges (idempotent, incremental) |
| `graph_inference_engine.py` | The 7 bounded inference rules |
| `graph_consistency_checker.py` | Detects contradictions and structural inconsistencies |
| `graph_gap_analyzer.py` | Produces `GraphGap` records for incomplete/disconnected knowledge |
| `graph_query_engine.py` | Bounded node/edge/semantic/traversal query API |
| `graph_context_projector.py` | Compact context bundles for a future Goal Generation Engine |
| `graph_serializer.py` | Compact/full/DOT/human-readable serialisation |
| `knowledge_graph.py` | `ApplicationKnowledgeGraph` — the orchestrator; the only class other GemmaQA code talks to |

## Graph model

A directed, typed, **attributed multigraph**: two nodes may be connected by several
distinct edge types simultaneously (an actor can `can_view`, `can_edit`, and `owns` the
same entity, each a separate edge). Implemented as plain Python dicts
(`memory.nodes: dict[str, KnowledgeNode]`, `memory.edges: dict[str, KnowledgeEdge]`) with
`_outgoing`/`_incoming` adjacency-index dicts for O(1) neighbour lookup — no external
graph database, no network service.

### Node and edge types

Node/edge **types are plain strings, not closed enums** — the specification explicitly
requires the vocabulary to remain extensible and able to preserve source terminology
(e.g. an `EntityRelationship.kind` value is used verbatim as an edge type). `schemas.py`
defines `SUGGESTED_NODE_TYPES` / `SUGGESTED_EDGE_TYPES` frozensets purely for
documentation/tests — they are **not** validated.

Suggested node types include: `application`, `module`, `page`, `page_state`, `actor`,
`permission`, `operation`, `entity`, `entity_state`, `entity_attribute`, `workflow`,
`workflow_step`, `workflow_branch`, `prerequisite`, `trigger`, `outcome`, `transition`,
`derived_output`, `metric`, `counter`, `badge`, `chart`, `report`, `queue`,
`notification`, `alert`, `unknown`, and more.

Suggested edge type families: application structure (`contains`, `appears_on`,
`navigates_to`, ...), actor (`has_permission`, `can_perform`, `owns`, `views`,
`participates_in`, ...), entity (`relates_to`, `has_state`, `assigned_to`, ...), workflow
(`has_step`, `precedes`, `triggered_by`, `requires`, `produces`, `acts_on`,
`performed_by`, `transitions`, `hands_off_to`, ...), dependency (`counts`, `sums`,
`includes_state`, `affects`, `visible_to`, `generated_by`, ...), evidence/uncertainty
(`supported_by`, `contradicted_by`, `equivalent_to`, `alias_of`, `conflicts_with`, ...).

`unknown` nodes preserve unknown-but-meaningful records rather than discarding them.

### Statuses

A **closed**, validated vocabulary shared by nodes and edges:
`observed`, `partially_observed`, `inferred`, `verified`, `candidate`, `contradicted`,
`blocked`, `stale`, `unknown`.

Every source-registry status vocabulary (Entity/Actor/Workflow status strings, or a
`WorkflowStep`'s own distinct `STEP_STATUSES`) is mapped onto this set via
`graph_confidence.status_for_projection()` before it ever reaches a `KnowledgeNode` or
`KnowledgeEdge` — an unmapped raw status would fail Pydantic validation (this is exactly
what the first live-verification bug looked like; see "Live verification findings").

## Identity and deduplication

Node identity is **deterministic**, built from `node_type` + a stable identifier from the
authoritative source record — never fuzzy name matching:

- `entity`/`actor`/`workflow` nodes: keyed by the **canonical-name term string** that
  Entity/Actor/Workflow Discovery already use to cross-reference each other
  (`entity:{normalize_term(name)}`, etc.) — since those registries already reference each
  other by term rather than internal UUID, this gives free, exact cross-registry identity.
- `workflow_step`/`transition`/`outcome`/`prerequisite`/`workflow_branch`/`trigger`/
  `derived_output` nodes: keyed by the source record's own stable UUID (they have no
  term shared across registries).
- `entity_state` nodes: `entity_state:{entity_term}:{state_label}`.
- `permission` nodes: `permission:{actor_term}:{permission_id}`.
- `operation` nodes: `operation:{entity_term}:{verb}`.

Nodes are **never merged based on fuzzy name similarity**. When identity is uncertain,
the graph records an `equivalent_to`/`alias_of` candidate edge or an `unresolved_identity`
gap instead of silently merging.

## Registry synchronisation

`GraphSynchronizer.synchronize()` projects `EntityRegistry` → `ActorRegistry` →
`WorkflowRegistry` → `DependencyRegistry` (in that order, so later stages can resolve
references created earlier in the same pass), then attempts to resolve any still-pending
references, then marks any previously-synced node/edge that wasn't touched this pass as
`stale` (never deleted).

Synchronisation is:

- **Idempotent** — `KnowledgeGraphMemory.upsert_node`/`upsert_edge` diff the incoming
  desired state against the existing record and only bump `observation_count`/
  `last_seen`/`graph_version` when something actually changed (new evidence, a changed
  confidence/status/attribute). Three consecutive `synchronize()` calls against
  unchanged registries leave `graph_version` and every `observation_count` untouched.
- **Incremental** — only registry records seen since the last pass are touched; stale
  marking is scoped per source registry.
- **Non-destructive** — nothing is ever deleted; superseded knowledge becomes `stale`,
  retaining its evidence trail.
- **Traceable** — every synchronisation pass emits a structured
  `knowledge_graph.synchronization` trace event.

## Relationship resolution

`RelationshipResolver` converts a reference (a node id, a canonical name, an alias, or a
bare hint) into an actual node, trying an exact node-id match first, then a normalised
canonical-name/alias match. When the target doesn't exist yet — e.g. a workflow step
names an actor before `ActorRegistry` has confirmed it — the reference is stored as a
`PendingReference` rather than dropped or guessed at. Every synchronisation pass retries
all unresolved references (`try_resolve_pending()`); once a target appears, the pending
edge is created (with its resolution trace) exactly once, never duplicated.

## Bounded inference

Exactly 7 deterministic, explainable rules — no open-ended LLM inference, no arbitrary
business-rule derivation from graph proximity. If a directly-synced edge already covers
what a rule would produce (same deterministic edge id), the rule is a no-op there.

| Rule | Pattern | Produces |
|---|---|---|
| **A** | step `performed_by` actor + workflow `has_step` step | actor `participates_in` workflow |
| **B** | step `acts_on` entity + workflow `has_step` step | workflow `acts_on` entity |
| **C** | actor `has_permission` permission + permission `enables` operation | actor `can_perform` operation |
| **D** | step A `precedes` step B, A's `to_state` == B's `from_state` | `compatible_continuation` |
| **E** | workflow → transition `to_state` state, state `includes_state` output | workflow `affects` output |
| **F** | output `appears_on` page, actor `views` page | output `visible_to` actor |
| **G** | step A `precedes` step B, different `performed_by` actors | actor A `hands_off_to` actor B |

Every inferred edge records, via a paired `GraphInference` record: which rule produced it,
which input node/edge ids it depended on, and a confidence capped at
`min(premise_confidences) * 0.85^(depth-1)` (then adjusted for contradictions/staleness/
corroboration exactly like an observed edge) — never higher than its weakest premise.

## Confidence model

- Node confidence is a clamped pass-through of the source record's confidence.
- Observed edge confidence additionally accounts for contradiction count, staleness, and
  independent-source corroboration.
- Inferred edge confidence is capped at the weakest premise, decayed by inference depth,
  then adjusted the same way.
- `observed` status confidence always outranks `inferred`; `verified` outranks a plain
  label match; duplicate evidence never inflates confidence indefinitely; contradictions
  reduce confidence without deleting the edge; stale relationships have reduced
  confidence but remain queryable.
- Dependency-sourced edges preserve their multiple confidence sub-dimensions
  (relationship / formula / scope / effect-direction / verification confidence) as edge
  attributes rather than collapsing them into one number.

## Contradictions and consistency

`GraphConsistencyChecker.check_all()` runs 8 checks, each producing a `GraphConsistencyIssue`
or `GraphContradiction` — **never** a silent overwrite of either side:

1. Permission conflict — an actor has both `can_perform` and `cannot_perform` the same
   operation under compatible scope.
2. Opposite effect directions — two edges between the same node pair disagree
   (`increase` vs `decrease`) under compatible scope → recorded as a `GraphContradiction`
   and linked from both edges' `contradiction_ids`.
3. Dangling reference — an edge endpoint doesn't exist as a node.
4. Verified-but-contradicted source — a `verified` edge whose source node is
   `contradicted`.
5. Performed despite denial — an actor performed a step whose `acts_on` entity **and**
   verb both match an operation the actor has a verified `lacks_permission` for (both the
   entity *and* the verb must match — see "Live verification findings" below for why).
6. Equivalence conflict — an `equivalent_to` edge between two nodes from the same source
   registry with different authoritative record ids.
7. Stale endpoint — an active edge with a stale endpoint node.
8. Cyclic workflow ordering — a cycle in a workflow's `precedes` chain, detected via DFS.

Contradictions and consistency issues carry `open` / `acknowledged` / `resolved` /
`false_positive` / `stale` status; they are never deleted, only re-evaluated each pass.

## Gap analysis

`GraphGapAnalyzer.analyze()` produces `GraphGap` records across 26 gap types — entity/
actor/workflow/permission/operation completeness gaps, unresolved identity/references,
contradiction-derived gaps, and structural gaps (`isolated_node`, `disconnected_module`,
`stale_subgraph`, `low_confidence_bridge`). Gaps deduplicate by `(gap_type, node_ids)`;
resolved gaps automatically reopen if the same content-key gap reappears later. **Gap
analysis never generates or executes an actual goal** — it only records the gap for a
future engine to decide what to do with.

## Query and traversal

`GraphQueryEngine` is the only way callers touch graph internals: node/edge retrieval by
type/status/confidence/source-registry/canonical-name/alias; ~17 domain-neutral semantic
queries (`actors_for_entity`, `workflows_for_actor`, `permissions_for_actor`,
`producers_for_output`, `consumers_for_output`, `high_risk_gaps`, `isolated_nodes`, ...);
and bounded traversal (`shortest_path`, `all_paths`, `neighbourhood`, `ancestors`,
`descendants`, `reachable_nodes`, `subgraph_for_entity`/`_actor`/`_workflow`/`_output`).

Every traversal accepts a `GraphTraversalConstraint` (`max_depth`, `allowed_node_types`,
`allowed_edge_types`, `min_confidence`, `allowed_statuses`, `required_scope`,
`include_stale`, `include_inferred`, `max_results`) and never walks or returns beyond it
— the concrete mechanism preventing unrestricted transitive closure.

## Context projection

`GraphContextProjector.context_for_node()` (and the `context_for_entity`/`_actor`/
`_workflow`/`_output` aliases) returns a small, bounded bundle for a future Goal
Generation Engine: focus node, bounded neighbours, relationships, an evidence
**summary** (never raw evidence payloads, screenshots, network bodies, or secrets),
contradictions, gaps, unresolved references, and the current `graph_version`.

## Memory and controller integration

- `RunMemory.knowledge_graph: ApplicationKnowledgeGraph` is created once per run in
  `AgentController.__init__` alongside the other discovery engines.
- After each `_run_dependency_engine(...)` call, the controller calls
  `self._run_knowledge_graph_sync()`, which invokes
  `graph.synchronize(entity_registry=..., actor_registry=..., workflow_registry=...,
  dependency_registry=..., iteration=len(memory.actions))` inside a non-fatal
  try/except (a synchronisation failure is logged and exploration continues — it never
  aborts a run).
- `RunMemory` exposes `synchronise_knowledge_graph()`, `knowledge_graph_snapshot()`,
  `knowledge_graph_statistics()`, `knowledge_graph_gaps()`,
  `knowledge_graph_consistency_issues()`, `query_knowledge_graph(query)`, and
  `context_for_node/_entity/_actor/_workflow/_output(**kwargs)` — all degrading to
  `None`/`[]` if no graph is attached.
- `RunMemory.memory_snapshot()` includes a **compact** graph summary (version, node/edge
  counts by type, observed/inferred/contradicted/stale edge counts, unresolved reference
  count, open consistency-issue/gap counts, connected-component/isolated-node counts) —
  never the full graph.

## Versioning

`graph_version` increments **at most once per `synchronize()` call**, and only if
anything actually changed anywhere across sync + inference + consistency + gap analysis
in that pass (`begin_pass()`/`end_pass()` bracket the entire pipeline). Each version bump
appends a `GraphVersion` record (added/updated/stale node/edge ids, resolved reference
ids, new contradiction/gap ids) to `memory.version_history`.
`ApplicationKnowledgeGraph.changes_since(version)` and `diff_snapshots(old, new)` merge
the relevant `GraphVersion` records into one summary — this is intentionally not a full
event-sourcing platform, just enough to answer "what changed since X."

## Serialisation

`GraphSerializer` provides: `compact_snapshot()` (small JSON-safe summary),
`full_snapshot()` (a complete `KnowledgeGraphSnapshot` including statistics), bounded
subgraph export, `to_dot()` (dependency-free Graphviz DOT text generation, bounded to 300
nodes / 500 edges, never a runtime requirement), and a human-readable summary. Passwords,
tokens, cookies, secret headers, and complete network response bodies are never
serialised.

## Observability

Every synchronisation pass emits a structured `knowledge_graph.synchronization` trace
event (via `GEMMAQA_EXPLORATION_TRACE`) summarising graph version, whether it bumped,
inference/consistency counts, and gap count — enough to answer "did this pass change
anything, and why" without needing to inspect the full graph.

## Performance and safety bounds

- All traversal is bounded by `GraphTraversalConstraint.max_depth` and `max_results` —
  there is no unbounded transitive-closure operation anywhere in the query engine.
- `all_paths` is a bounded recursive DFS honouring both `max_depth` and `max_results`.
- Inference confidence decay (`0.85^depth`) discourages deep inference chains from
  appearing artificially confident.
- Cyclic-ordering detection in the consistency checker terminates on any graph shape
  (including graphs with cycles) without infinite recursion.

## Testing

`tests/test_knowledge_graph.py` (109 tests) covers, across dedicated test classes: node
projection, edge projection, synchronisation idempotency/incrementality, identity
resolution (direct/alias/stale/forward-reference), all 7 inference rules, all 8
consistency checks, gap analysis (including reopening and dedup), the full query/
traversal API, context projection, versioning/diffing, and an AST-based
application-neutrality contract test (no hardcoded business vocabulary anywhere in the
package). Six integration fixtures exercise realistic multi-registry scenarios:
single-actor lifecycle, cross-role lifecycle, branched workflow, contradictory evidence,
incomplete knowledge, and scope-sensitive dependency.

## Live verification

The **same, unmodified** implementation was run against three structurally different
applications via `backend/scripts/knowledge_graph_live_capture.py` (a real Playwright
browser through `AgentController` + a deterministic mock LLM provider):

| Application | Nodes | Edges | Inferred edges | Gaps | Consistency issues | Connected components |
|---|---|---|---|---|---|---|
| ServiceFlow | 176 | 546 | — | 42 | 9 | 1 |
| SauceDemo | 72 | 213 | 0 | 25 | 0 | 1 |
| InsightBoard | 53 | 118 | 2 | 36 | 0 | 2 |

No false merges (`equivalent_to`/`alias_of` edges connecting nodes of different types)
were observed on any of the three runs.

### Live verification findings and fixes

Two real, generalisable bugs were found (neither was caught by the 103+ unit tests
authored before live verification, because those fixtures happened to only exercise the
one `WorkflowStep` status value and never a two-different-operations permission-denial
scenario):

1. **Workflow-step status crash.** `WorkflowStep.status` uses a status vocabulary
   (`STEP_STATUSES`, including `"unverified"` and `"completed"`) distinct from the
   graph's closed `GRAPH_STATUSES` set. The synchronizer passed `step.status` through to
   six separate node/edge upserts *unmapped*, crashing Pydantic validation on every
   real-world step whose status was `"unverified"` (the common default) or `"completed"`.
   Because the exception fired mid-pass, `graph_version` never incremented and inference/
   consistency/gap analysis never ran, silently leaving the graph in a corrupt partial
   state (47 nodes / 93 edges / 0 everything else) despite it looking superficially
   populated. **Fix:** `graph_confidence.status_for_projection()` now maps every
   `STEP_STATUSES` value (added `"completed" → "observed"`), and the synchronizer
   computes the mapped status once per step and reuses it for all six related upserts.
   Regression test: `test_every_workflow_step_status_projects_without_crashing`
   (parametrized over 5 step statuses).
2. **Permission-denial false positives.** `_check_performed_despite_denial` matched a
   denied permission's operation against a performed step by **entity alone**, so a
   denial of one operation (e.g. `export`) on an entity falsely flagged *any other*
   step performed on that same entity (e.g. `create`) as a permission violation — 35 of
   43 ServiceFlow consistency issues were this false positive. **Fix:** added
   `_operation_verb_key()` alongside the existing `_operation_entity_key()`, requiring
   both the entity *and* the verb to match before flagging the issue. Regression test:
   `test_denial_of_one_operation_does_not_flag_unrelated_operation`.

After both fixes, re-verification showed ServiceFlow going from
`nodes=47/edges=93/gaps=0/issues=0/graph_version=0` (broken) to
`nodes=176/edges=546/gaps=42/issues=9/graph_version=50` (healthy), with the remaining 9
issues being 8 legitimate `dangling_reference` findings and 1 legitimate
`performed_despite_denial` finding.

### Known upstream limitation (not fixed here, out of scope)

ServiceFlow's remaining 8 `dangling_reference` consistency issues point at entity
identifiers built from raw UUID-like strings that Entity Discovery's
`EntityRelationship.subject_entity_id`/`object_entity_id` occasionally holds instead of a
canonical term. `normalize_term`'s word-splitting regex mangles these into
unrecognisable fragments (e.g. `entity:a e c a- f- b b- b c- bcf f b`), which the
consistency checker correctly flags as **unresolved** rather than fabricating a phantom
node for garbage input. This is a data-quality artifact of the already-completed Entity
Discovery milestone, not a Knowledge Graph bug — per the "fix only generalisable
problems" directive, it is documented here rather than patched in an unrelated,
already-shipped engine.

Separately, on SauceDemo, inference rule C (permission capability) produced 0
`can_perform` edges. This was investigated and is **not a bug**: SauceDemo's only two
positive-evidence permissions (`can_view_menu`, `can_create_to_cart`) don't parse into a
`(verb, entity)` pair matching any entity/operation Entity Discovery actually found (there
is no real "menu" or "to cart" business entity — the permission-id naming scheme itself,
from the already-completed Actor Discovery milestone, is best-effort and doesn't always
decompose cleanly). Both permissions correctly surfaced as `permission_without_operation`
gaps and `PendingReference`s rather than being silently dropped or incorrectly linked.

## Definition of done

- [x] Schemas, storage core, factories, confidence model implemented.
- [x] Registry synchronisation is idempotent, incremental, non-destructive, traceable.
- [x] Relationship resolution handles direct/alias/stale/forward references via pending
      storage.
- [x] Exactly 7 bounded inference rules, each recording rule id + inputs + confidence
      derivation.
- [x] 8 consistency checks; contradictions/gaps retained, never silently overwritten.
- [x] 26 gap types; gap analysis never generates or executes a goal.
- [x] Full bounded query/traversal API + context projection API.
- [x] Wired into `RunMemory` and the controller, non-fatal.
- [x] 109 unit/integration tests + 6 integration fixtures; full suite (887 passed / 1
      skipped / 0 failed) green.
- [x] Live-verified on ServiceFlow, SauceDemo, and InsightBoard with the same
      unmodified implementation; 2 generalisable bugs found and fixed, each with a
      regression test; 1 upstream limitation documented, not patched here.
- [x] This document.

**This milestone does not generate goals, plan scenarios, select QA strategies, or
execute autonomous investigations.** The next recommended milestone is a Goal Generation
Engine consuming this graph's gaps, contradictions, and context projections as its input
boundary.

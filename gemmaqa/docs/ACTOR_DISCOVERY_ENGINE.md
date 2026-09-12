# Autonomous Actor Discovery Engine

GemmaQA automatically discovers every actor participating in an application
— any human or system identity that owns permissions, creates/modifies data,
approves workflows, or consumes information. Application-neutral by
construction: no actor/role name (no "Admin", "Manager", "Driver", ...)
exists as a string literal anywhere in this package's code, enforced by an
AST-walking contract test (see [Testing](#testing)). Every name comes from
the target application's own observed text.

**Status:** implemented, wired into the observation loop
(`app/agent/controller.py`), best-effort/non-fatal, live-verified against
three real applications.

## Architecture

```
app/intelligence/actor_discovery/
    schemas.py                    ActorRecord, ActorEvidence, PermissionCandidate,
                                   RoleRelationship, ActorSession, ActorCandidate
    actor_candidate_builder.py     CanonicalPageModel -> raw actor/role candidate terms
                                    + page-context classification (settings/roles/
                                    permissions/profile/dashboard, access-denied text,
                                    invite/approval context, assignment targets)
    actor_classifier.py            candidates -> per-page findings; determines which
                                    known actor this page's SESSION evidence belongs to
    permission_discovery.py        infers can_<verb>[_<noun>] permissions from visible/
                                    disabled controls, page reachability, network 401/403,
                                    access-denied text
    role_relationships.py          co-assignment + permission-superset hierarchy +
                                    actor difference analysis (compare_actors)
    actor_memory.py                merge findings into persistent ActorRecords,
                                    confidence/status/session-richness scoring
    actor_registry.py              the queryable catalog (Planner-facing) + session store
    actor_discovery_engine.py      orchestrator + entity cross-reference + tracing
```

Sits alongside `app/intelligence/entity_discovery/` (same package family, same
guardrails) and **reuses** its generic linguistic primitives
(`normalize_term`, `singularize`, `split_verb_and_noun`, `url_path_terms`,
`OPERATION_VERBS`, `HTTP_METHOD_OPERATIONS`, `NON_ENTITY_OPERATIONS`) rather
than duplicating a second verb lexicon — permission inference needs exactly
the same "what verb is this" logic entity discovery already built.

## Pipeline (`ActorDiscoveryEngine.observe`)

Called once per observation, given the fresh `CanonicalPageModel` plus
lightweight session context (`authenticated: bool`, `login_method: str | None`):

1. **`ActorCandidateBuilder.build()`** — extracts candidate role/actor terms:
   role dropdown options, role-table-column cell values (splitting
   comma/slash-separated multi-role cells), role-ish nav items/breadcrumbs
   (by hint word, or by matching an **already-known** role term — mirrors
   `entity_relationship_builder`'s `known_terms` pattern). Also classifies
   page context (settings/user-management/roles/permissions/profile/
   dashboard) and detects access-denied text, invite/approval context, and
   assignment-dialog targets.
2. **`PermissionDiscovery.discover()`** — infers permissions for the current
   page (see [Permission discovery](#permission-discovery)).
3. **`ActorMemory.merge_discovery()`** — merges named-role findings AND
   session-level evidence (pages/navigation/dashboards/permissions/login
   method) into the registry (see [Memory](#memory--merging--session-richness)).
4. **Entity cross-reference** — if an `EntityRegistry` is supplied, any
   entity whose `related_pages` includes this page's URL is added to the
   session actor's `known_entities`.
5. **`RoleRelationshipBuilder`** — co-assignment findings from this page,
   plus a full hierarchy re-comparison across all known actors.
6. **Trace** — one `actor_discovery.observation` event (see
   [Tracing](#tracing)).

## Evidence sources

| Source | `ActorEvidence.source_kind` |
|---|---|
| Navigation/sidebar/header items (role-hinted, or matching an already-known role) | `navigation_item` |
| Breadcrumbs | `breadcrumb` |
| Settings pages | `settings_page` |
| User-management pages | `user_management_page` |
| Roles pages | `roles_page` |
| Permissions pages | `permissions_page` |
| Role `<select>` options | `role_dropdown_option` |
| Role-column table cells | `role_table_cell` |
| User-creation forms | `user_creation_form` |
| Invite dialogs | `invite_dialog` |
| Assignment dialogs | `assignment_dialog` |
| Approval dialogs | `approval_dialog` |
| Workflow ownership | `workflow_ownership` |
| Network 401/403 | `network_401` / `network_403` |
| JWT claims *(schema-ready, not yet populated — see [Known limitations](#known-limitations))* | `jwt_claim` |
| Authentication responses *(schema-ready, not yet populated)* | `auth_response` |
| Profile menu / account pages | `profile_menu` / `account_page` |
| Session info (the session-actor's own richness trail) | `session_info` |
| Access-denied page text | `access_denied_page` |
| Entity Registry cross-reference | `entity_registry` |
| Visible/disabled controls (permission evidence, not actor-naming evidence) | `visible_control` / `disabled_control` |

## The "current session" placeholder

**Most applications never display their own role name anywhere in the
UI** — confirmed live on all three test applications (ServiceFlow,
InsightBoard, SauceDemo: none show "You are logged in as: X"). Session-level
evidence (pages visited, navigation seen, dashboards reached, permissions
inferred, login method) still needs somewhere to accumulate even when no
role NAME is ever observed — `"current session"` is that structural bucket,
never a business role name. If a profile/account page names **exactly one**
role-ish term, that term is used instead of the placeholder (best-effort —
see [Known limitations](#known-limitations)).

## Permission discovery

No permission name is hardcoded as a business capability list. Every
permission id is a `can_<verb>[_<noun>]` pattern built from entity
discovery's generic verb lexicon. Four evidence signals, tracked as
**positive** and **negative** evidence separately so standing is always
explainable (`PermissionCandidate.granted` = more positive than negative):

1. **Visible + enabled control** → positive (`Export` button visible →
   `can_export`).
2. **Visible + disabled control** → negative (`Delete Customer` greyed out →
   `can_delete_customer` currently gated).
3. **Page reachability** — merely reaching a settings/user-management/
   roles/permissions page is itself capability evidence
   (`can_access_settings`, `can_manage_users`, `can_manage_roles`,
   `can_manage_permissions`).
4. **Network 401/403** and **generic "access denied" page text** → negative
   evidence for whatever operation/topic was being attempted.

`NON_ENTITY_OPERATIONS` (Save/Cancel/Submit/Apply/Reset/Refresh — reused
from entity discovery) are excluded from becoming permissions: every form in
every application has these controls; recording them would report the same
meaningless capability for every actor.

## Role discovery

- **Role dropdowns**: any `<select>`-like field whose label/name hints at a
  role ("Role", "User Type", "Access Level", "Permission Level", ...),
  mined for its options. Placeholder options ("-- Select a role --", "None")
  are rejected after stripping decorative punctuation.
- **Role tables**: a table with a role-hinted column header; each sample
  row's cell value is a candidate. A cell listing multiple roles
  ("Manager, Support") is split and each becomes its own candidate —
  **multi-role assignment** evidence (see below).
- **Hierarchy** (`role_relationships.infer_hierarchy`): once two actors each
  have at least 2 observed permissions, a **strict superset** of granted
  permissions infers `parent_of`/`inherits_from` — purely comparative,
  never a guess from names ("Admin" is not assumed senior to "Viewer" by
  name alone).
- **Multi-role assignment** (`co_assigned_with`): two role terms observed in
  the **same table cell** are recorded as co-assigned, symmetrically.
- **System/default/guest/temporary roles**: `role_modifier_flags()` sets
  `is_guest`/`is_system`/`is_default`/`is_temporary` from words observed
  **directly in the role's own text** ("Guest User", "System Admin",
  "Temporary Access") — generic English descriptors of a role slot's
  nature, never a business identity name in themselves.

## Actor difference analysis

`role_relationships.compare_actors(a, b)` is a pure comparison — navigation,
dashboards, pages, permissions, entities, and visible-region differences
between any two known actors. Works on actors discovered any way (a live
multi-session run, a Users-table listing several roles, or test fixtures).
`ActorRegistry.all_pairwise_differences()` runs it across every pair of
`known_actors()`. **A single-role application naturally reports zero
differences** — there is nothing to compare against — this is correct
behavior, not a gap (live-verified: all three test applications, having only
one actually-authenticated actor each, report no differences; the
comparison logic itself is unit-tested directly with multi-actor fixtures).

## Session management (infrastructure only — no auto-switching)

`ActorSession` (per actor): `credential_profile_id`, `login_method`,
`landing_page`, `dashboard_url`, `known_permission_ids`, `last_explored_url`,
`last_state_fingerprint`. `ActorRegistry.session_for(term)` /
`.all_sessions()` support returning to any known actor's last state later —
**per the task's explicit scope, nothing in this phase triggers automatic
switching**; this is storage and lookup only.

## Memory & merging & session richness

`ActorMemory` merges every observation's findings — same term always merges
into the same record, evidence/aliases/pages/navigation/dashboards/
permissions accumulate and are deduped, nothing is discarded.

**Two things this build got wrong on the first pass, found by live testing
against real applications, and fixed** (see
[Live findings](#live-findings-bugs-found-and-fixed-by-live-testing)):
confidence/status for the (extremely common) unnamed session actor, and
richness-aware confidence scoring.

`status_for()`: an actually-**observed** session (anonymous or
authenticated — both are legitimate actors) is judged by session richness:
`incomplete` until it has some permission or dashboard evidence, then
`confirmed`. A passively-named role term (dropdown/table/nav only, never
actually browsed as) needs ≥2 distinct evidence *kinds* to reach
`unverified`, otherwise stays `candidate`.

## Actor Registry — Planner queries

- `known_actors()` — confirmed + incomplete.
- `unknown_actors()` — candidate role terms, not yet corroborated.
- `unverified_actors()` — a role NAME well-evidenced but never actually
  observed as a session.
- `actors_requiring_exploration()` — incomplete, then unverified, then
  candidate (most-promising-first).
- `actors_missing_permissions()` — known actors with no (or only
  low-confidence) permission evidence.
- `actors_missing_dashboard_understanding()` — known actors who never
  reached a dashboard-classified page.

All six are also exposed on `RunMemory` (`known_actors()`,
`unknown_actors()`, `unverified_actors()`, `actors_requiring_exploration()`,
`actors_missing_permissions()`, `actors_missing_dashboard_understanding()`),
degrading to `[]` with no registry, plus a compact actor summary folded into
`memory_snapshot()`.

## Tracing

One `actor_discovery.observation` event per observation
(`app.utils.exploration_trace`): `session_actor`, `created`/`updated` terms,
`new_relationships`, `navigation_differences` (full pairwise diff list),
per-touched-actor detail (`status`, `confidence`, `aliases`,
`permission_evidence`, and a `why` list naming the actual observed text
behind the discovery), and running registry totals.

## Testing

`tests/test_actor_discovery.py` (48 tests): hint-based classification, role
dropdown/table extraction (including multi-role cells and placeholder
rejection), invite/approval/assignment-context detection, permission
inference across all four evidence signals, memory merging (including
regression tests for both live-found bugs), confidence corroboration,
role relationships (co-assignment, hierarchy-by-permission-superset, no
false hierarchy from insufficient or equal evidence), actor difference
analysis (single-role → no differences, multi-role → real differences),
the full six-method registry query API (including `RunMemory` pass-through
and no-registry degradation), session infrastructure (capture + independent
multi-actor storage, no switching), **two structurally unrelated synthetic
applications** (a field-service app with a role dropdown + permission page;
a logistics app with 401 evidence + a different dashboard) proven with the
*same* code, an AST-based contract test (no business role name as a string
literal in this package's actual code), and one live Playwright integration
test.

**Live-verified** against all three applications from
[`EVIDENCE_DRIVEN_EXPLORATION_REPORT.md`](./EVIDENCE_DRIVEN_EXPLORATION_REPORT.md)
with the identical, unmodified engine — every one reached `confirmed` status
with real, differentiated confidence and real permission/dashboard/entity
data:

- **ServiceFlow**: `current session` confirmed (confidence 0.63), landing
  page and dashboard both `/dashboard`, `login` method, permissions
  including `can_access_settings`/`can_create_customer`, 14 cross-referenced
  known entities (customer, job, ...).
- **InsightBoard**: `current session` confirmed (confidence 0.41) as an
  **anonymous** session (no login exists) — proving guest/anonymous actors
  are discovered as legitimate actors, not excluded for lacking auth.
- **SauceDemo**: `current session` confirmed (confidence 0.35), `login`
  method, correctly picked up `can_create_to_cart`/`can_view_menu`.

## Live findings (bugs found and fixed by live testing)

1. **Session actor stuck at `candidate`/confidence 0.0 forever.** The
   "current session" placeholder — used by every application that never
   displays its own role name (the common case, confirmed on all three test
   apps) — never received `role_dropdown_option`-style evidence, so
   confidence (computed only from `supporting_evidence`) stayed at exactly
   0.0 and status never left `candidate`, regardless of how many real pages/
   permissions/dashboards it accumulated. Fixed by judging session actors on
   session richness (pages/permissions/dashboards) instead, and by pushing a
   `session_info` evidence entry into `supporting_evidence` on every session
   merge. Regression:
   `test_session_actor_reaches_confirmed_without_any_named_role_evidence`.
2. **"home"/"landing" in `DASHBOARD_HINTS` was far too broad.** A plain page
   titled "Home" got misclassified as a dashboard, which — via
   page-reachability permission inference — silently promoted a thin,
   just-loaded session straight to `confirmed`. Fixed by narrowing
   `DASHBOARD_HINTS` to `dashboard`/`overview` only. Regression:
   `test_authenticated_session_with_no_permissions_or_dashboards_is_incomplete`.
3. **Placeholder dropdown options not stripped of decorative punctuation.**
   `"-- Select a role --"` (common real markup) wasn't recognized as a
   placeholder because the leading/trailing dashes weren't stripped before
   the placeholder-text comparison, producing a spurious `"select a role"`
   candidate actor. Fixed with a punctuation-stripping pass before the
   placeholder check. Regression: `test_placeholder_dropdown_options_are_rejected`.
4. **Bare role names in navigation were invisible.** A nav item that IS a
   role name but carries no role-ish hint word of its own ("Manager",
   unlike "Manage Roles") was never recognized — even once "manager" was
   already a well-established candidate from a role dropdown elsewhere.
   Fixed by threading `known_role_terms` through the candidate builder
   (mirroring `entity_relationship_builder`'s `known_terms` pattern): a nav
   item/breadcrumb matching an **already-known** role term now counts as
   corroborating evidence. Regression:
   `test_bare_role_name_in_navigation_only_counted_once_already_known`.
5. **Confidence for well-explored session actors understated how much was
   actually known.** All three live applications initially reported
   confidence ≈0.1–0.13 for a `confirmed` actor with real permissions,
   dashboards, and cross-referenced entities — because confidence only ever
   counted distinct evidence **kinds**, and a session bucket only ever has
   one kind (`session_info`). Fixed with `session_richness_bonus()` — an
   explicit, transparent bonus for having permissions/dashboards/entities/
   multiple pages, recomputed after entity cross-reference too (which
   happens after the main merge). Post-fix, live confidence rose to a
   realistic 0.35–0.63 across all three apps. Regression:
   `test_session_confidence_reflects_richness_not_just_evidence_kind_count`.
6. **Anonymous sessions couldn't reach `confirmed`.** InsightBoard has no
   authentication at all; its rich, fully-explored anonymous session stayed
   capped at `candidate` purely for lacking a login — even though a guest/
   anonymous visitor is exactly the kind of actor the task asks this engine
   to discover. Fixed by judging "was this a real, observed session" on
   `known_session_types` + `known_pages` generally, not specifically
   `"authenticated" in known_session_types`. Regression:
   `test_anonymous_session_is_a_legitimate_actor_too`.

## Known limitations

- **JWT claims and API response bodies are not capturable today.** GemmaQA's
  `NetworkMonitor` only records **failed** (4xx/5xx) requests and never
  headers or bodies for any request (confirmed by direct inspection before
  building this engine). The `jwt_claim`/`auth_response` evidence kinds
  exist in the schema for forward compatibility, but nothing populates them
  yet — this would need new `NetworkMonitor` plumbing (capturing 2xx
  responses too, extracting/scrubbing headers and bodies) that is out of
  scope here. What **is** real and live-verified: 401/403 status capture,
  which already flows into permission discovery today.
- **The "profile page names my own role" heuristic is best-effort.** It
  requires the page to be profile-classified AND to have named **exactly
  one** role-ish term; a profile page that also links to unrelated
  role-management content could misattribute. No application encountered in
  live testing exercised this path (none display their own role name), so it
  remains unit-tested but not live-proven.
- **No automatic actor/session switching** — explicitly out of scope per the
  task; only storage/lookup infrastructure (`ActorSession`,
  `ActorRegistry.session_for`/`.all_sessions()`) exists.
- **Single-session runs rarely observe multiple actors.** Actor difference
  analysis and hierarchy inference are fully implemented and unit-tested
  with multi-actor fixtures, but no live run in this phase actually
  authenticated as more than one identity — a real multi-role application
  with a second credential profile would be needed to live-prove
  cross-actor comparison end-to-end.
- **English-only linguistics**, inherited from the shared entity-discovery
  primitives this engine reuses.
- **Markup-dependent**, like the Perception Engine and Entity Discovery
  Engine it builds on — an application with no semantic role
  dropdowns/tables/pages yields fewer, weaker candidates (though the
  session-richness fix means the *session actor itself* is still fully
  discoverable regardless).

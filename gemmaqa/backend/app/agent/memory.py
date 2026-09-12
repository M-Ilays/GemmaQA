"""In-memory working memory for an autonomous QA run."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from app.agent.goals import ExplorationGoal
from app.application.coverage import compute_coverage as compute_app_coverage
from app.application.store import ApplicationStore
from app.application.url_normalize import normalize_url, same_origin
from app.schemas import (
    ActionResult,
    BugAnalysisResult,
    BugClassification,
    CoverageRecord,
    Defect,
    EvidenceItem,
    FormDescriptor,
    ModuleRecord,
    PageState,
    ProductOverview,
    RoleObservation,
    RunConfiguration,
    TableDescriptor,
    TestExecution,
    TestScenario,
    Workflow,
    WorkflowStep,
)
from app.utils.ids import new_id


# How many consecutive stalled/looping stop-checks an in-progress
# authentication may excuse before the run reports the auth state as the
# problem. A real login is a few steps; anything longer is a misdiagnosis.
MAX_AUTH_STALL_CHECKS = 5


def action_signature(
    *,
    page_fingerprint: str | None,
    action_type: str,
    element_id: str | None,
    value_category: str | None = None,
) -> str:
    return "|".join(
        [
            page_fingerprint or "",
            action_type,
            element_id or "",
            value_category or "",
        ]
    )


@dataclass
class NavigationEdge:
    source_url: str
    target_url: str
    via_action: str
    element_id: str | None = None
    action_label: str = ""


@dataclass
class ObservationNote:
    note_id: str
    title: str
    detail: str
    page_url: str | None = None
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class RunMemory:
    """Accumulates discoveries, budgets, and repetition counters for a run."""

    run_id: str
    start_url: str
    configuration: RunConfiguration | None = None

    product_overview: ProductOverview | None = None
    domain_hypothesis: str = ""
    domain_purpose: str = ""

    pages: list[PageState] = field(default_factory=list)
    page_inventory: dict[str, PageState] = field(default_factory=dict)
    visited_urls: set[str] = field(default_factory=set)
    page_fingerprints: set[str] = field(default_factory=set)
    unexplored_urls: list[str] = field(default_factory=list)

    modules: list[ModuleRecord] = field(default_factory=list)
    navigation_edges: list[NavigationEdge] = field(default_factory=list)
    forms: list[FormDescriptor] = field(default_factory=list)
    tables: list[TableDescriptor] = field(default_factory=list)
    known_modals: set[str] = field(default_factory=set)
    known_tabs: set[str] = field(default_factory=set)
    known_form_ids: set[str] = field(default_factory=set)
    known_table_ids: set[str] = field(default_factory=set)
    # Distinct RecordCollection element_ids seen across the whole run (via the
    # Canonical Page Model) -- mirrors known_table_ids/known_form_ids, but for
    # perception's universal grid/collection view. See remember_collections().
    known_collection_ids: set[str] = field(default_factory=set)

    workflows: list[Workflow] = field(default_factory=list)
    roles: list[RoleObservation] = field(default_factory=list)

    actions: list[ActionResult] = field(default_factory=list)
    failed_actions: list[ActionResult] = field(default_factory=list)
    action_signatures: set[str] = field(default_factory=set)
    signature_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    failed_per_element: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    url_visit_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    modal_toggle_streak: int = 0
    last_modal_fingerprint: str | None = None

    scenarios: list[TestScenario] = field(default_factory=list)
    executions: list[TestExecution] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)

    bugs: list[Defect] = field(default_factory=list)
    suspected_bugs: list[BugAnalysisResult] = field(default_factory=list)
    observations: list[ObservationNote] = field(default_factory=list)

    user_journeys: list[str] = field(default_factory=list)
    regression_checklist: list[str] = field(default_factory=list)
    mermaid_diagrams: list[str] = field(default_factory=list)
    doc_sections: dict[str, str] = field(default_factory=dict)

    remaining_action_budget: int = 50
    remaining_page_budget: int = 20
    no_progress_streak: int = 0
    stop_reason: str | None = None
    decisions_validated: int = 0
    decisions_rejected: int = 0
    browser_adapter_id: str = "direct_playwright"
    provider_type: str = "mock"
    adapter_connection_status: str = "unknown"
    adapter_capabilities: dict[str, bool] = field(default_factory=dict)
    adapter_execution_failures: int = 0
    unsupported_evidence_features: list[str] = field(default_factory=list)

    # Hrefs the safety validator has already rejected as out-of-scope (e.g. external
    # social-media links). Element IDs are re-numbered on every page load, so dedup by
    # the actual target URL instead — otherwise the same known-blocked link gets
    # proposed and re-blocked again on every single page that happens to repeat it.
    blocked_hrefs: set[str] = field(default_factory=set)

    # Authentication / frontier (non-secret)
    authenticated: bool = False
    auth_status: str = "unknown"
    auth_method: str | None = None
    auth_blocker: str | None = None
    auth_checkpoint_url: str | None = None
    auth_signals: list[str] = field(default_factory=list)
    anonymous_page_count: int = 0
    authenticated_page_count: int = 0
    frontier_candidate_count: int = 0
    auth_strategy: Any = field(default=None, repr=False)

    current_workflow_steps: list[WorkflowStep] = field(default_factory=list)
    workflow_start_url: str | None = None

    # First-class exploration goals (app.agent.goals) — the planner selects a goal
    # before picking a candidate for it; this is the durable record of that lifecycle.
    goals: list[ExplorationGoal] = field(default_factory=list)

    # Latest ApplicationStore-derived exploration gaps (app.application.gaps),
    # recomputed each planning iteration by app.agent.goals.sync_gap_goals — this is
    # what feeds goals from the store's own model, not just the current page.
    gaps: list[Any] = field(default_factory=list)

    # How many times should_stop() has recovered from an apparent loop/stall by
    # deprioritizing the current goal instead of stopping outright (see should_stop).
    # Capped so a run that's genuinely, persistently stuck still stops eventually.
    loop_recovery_count: int = 0
    _max_loop_recoveries: int = 3
    # Consecutive stalled/looping stop-checks waved through because
    # authentication reported itself still in progress. See `should_stop`.
    auth_stall_checks: int = 0
    # Labels of navigation items this run has already followed. A sidebar item
    # appears on EVERY page, so its per-page action signature resets constantly;
    # its label does not. See `note_navigation_taken`.
    navigation_labels_taken: set[str] = field(default_factory=set)

    # Count of successful safe-write completions this run (safe_test_data_create
    # clicks, generic form-workflow submissions) — the generic signal
    # app.agent.prerequisites uses for "some entity/cart item now exists".
    safe_writes_completed: int = 0

    # Canonical application structure (single source of truth for docs/UI/APIs)
    app_store: ApplicationStore | None = field(default=None, repr=False)

    # Live generic safe-form-fill workflow (app.agent.form_workflow), independent of
    # AuthenticationStrategy.active_workflow — at most one in progress at a time.
    active_form_workflow: Any = field(default=None, repr=False)

    # form_id -> failure count. A form whose workflow ended failed/blocked must not
    # immediately get a brand-new workflow started for it again — without this, a
    # form that can never actually converge (e.g. its real submit control still
    # isn't correctly resolved) produces an outer infinite start->fail->restart loop,
    # each bounded individually by GenericFormWorkflow's own step budget but with no
    # cap across attempts.
    failed_form_workflow_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Signatures (app.agent.form_identity) of forms whose workflow has reached a
    # terminal state — succeeded OR not. Keyed by signature rather than
    # `form_id` because `form_id` is positional and a successful submit usually
    # renumbers it; and it records SUCCESS as well as failure because every
    # existing guard counted only failures, so a form whose submit the
    # application kept accepting was eligible to start again forever.
    finished_form_signatures: set[str] = field(default_factory=set, repr=False)

    # Latest Universal Page Perception Engine output (app.perception.engine),
    # refreshed on every successful observation. FrontierBuilder is the primary
    # consumer for non-authentication exploration candidates (app.agent.frontier).
    # Bounded, secret-free record of how model context was built on each
    # planning iteration (page source, sections reduced/omitted, evidence
    # counts, rejected evidence ids, estimated tokens). Reporting reads this;
    # nothing in the decision path does. See RunMemory.record_model_context.
    model_context_events: list[dict[str, Any]] = field(default_factory=list, repr=False)

    # What the application did with each form submission — accepted, refused
    # with a 4xx, failed with a 5xx, or unproven. See
    # app.agent.submission_outcome; a refused write must never be reported as a
    # completed one.
    submission_outcomes: list[dict[str, Any]] = field(default_factory=list, repr=False)

    # Work the application offered and GemmaQA was structurally ready to do, but
    # was not PERMITTED to do — keyed by the configuration flag that would have
    # allowed it. A live run stopped after 27 of its 480 allowed actions with
    # `all_safe_candidates_exhausted` because every remaining candidate was a
    # write and the write flags were off; nothing said so, so the run read as
    # "GemmaQA gave up" rather than "GemmaQA was not allowed". See
    # `note_write_candidate_suppressed` / `write_permission_gap`.
    suppressed_write_candidates: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    # form_id -> the field labels that name records the application does not hold.
    unsatisfied_form_references: dict[str, list[str]] = field(default_factory=dict, repr=False)

    canonical_page_model: Any = field(default=None, repr=False)
    # The PageState.state_fingerprint that was current at the exact moment
    # `canonical_page_model` was captured (set alongside it in
    # controller.py._run_perception_engine). CanonicalPageModel computes its
    # OWN, richer fingerprint (app.perception.state_builder) — a deliberately
    # different algorithm/input set than PageObserver's `compute_fingerprint`,
    # so the two fingerprints are NEVER expected to be equal even for the same
    # instant. FrontierBuilder must compare against THIS marker (the same
    # PageObserver-fingerprint scheme as the current PageState), never against
    # `canonical_page_model.state_fingerprint` directly, to detect a stale
    # model left over from a previous iteration (e.g. because perception
    # failed this round and the field was never cleared).
    canonical_page_model_source_fingerprint: str | None = field(default=None, repr=False)

    # Entity Discovery Engine registry (app.intelligence.entity_discovery) —
    # accumulated across every observation of the run. `Any`-typed to avoid a
    # memory.py -> intelligence dependency; populated by
    # controller.py._run_perception_engine, queried via the entity_* methods
    # below (the Planner-facing API).
    entity_registry: Any = field(default=None, repr=False)

    # Actor Discovery Engine registry (app.intelligence.actor_discovery) —
    # same pattern as entity_registry: accumulated across the run, `Any`-typed
    # to avoid a memory.py -> intelligence dependency, queried via the
    # actor_*/actors_* methods below.
    actor_registry: Any = field(default=None, repr=False)

    # Workflow Discovery Engine registry (app.intelligence.workflow_discovery)
    # — same pattern as entity_registry/actor_registry: accumulated across
    # the run, `Any`-typed to avoid a memory.py -> intelligence dependency,
    # queried via the workflow_*/workflows_* methods below. Populated from
    # controller.py's post-action hook (not _run_perception_engine — workflow
    # discovery needs a before/after CanonicalPageModel pair plus the action,
    # which a single observation can't provide).
    workflow_registry: Any = field(default=None, repr=False)

    # Business Dependency Discovery Engine registry
    # (app.intelligence.dependency_discovery) — same pattern as
    # workflow_registry: accumulated across the run, `Any`-typed, populated
    # from the same before/after post-action hook (dependency discovery
    # needs the same before/after CanonicalPageModel pair plus the executed
    # action that workflow discovery needs, for before/after correlation).
    dependency_registry: Any = field(default=None, repr=False)

    # CRUD Discovery Engine registry (app.intelligence.crud_discovery) —
    # same before/after-pair pattern as workflow_registry/dependency_registry,
    # populated from the same post-action hook. Holds CRUDWorkflowHypothesis
    # records (create/edit/delete entry points and, once corroborated by a
    # before/after pair, "supported" hypotheses) — discovery only, never an
    # executed action itself.
    crud_registry: Any = field(default=None, repr=False)

    # Adaptive Application Understanding Engine
    # (app.intelligence.adaptive_understanding) — judges whether each
    # observation is trustworthy enough to reason from, and what kind of
    # screen it shows, from observable evidence only (never the URL, never a
    # framework signature). Attached unconditionally by the controller: it
    # replaces an assumption every run previously made silently, so there is
    # no feature to opt into. `Any`-typed for the same reason as the other
    # engine fields — to avoid a memory.py -> intelligence import.
    understanding_engine: Any = field(default=None, repr=False)

    # Temporary Record Registry (app.agent.temporary_record_registry) — the
    # run-scoped store of every record GemmaQA itself created via
    # GenericFormWorkflow, and the sole authority for what's eligible for
    # cleanup. `Any`-typed to avoid a memory.py -> agent.temporary_record_
    # registry import-order dependency, same pattern as every other registry
    # field above. Instantiated in controller.py's setup (needs `run_id`).
    temporary_record_registry: Any = field(default=None, repr=False)

    # Application Knowledge Graph (app.intelligence.knowledge_graph) — a
    # semantic PROJECTION of the four registries above, never a second
    # application-memory system. `Any`-typed to avoid a memory.py ->
    # intelligence dependency; populated from the same post-action hook,
    # synchronised AFTER entity/actor/workflow/dependency registries update
    # each iteration. Queried via the knowledge_graph_*/context_for_*
    # methods below.
    knowledge_graph: Any = field(default=None, repr=False)

    # Goal Generation Engine (app.intelligence.goal_generation) -- the first
    # reasoning engine that CONSUMES the Knowledge Graph rather than
    # projecting into it. `Any`-typed for the same reason as
    # `knowledge_graph`. Decides WHAT to investigate next (evidence-backed,
    # prioritised `InvestigationGoal` records); never executes a browser
    # action, never plans a scenario. Synchronised AFTER the knowledge graph
    # each iteration via `generate_goals()`.
    goal_engine: Any = field(default=None, repr=False)

    # Scenario Planning Engine (app.intelligence.scenario_planning) --
    # converts each current InvestigationGoal into one or more declarative,
    # browser-independent `InvestigationScenario` records. Decides HOW a
    # goal COULD be investigated; never executes a browser action, never
    # switches actors, never mutates application state. Synchronised AFTER
    # goal generation each iteration via `generate_scenarios()`.
    scenario_engine: Any = field(default=None, repr=False)

    # QA Strategy Engine (app.intelligence.qa_strategy) -- consumes the
    # current InvestigationScenario/InvestigationGoal records and decides
    # WHICH scenarios should execute, in what order, and why. Never
    # executes a browser action, never switches actors, never mutates
    # application state, never replaces the runtime Planner. Synchronised
    # AFTER scenario planning each iteration via `generate_strategy()`.
    strategy_engine: Any = field(default=None, repr=False)

    # Autonomous Investigation Engine (app.intelligence.autonomous_investigation)
    # -- the execution brain. Selects the next executable scenario from QA
    # Strategy's queues and drives it through the SAME runtime Planner ->
    # SafetyValidator -> ActionExecutor -> BrowserAdapter pipeline every
    # other action already uses; never a raw browser call, never a
    # replacement for any of those. Strictly opt-in: `None` unless the run
    # explicitly enabled it (`RunConfiguration.enable_autonomous_
    # investigation`), so every other run's behaviour is unaffected.
    investigation_engine: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.app_store is None:
            self.app_store = ApplicationStore(self.run_id, self.start_url)

    def bootstrap_budgets(self, config: RunConfiguration) -> None:
        self.configuration = config
        # max_actions / max_pages are no longer stop limits. Remaining-*
        # fields stay as leftover context numbers and are not decremented.

    def remember_page(self, page: PageState, *, explored: bool = False) -> bool:
        """Record an observed page via the shared canonical upsert.

        Defaults to discovered (not explored). A simple revisit increments
        visit_count only. Pass explored=True after a meaningful action.
        """
        is_new = False
        canonical = normalize_url(page.url, base_url=self.start_url) or page.url
        # Keep PageState.url aligned with canonical for inventories
        if canonical and page.url != canonical:
            page.url = canonical

        if self.app_store:
            app_page = self.app_store.upsert_page(page, explored=explored)
            if app_page is None:
                # External — do not count as application page
                return False
            self.modules = self.app_store.sync_legacy_modules()
            if self.app_store.model.purpose and self.app_store.model.purpose_confidence >= 0.45:
                self.domain_purpose = self.app_store.model.purpose
                self.domain_hypothesis = self.app_store.model.inferred_domain or self.domain_hypothesis

        fp = page.state_fingerprint or canonical
        if canonical not in self.visited_urls:
            self.visited_urls.add(canonical)
            is_new = True
        if fp not in self.page_fingerprints:
            self.page_fingerprints.add(fp)
            is_new = True

        self.page_inventory[canonical] = page
        replaced = False
        for i, existing in enumerate(self.pages):
            existing_c = normalize_url(existing.url, base_url=self.start_url) or existing.url
            if existing_c == canonical:
                self.pages[i] = page
                replaced = True
                break
        if not replaced:
            self.pages.append(page)

        self.url_visit_counts[canonical] += 1
        self.unexplored_urls = [
            u
            for u in self.unexplored_urls
            if (normalize_url(u, base_url=self.start_url) or u) != canonical
        ]

        from app.application.url_normalize import is_external_doc_or_social

        allowed = self.app_store.model.allowed_origin if self.app_store else ""
        for el in page.interactive_elements:
            if el.tag == "a" and el.href:
                href = el.href
                if href.startswith("#") or href.lower().startswith("javascript:"):
                    continue
                abs_url = href if href.startswith("http") else urljoin(canonical, href)
                abs_c = normalize_url(abs_url, base_url=canonical) or abs_url
                if not abs_c or is_external_doc_or_social(abs_c):
                    if self.app_store and abs_c:
                        self.app_store.record_external_link(
                            abs_c,
                            label=el.accessible_name or el.text or abs_c,
                            category="docs",
                        )
                    continue
                if allowed and not same_origin(abs_c, allowed):
                    if self.app_store:
                        self.app_store.record_external_link(
                            abs_c,
                            label=el.accessible_name or el.text or abs_c,
                            category="external",
                        )
                    continue
                if abs_c not in self.visited_urls and abs_c not in self.unexplored_urls:
                    self.unexplored_urls.append(abs_c)
                    if self.app_store:
                        self.app_store.register_candidate_url(abs_c)

        if self.app_store:
            self.unexplored_urls = self.app_store.filter_unexplored(self.unexplored_urls)
        # Final safety: never keep external/docs hosts as exploration targets
        self.unexplored_urls = [
            u
            for u in self.unexplored_urls
            if u and not is_external_doc_or_social(u)
        ]

        for form in page.forms:
            if form.form_id not in self.known_form_ids:
                self.known_form_ids.add(form.form_id)
                self.forms.append(form)
                is_new = True
        for table in page.tables:
            if table.table_id not in self.known_table_ids:
                self.known_table_ids.add(table.table_id)
                self.tables.append(table)
                is_new = True
        for modal in page.modals or page.dialogs:
            if modal and modal not in self.known_modals:
                self.known_modals.add(modal)
                is_new = True
        for tab in page.tabs:
            if tab and tab not in self.known_tabs:
                self.known_tabs.add(tab)
                is_new = True

        return is_new

    def remember_edge(
        self,
        source_url: str,
        target_url: str,
        via_action: str,
        element_id: str | None = None,
        action_label: str = "",
    ) -> None:
        src = normalize_url(source_url, base_url=self.start_url) or source_url
        dst = normalize_url(target_url, base_url=self.start_url) or target_url
        if not src or not dst or src == dst:
            return
        label = (action_label or "").strip() or via_action
        # Deduplicate legacy edges (increment occurs in app_store)
        for edge in self.navigation_edges:
            if (
                edge.source_url == src
                and edge.target_url == dst
                and edge.via_action == via_action
                and (edge.action_label or "").lower() == label.lower()
            ):
                if self.app_store:
                    self.app_store.record_navigation(
                        from_url=src,
                        to_url=dst,
                        action_type=via_action,
                        action_label=label,
                        element_id=element_id,
                    )
                return
        self.navigation_edges.append(
            NavigationEdge(
                source_url=src,
                target_url=dst,
                via_action=via_action,
                element_id=element_id,
                action_label=label,
            )
        )
        if self.app_store:
            self.app_store.record_navigation(
                from_url=src,
                to_url=dst,
                action_type=via_action,
                action_label=label,
                element_id=element_id,
            )

    def remember_blocked_href(self, href: str | None) -> None:
        if href:
            self.blocked_hrefs.add(href.strip())

    def is_href_blocked(self, href: str | None) -> bool:
        return bool(href) and href.strip() in self.blocked_hrefs

    def has_seen_signature(self, signature: str) -> bool:
        return signature in self.action_signatures

    def mark_signature(self, signature: str) -> None:
        self.action_signatures.add(signature)
        self.signature_counts[signature] += 1

    def remember_action(
        self,
        result: ActionResult,
        *,
        before_fingerprint: str | None,
        value_category: str | None = None,
        made_progress: bool,
    ) -> None:
        self.actions.append(result)
        self.note_navigation_taken(result.action)

        sig = action_signature(
            page_fingerprint=before_fingerprint,
            action_type=result.action.action.value,
            element_id=result.action.element_id,
            value_category=value_category,
        )
        self.mark_signature(sig)

        if not result.success:
            self.failed_actions.append(result)
            if result.action.element_id:
                self.failed_per_element[result.action.element_id] += 1

        if made_progress:
            self.no_progress_streak = 0
        else:
            self.no_progress_streak += 1

        # Track form inspection / testing on the canonical store
        action_name = result.action.action.value
        if self.app_store and result.success:
            if action_name == "inspect_form" and result.action.element_id:
                self.app_store.mark_form_inspected(result.action.element_id)
            elif action_name in {"fill", "select", "check", "type", "clear"}:
                self.app_store.mark_form_tested(
                    page_url=result.after_url or result.before_url,
                    element_id=result.action.element_id,
                )

        # Meaningful successful actions contribute to the workflow catalog
        if result.success and self._is_workflow_worthy(result):
            self._append_workflow_step(result)

        if result.before_url and result.after_url and result.before_url != result.after_url:
            label = self._action_label_from_result(result)
            self.remember_edge(
                result.before_url,
                result.after_url,
                result.action.action.value,
                result.action.element_id,
                action_label=label,
            )
            if self.app_store:
                # Both the page left and the page arrived at were exercised
                self.app_store.mark_explored(result.before_url)
                self.app_store.mark_explored(result.after_url)
        elif result.success and action_name in {"inspect_form", "inspect_table", "fill", "select"}:
            if self.app_store:
                self.app_store.mark_explored(result.after_url or result.before_url or "")

    def _action_label_from_result(self, result: ActionResult) -> str:
        meta = result.action.metadata or {}
        for key in ("action_label", "label", "name", "text", "accessible_name"):
            if meta.get(key):
                return str(meta[key]).strip()
        reason = result.action.reason or ""
        for prefix in (
            "Exploring navigation control:",
            "Exploring unvisited link:",
            "Click navigation:",
            "Open ",
            "Click ",
        ):
            if prefix.lower() in reason.lower():
                idx = reason.lower().find(prefix.lower())
                extracted = reason[idx + len(prefix) :].strip(" :\"'")
                if extracted:
                    return extracted.split(".")[0].strip() or result.action.action.value
        # Resolve label from last known page elements
        el_id = result.action.element_id
        if el_id:
            for page in reversed(self.pages):
                for el in page.interactive_elements or []:
                    if el.element_id == el_id:
                        text = (
                            el.accessible_name
                            or el.visible_text
                            or el.text
                            or el.label
                            or el.name
                            or ""
                        )
                        if text:
                            return str(text).strip()
        return result.action.action.value

    def note_modal_state(self, modals: list[str]) -> None:
        fp = "|".join(sorted(modals)) if modals else ""
        if self.last_modal_fingerprint and fp != self.last_modal_fingerprint:
            # oscillating between two modal states
            if self.modal_toggle_streak >= 0:
                self.modal_toggle_streak += 1
        else:
            self.modal_toggle_streak = 0
        self.last_modal_fingerprint = fp

    @staticmethod
    def _is_record_edit_cycle(urls: set[str]) -> bool:
        """Detail ↔ edit of the same record is the update workflow, not a loop."""
        if len(urls) != 2:
            return False
        a, b = sorted(u.lower() for u in urls)
        pairs = (
            ("contactdetails", "editcontact"),
            ("/detail", "/edit"),
            ("/show", "/edit"),
            ("details", "edit"),
        )
        return any((x in a and y in b) or (x in b and y in a) for x, y in pairs)

    def detect_navigation_loop(self) -> bool:
        """True if the last few URL transitions bounce between two distinct pages."""
        recent_urls = [a.after_url for a in self.actions[-6:] if a.after_url]
        if len(recent_urls) < 4:
            return False
        unique = set(recent_urls[-4:])
        # Require two distinct URLs — repeated fills/clicks on one page are not a loop
        if len(unique) == 2 and len(recent_urls[-4:]) == 4:
            if self._is_record_edit_cycle(unique):
                return False
            # A-B-A-B pattern
            return recent_urls[-1] == recent_urls[-3] and recent_urls[-2] == recent_urls[-4]
        return False

    def detect_modal_loop(self) -> bool:
        return self.modal_toggle_streak >= 4

    def element_failed_too_often(self, element_id: str | None, limit: int = 3) -> bool:
        if not element_id:
            return False
        return self.failed_per_element.get(element_id, 0) >= limit

    def should_stop(
        self,
        no_progress_limit: int = 5,
        *,
        build_frontier: Callable[[], list[Any]] | None = None,
    ) -> tuple[bool, str | None]:
        """Decide whether the run should stop, and why.

        Reasons are the canonical, truthful vocabulary the rest of the system relies
        on (exploration_complete, all_safe_candidates_exhausted,
        unresolved_authentication, persistent_navigation_loop, ...) — never a raw
        free-text string. A navigation loop or no-progress streak never stops the run
        immediately: it first checks whether any frontier candidate, pending goal, or
        unexplored URL still offers a way forward (via `build_frontier`, supplied by the
        caller since building one needs the current page state). If an alternative
        exists, this backtracks — deprioritizing whatever goal produced the repetition —
        and lets the run continue instead of quitting. Only a loop that persists with
        genuinely nothing else left, or a runtime/safety hard limit, actually stops the run;
        having no unseen controls on the CURRENT page alone is never sufficient.
        """
        stalled = self.no_progress_streak >= no_progress_limit
        looping = self.detect_navigation_loop() or self.detect_modal_loop()
        if not stalled and not looping:
            return False, None

        # Do not treat a stall/loop as any kind of resolution while authentication is
        # still actively being worked or genuinely blocked — surface the real state
        # rather than a misleading stall/loop label.
        if not self.authenticated:
            if self.auth_blocker:
                return True, "unresolved_authentication"
            if self.auth_status in {
                "detected",
                "credentials_required",
                "ready",
                "filling",
                "submitted",
                "rejected",
                "session_expired",
            }:
                # Bounded, not unconditional. This exemption exists so a genuine
                # multi-step login is not mistaken for a stall — but it used to
                # hold forever, which meant a misdiagnosed auth state disabled
                # loop detection for the whole run. A live run spent 170 actions
                # repeating one three-action cycle against an admin form it had
                # mistaken for a login screen, across four pages, and only the
                # operator's cancel ended it.
                #
                # Authentication is a handful of steps. If it has not converged
                # after this many consecutive stalled/looping checks, the auth
                # state is the thing that is wrong, and the run must be allowed
                # to say so.
                self.auth_stall_checks += 1
                if self.auth_stall_checks <= MAX_AUTH_STALL_CHECKS:
                    return False, None
                return True, "unresolved_authentication"

        available_candidates: list[Any] = []
        if build_frontier is not None:
            try:
                available_candidates = [
                    c for c in build_frontier() if getattr(c, "status", "available") not in {"exhausted", "blocked"}
                ]
            except Exception:
                available_candidates = []
        pending_goals = [g for g in self.goals if g.status in {"proposed", "active", "deferred"}]
        has_alternative = bool(available_candidates or pending_goals or self.unexplored_urls)

        if has_alternative and self.loop_recovery_count < self._max_loop_recoveries:
            self.loop_recovery_count += 1
            active_goal = next((g for g in self.goals if g.status == "active"), None)
            if active_goal is not None:
                active_goal.status = "deferred"
                active_goal.priority += 500
                active_goal.evidence.append(
                    "Deprioritized after a navigation/no-progress loop was detected; "
                    "backtracking to a different goal instead of stopping."
                )
            self.no_progress_streak = 0
            return False, None

        if looping:
            return True, "persistent_navigation_loop"
        return True, "all_safe_candidates_exhausted"

    def sync_auth_public(self) -> None:
        auth = self.auth_strategy
        if auth is None:
            return
        public = auth.public_status()
        self.authenticated = bool(public.get("authenticated"))
        self.auth_status = str(public.get("auth_status") or "unknown")
        self.auth_method = public.get("auth_method")
        self.auth_blocker = public.get("auth_blocker")
        self.auth_checkpoint_url = public.get("checkpoint_url")
        self.auth_signals = list(public.get("auth_signals") or [])
        self.anonymous_page_count = int(public.get("anonymous_pages") or 0)
        self.authenticated_page_count = int(public.get("authenticated_pages") or 0)

    def remember_bug(self, bug: Defect) -> None:
        key = (bug.title, bug.page_url)
        existing = {(b.title, b.page_url) for b in self.bugs}
        if key not in existing:
            self.bugs.append(bug)

    def remember_bug_analysis(self, analysis: BugAnalysisResult, page_url: str | None = None) -> None:
        if analysis.classification == BugClassification.NO_DEFECT:
            # "I looked and there is nothing to report" is not a note to keep.
            # It already fell through to no-op below, but only by accident of
            # branch ordering — stated here so it survives an edit.
            return
        if analysis.classification == BugClassification.OBSERVATION:
            if analysis.title:
                self.observations.append(
                    ObservationNote(
                        note_id=new_id(),
                        title=analysis.title,
                        detail=analysis.actual_result or analysis.business_impact,
                        page_url=page_url,
                        evidence_ids=list(analysis.evidence_ids),
                    )
                )
            return
        if analysis.classification == BugClassification.SUSPECTED_BUG:
            self.suspected_bugs.append(analysis)
        # confirmed / suspected with title also mirrored into defects by bug_analyzer

    def remember_evidence(self, items: list[EvidenceItem]) -> None:
        existing = {e.evidence_id for e in self.evidence}
        for item in items:
            if item.evidence_id not in existing:
                self.evidence.append(item)
            if self.app_store:
                from pathlib import Path

                from app.config import get_settings

                root = get_settings().run_evidence_dir(self.run_id)
                try:
                    rel = str(Path(item.path).resolve().relative_to(root.resolve())).replace(
                        "\\", "/"
                    )
                except Exception:
                    rel = Path(item.path).name
                self.app_store.add_evidence(
                    evidence_id=item.evidence_id,
                    kind=item.kind,
                    relative_path=rel,
                    description=item.description or "",
                    absolute_path=item.path,
                )

    def set_modules(self, modules: list[ModuleRecord]) -> None:
        # Prefer canonical store projection; explorer list is merged into store via observe_page
        if self.app_store and self.app_store.model.modules:
            self.modules = self.app_store.sync_legacy_modules()
        else:
            self.modules = modules

    def note_unexplored_url(self, url: str) -> None:
        """Track a same-origin candidate URL as discovered but not yet visited."""
        canonical = normalize_url(url, base_url=self.start_url) or url
        if not canonical:
            return
        if self.app_store:
            if not same_origin(canonical, self.app_store.model.allowed_origin):
                return
            self.app_store.register_candidate_url(canonical)
        if canonical not in self.visited_urls and canonical not in self.unexplored_urls:
            self.unexplored_urls.append(canonical)

    def _page_journey_label(self, page) -> str:
        """Disambiguate duplicate titles (e.g. Contact List App on / and /login)."""
        path = (getattr(page, "normalized_path", None) or "").strip() or "/"
        title = (getattr(page, "heading", None) or getattr(page, "title", None) or "").strip()
        if title and path and path not in {"", "/"}:
            return f"{title} ({path})"
        return title or path or getattr(page, "canonical_url", "") or "page"

    def rebuild_journeys(self) -> list[str]:
        """Build user journeys from labeled navigation edges."""
        journeys: list[str] = []
        if self.app_store and self.app_store.model.navigation_edges:
            for e in self.app_store.model.navigation_edges:
                src = self.app_store.model.page_by_id(e.from_page_id)
                dst = self.app_store.model.page_by_id(e.to_page_id)
                if not src or not dst:
                    continue
                src_l = self._page_journey_label(src)
                dst_l = self._page_journey_label(dst)
                label = e.action_label or e.action_type
                journeys.append(f"{src_l} -- {label} --> {dst_l}")
        else:
            for e in self.navigation_edges:
                label = e.action_label or e.via_action
                journeys.append(f"{e.source_url} -- {label} --> {e.target_url}")
        self.user_journeys = list(dict.fromkeys(journeys))
        return self.user_journeys

    def coverage(self) -> CoverageRecord:
        # app_store is always constructed in __post_init__ — there is exactly one
        # coverage implementation; ReportBuilder.compute_coverage delegates here
        # rather than keeping a second, divergent calculation.
        self.app_store.prune_stub_pages()
        for u in list(self.unexplored_urls):
            self.app_store.register_candidate_url(u)
        budget_total = max(len(self.actions), 1)
        store_scenarios = self.app_store.model.scenarios
        if store_scenarios:
            generated = len(store_scenarios)
            executed_n = sum(
                1
                for s in store_scenarios
                if s.execution_status.value not in {"not_run", "scheduled"}
            )
            passed_n = sum(
                1 for s in store_scenarios if s.execution_status.value == "passed"
            )
            failed_n = sum(
                1 for s in store_scenarios if s.execution_status.value == "failed"
            )
        else:
            generated = len(self.scenarios)
            executed_list = [
                e for e in self.executions if e.status not in {"not_run", "scheduled"}
            ]
            executed_n = len(executed_list)
            passed_n = len([e for e in executed_list if e.status == "passed"])
            failed_n = len([e for e in executed_list if e.status == "failed"])
        # Prefer finalized workflows; else count actions as workflow steps
        wf_count = len(self.workflows) or (
            1 if any(a.success for a in self.actions) else 0
        )
        active_wf = self.active_form_workflow

        goals_generated = (
            self.goal_engine.statistics().total_goals if self.goal_engine is not None else 0
        )
        scenario_statistics = self.scenario_engine.statistics() if self.scenario_engine is not None else None

        # Collections "inspected" = collections GemmaQA actually reasoned about
        # (produced a CRUD hypothesis beyond entry_point, or a temporary record
        # was tied to it), never merely rendered on a visited page.
        collections_inspected = 0
        if self.known_collection_ids:
            inspected_ids: set[str] = set()
            if self.crud_registry is not None:
                for h in self.crud_registry.all_hypotheses():
                    cid = getattr(h, "collection_id", None)
                    if cid and getattr(h, "status", "") != "entry_point":
                        inspected_ids.add(cid)
            if self.temporary_record_registry is not None:
                for entry in self.temporary_record_registry.entries.values():
                    if entry.collection_element_id:
                        inspected_ids.add(entry.collection_element_id)
            collections_inspected = len(inspected_ids & self.known_collection_ids)

        # Local (non-navigating) controls: interactive elements with no href,
        # across every page observed this run, vs. how many were actually acted on.
        local_control_ids: set[str] = set()
        for p in self.pages:
            for el in p.interactive_elements:
                if el.element_id and not el.href:
                    local_control_ids.add(el.element_id)
        exercised_ids = {
            a.action.element_id
            for a in self.actions
            if a.action.element_id and a.action.element_id in local_control_ids
        }

        cleanup_counts: dict[str, int] | None = None
        if self.temporary_record_registry is not None:
            snapshot = self.temporary_record_registry.snapshot()
            by_state = snapshot.get("by_state", {})
            cleanup_counts = {
                "pending": len(self.temporary_record_registry.records_pending_cleanup()),
                "succeeded": by_state.get("absence_verified", 0),
                "deleted": by_state.get("deleted", 0),
                "failed": by_state.get("cleanup_failed", 0),
                "manual_required": by_state.get("manual_cleanup_required", 0),
            }

        return compute_app_coverage(
            self.app_store.model,
            actions_taken=len(
                [a for a in self.actions if a.action.action.value != "finish"]
            ),
            action_budget=budget_total,
            tables_discovered=len(self.known_table_ids) or sum(len(p.tables) for p in self.pages),
            tables_inspected=len(
                {
                    a.action.element_id
                    for a in self.actions
                    if a.action.action.value == "inspect_table" and a.action.element_id
                }
            ),
            workflows_identified=wf_count,
            bugs_found=len(self.bugs),
            suspected_issues=len(self.suspected_bugs),
            observations=len(self.observations),
            tests_generated=generated,
            tests_executed=executed_n,
            passed=passed_n,
            failed=failed_n,
            goals=[g.to_dict() for g in self.goals],
            gaps=[gap.to_dict() for gap in self.gaps],
            safe_writes_completed=self.safe_writes_completed,
            active_form_workflow_state=(active_wf.state if active_wf is not None else None),
            frontier_candidate_count=self.frontier_candidate_count,
            attempted_candidate_count=len(self.action_signatures),
            distinct_state_count=len(self.page_fingerprints),
            roles_observed=len(self.roles) if self.roles else None,
            crud_hypotheses=(self.crud_registry.all_hypotheses() if self.crud_registry is not None else None),
            investigation_statistics=(
                self.investigation_engine.statistics() if self.investigation_engine is not None else None
            ),
            goals_generated=goals_generated,
            scenario_statistics=scenario_statistics,
            collections_discovered=len(self.known_collection_ids),
            collections_inspected=collections_inspected,
            local_controls_discovered=len(local_control_ids),
            local_controls_exercised=len(exercised_ids),
            cleanup_counts=cleanup_counts,
        )

    def finalize_workflow(self, name: str | None = None) -> Workflow | None:
        # Already finalized and no new steps — do not re-synthesize a duplicate journey
        if len(self.current_workflow_steps) < 1:
            if self.workflows:
                self.rebuild_journeys()
                return None
            if self.actions:
                for result in self.actions:
                    if self._is_workflow_worthy(result):
                        self._append_workflow_step(result)
        if len(self.current_workflow_steps) < 1:
            self.current_workflow_steps = []
            return None
        page_ids: list[str] = []
        if self.app_store:
            for p in self.app_store.model.visited_pages():
                page_ids.append(p.id)
        wf = Workflow(
            workflow_id=new_id(),
            name=name or f"Exploratory journey from {self.workflow_start_url or self.start_url}",
            description="Recorded from executed browser actions",
            starting_page=self.workflow_start_url or self.start_url,
            preconditions=["Authorized URL accessible"],
            steps=list(self.current_workflow_steps),
            mermaid=self._workflow_mermaid(self.current_workflow_steps),
            page_ids=page_ids,
        )
        self.workflows.append(wf)
        self.current_workflow_steps = []
        self.rebuild_journeys()
        return wf

    def _is_workflow_worthy(self, result: ActionResult) -> bool:
        """Keep workflow catalog focused on navigation / inspection, not field clicks."""
        if not result.success:
            return False
        action_name = result.action.action.value
        if action_name in {"finish", "wait", "screenshot", "hover", "press"}:
            return False
        if action_name in {"inspect_form", "inspect_table", "open_url", "fill", "select", "check"}:
            return True
        if action_name != "click":
            return False
        el_id = result.action.element_id
        if el_id:
            for page in reversed(self.pages):
                for el in page.interactive_elements or []:
                    if el.element_id != el_id:
                        continue
                    cat = (el.category or "").lower()
                    tag = (el.tag or "").lower()
                    input_type = (el.input_type or getattr(el, "type", None) or "").lower()
                    if cat in {"input", "textarea", "select"} or tag in {
                        "input",
                        "textarea",
                        "select",
                    }:
                        if input_type not in {"button", "submit"} and tag != "button":
                            return False
                    text = " ".join(
                        filter(
                            None,
                            [
                                el.accessible_name,
                                el.visible_text,
                                el.text,
                                el.name,
                            ],
                        )
                    ).lower()
                    if input_type == "submit" or text.strip() in {"submit", "save"}:
                        # Blind submit clicks are not meaningful journey steps
                        return False
                    return True
        # Fallback: keep labeled navigation clicks
        label = self._action_label_from_result(result).lower()
        if label in {"click", "submit", "email", "password", "first name", "last name"}:
            return False
        return True

    def _append_workflow_step(self, result: ActionResult) -> None:
        if not self.current_workflow_steps:
            self.workflow_start_url = result.before_url
        label = self._action_label_from_result(result)
        desc = label if label != result.action.action.value else (result.action.reason or result.action.action.value)
        step = WorkflowStep(
            step_id=new_id(),
            order=len(self.current_workflow_steps) + 1,
            action=result.action.action.value,
            description=str(desc)[:240],
            expected=result.action.expected_result,
            page_url=result.after_url,
            element_id=result.action.element_id,
            evidence_ids=list(result.evidence_ids),
        )
        self.current_workflow_steps.append(step)

    def _workflow_mermaid(self, steps: list[WorkflowStep]) -> str:
        lines = ["flowchart LR", "  S[Start]"]
        prev = "S"
        for idx, step in enumerate(steps[:20]):
            node = f"W{idx}"
            label = (step.action or "step").replace('"', "'")[:28]
            lines.append(f'  {node}["{label}"]')
            lines.append(f"  {prev} --> {node}")
            prev = node
        lines.append(f"  {prev} --> E[End]")
        return "\n".join(lines)

    def previous_actions_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "action": r.action.action.value,
                "element_id": r.action.element_id,
                "success": r.success,
                "url": r.after_url,
                "reason": r.action.reason,
                "label": self._action_label_from_result(r),
                "category": r.action.category.value if r.action.category else None,
            }
            for r in self.actions
        ]

    def record_submission_outcome(self, form_id: str, outcome: Any) -> None:
        """Record what the application did with one form submission.

        Kept so the report can state plainly how many write attempts the
        application ACCEPTED versus refused. Before this existed, a refused
        submit was indistinguishable from a completed one in the run record.
        """
        if outcome is None:
            return
        try:
            entry = outcome.to_dict()
        except Exception:  # pragma: no cover - defensive
            return
        entry["form_id"] = form_id
        self.submission_outcomes.append(entry)
        if len(self.submission_outcomes) > 100:
            del self.submission_outcomes[:-100]

    def note_form_workflow_finished(self, workflow: Any) -> None:
        """This run has finished with this form — whatever the outcome.

        Records SUCCESS as well as failure, which is the whole point. Every
        earlier loop guard counted only failures, so a form whose submissions
        the application kept ACCEPTING stayed eligible to start again forever:
        observed live on OrangeHRM's Buzz newsfeed at 20 accepted posts in 44
        actions, each one a brand-new workflow, until the operator cancelled the
        run. Nothing was wrong from the workflow's point of view — that was
        exactly the problem.

        Keyed by `form_signature`, never `form_id`; see `app.agent.form_identity`
        for why the id cannot be trusted here.
        """
        signature = (getattr(workflow, "form_signature", "") or "").strip()
        if signature:
            self.finished_form_signatures.add(signature)

    def has_finished_form(self, signature: str) -> bool:
        return bool(signature) and signature in self.finished_form_signatures

    def note_unsatisfied_references(self, form_id: str, labels: list[str]) -> None:
        """A form could not be completed because a field names a record the
        application does not hold.

        Kept on the run, not only on the form workflow, because the workflow is
        discarded the moment it blocks — and this is precisely the kind of
        finding that must outlive it. "The form was not tested" and "the form
        cannot be tested until an employee exists" are different reports, and
        only the second tells the operator what to do.
        """
        if not labels:
            return
        known = self.unsatisfied_form_references.setdefault(form_id, [])
        for label in labels:
            if label and label not in known:
                known.append(label)

    def note_write_candidate_suppressed(
        self, *, required_flag: str, capability: str, evidence: str
    ) -> None:
        """Record that the application offered `capability` and the ONLY reason
        GemmaQA did not pursue it was the `required_flag` policy setting.

        Called from the frontier at the exact points where a permission flag —
        not missing structure, not a failed attempt — is what stops a candidate
        being emitted. That distinction is the whole value: "there was nothing
        to do" and "there was plenty to do and I was not allowed to" are
        different findings, and only one of them is the operator's to fix.
        """
        entry = self.suppressed_write_candidates.setdefault(
            required_flag, {"required_flag": required_flag, "capabilities": [], "occurrences": 0, "examples": []}
        )
        entry["occurrences"] = int(entry["occurrences"]) + 1
        if capability not in entry["capabilities"]:
            entry["capabilities"].append(capability)
        if evidence and evidence not in entry["examples"] and len(entry["examples"]) < 5:
            entry["examples"].append(evidence)

    def write_permission_gap(self) -> dict[str, Any]:
        """What this run could have exercised had it been permitted to.

        Empty when nothing was suppressed — an empty dict means "policy did not
        limit this run", never "not measured".
        """
        if not self.suppressed_write_candidates:
            return {}
        blocked = sorted(
            self.suppressed_write_candidates.values(),
            key=lambda e: (-int(e["occurrences"]), str(e["required_flag"])),
        )
        capabilities = sorted({c for e in blocked for c in e["capabilities"]})
        flags = [str(e["required_flag"]) for e in blocked]
        return {
            "missing_permissions": flags,
            "blocked_capabilities": capabilities,
            "blocked": blocked,
            "explanation": (
                "The application offered "
                + ", ".join(capabilities)
                + " and GemmaQA was structurally ready to exercise "
                + ("it" if len(capabilities) == 1 else "them")
                + ", but this run's configuration did not permit it. Enable "
                + ", ".join(flags)
                + " to test this."
            ),
        }

    def submission_outcome_summary(self) -> dict[str, Any]:
        """Accepted / refused / unproven write attempts, with evidence."""
        if not self.submission_outcomes:
            return {}
        by_outcome: dict[str, int] = {}
        for entry in self.submission_outcomes:
            key = str(entry.get("outcome") or "unknown")
            by_outcome[key] = by_outcome.get(key, 0) + 1
        refused = [e for e in self.submission_outcomes if str(e.get("outcome", "")).startswith("rejected")]
        return {
            "total_submissions": len(self.submission_outcomes),
            "by_outcome": by_outcome,
            "accepted": by_outcome.get("accepted", 0),
            "refused": len(refused),
            "unproven": by_outcome.get("unknown", 0),
            "refusals": [
                {
                    "form_id": e.get("form_id"),
                    "outcome": e.get("outcome"),
                    "http_status": e.get("http_status"),
                    "messages": e.get("messages") or [],
                    "signals": e.get("signals") or [],
                }
                for e in refused[:10]
            ],
        }

    @property
    def testing_objective(self) -> str | None:
        """The operator's objective for this run, or None when none was given.

        Derived from `configuration` rather than stored separately — one source
        of truth, so it cannot drift from what the run was actually configured
        with.
        """
        return getattr(self.configuration, "testing_objective", None) if self.configuration else None

    # Keep the tail only: this is diagnostics, and a 500-action run should not
    # grow an unbounded parallel history alongside the real one.
    MAX_MODEL_CONTEXT_EVENTS = 50

    def record_model_context(self, metadata: dict[str, Any] | None) -> None:
        """Record one planning iteration's context-construction metadata."""
        if not metadata:
            return
        self.model_context_events.append(dict(metadata))
        if len(self.model_context_events) > self.MAX_MODEL_CONTEXT_EVENTS:
            del self.model_context_events[: -self.MAX_MODEL_CONTEXT_EVENTS]

    def model_context_summary(self) -> dict[str, Any]:
        """Aggregate view for the final report. Counts and names only."""
        events = self.model_context_events
        if not events:
            return {}
        latest = events[-1]
        page_sources: dict[str, int] = {}
        decision_paths: dict[str, int] = {}
        reduced: dict[str, int] = {}
        omitted: dict[str, int] = {}
        rejected_total = 0
        for event in events:
            path = str(event.get("decision_path") or "unknown")
            decision_paths[path] = decision_paths.get(path, 0) + 1
            # Only model-driven iterations have a page projection; counting an
            # "unknown" source for a deterministic one would be misleading.
            if event.get("page_source"):
                source = str(event["page_source"])
                page_sources[source] = page_sources.get(source, 0) + 1
            for name in event.get("reduced_sections") or []:
                reduced[str(name)] = reduced.get(str(name), 0) + 1
            for name in event.get("omitted_sections") or []:
                omitted[str(name)] = omitted.get(str(name), 0) + 1
            rejected_total += int(event.get("rejected_evidence_ids") or 0)
        return {
            "planning_iterations_recorded": len(events),
            "decision_path_counts": decision_paths,
            "model_calls_recorded": decision_paths.get("model", 0),
            "page_source_counts": page_sources,
            "sections_reduced_counts": reduced,
            "sections_omitted_counts": omitted,
            "invalid_evidence_ids_rejected": rejected_total,
            "reduction_applied_count": sum(1 for e in events if e.get("reduction_applied")),
            "latest": {
                key: latest.get(key)
                for key in (
                    "page_source",
                    "estimated_tokens",
                    "prompt_chars",
                    "evidence_items",
                    "gaps_included",
                    "graph_nodes_projected",
                    "graph_edges_projected",
                    "engine_outputs_considered",
                    "images_attached",
                    "provider",
                    "decision_path",
                    "real_prompt_construction_exercised",
                )
                if key in latest
            },
        }

    def memory_snapshot(self) -> dict[str, Any]:
        snap = {
            "visited_urls": list(self.visited_urls)[:30],
            "modules": [m.name for m in self.modules],
            "forms_known": len(self.known_form_ids),
            "tables_known": len(self.known_table_ids),
            "workflows": len(self.workflows),
            "bugs": len(self.bugs),
            "suspected_bugs": len(self.suspected_bugs),
            "remaining_action_budget": self.remaining_action_budget,
            "remaining_page_budget": self.remaining_page_budget,
            "no_progress_streak": self.no_progress_streak,
            "authenticated": self.authenticated,
            "auth_status": self.auth_status,
            "auth_method": self.auth_method,
            "auth_blocker": self.auth_blocker,
            "frontier_candidate_count": self.frontier_candidate_count,
            "blocked_hrefs": list(self.blocked_hrefs),
            "active_goal": next(
                (g.to_dict() for g in self.goals if g.status == "active"), None
            ),
            "goal_counts": {
                status: sum(1 for g in self.goals if g.status == status)
                for status in ("proposed", "active", "completed", "blocked", "deferred", "abandoned")
                if any(g.status == status for g in self.goals)
            },
            "gap_counts": {
                gap_type: sum(1 for gap in self.gaps if gap.gap_type == gap_type)
                for gap_type in {gap.gap_type for gap in self.gaps}
            },
        }
        # Credential flags only — never raw secrets
        if self.auth_strategy is not None:
            snap.update(self.auth_strategy.vault.public_flags())
        # Compact discovered-entity picture (names/status/operations only —
        # never raw evidence bodies) so the LLM's planning context knows what
        # the application is ABOUT without any application-specific code.
        if self.entity_registry is not None:
            try:
                snap["entities"] = {
                    "known": [r.canonical_name for r in self.entity_registry.known_entities()][:15],
                    "incomplete": [r.canonical_name for r in self.entity_registry.incomplete_entities()][:10],
                    "candidates": [r.canonical_name for r in self.entity_registry.unknown_entities()][:10],
                }
            except Exception:
                pass
        if self.actor_registry is not None:
            try:
                snap["actors"] = {
                    "known": [r.canonical_name for r in self.actor_registry.known_actors()][:15],
                    "unverified": [r.canonical_name for r in self.actor_registry.unverified_actors()][:10],
                    "candidates": [r.canonical_name for r in self.actor_registry.unknown_actors()][:10],
                }
            except Exception:
                pass
        if self.workflow_registry is not None:
            try:
                snap["workflows"] = {
                    "known": [w.canonical_name for w in self.workflow_registry.known_workflows()][:15],
                    "incomplete": [w.canonical_name for w in self.workflow_registry.incomplete_workflows()][:10],
                    "cross_role": [w.canonical_name for w in self.workflow_registry.cross_role_workflows()][:10],
                    "gap_count": len(self.workflow_registry.workflow_gaps()),
                }
            except Exception:
                pass
        if self.dependency_registry is not None:
            try:
                snap["dependencies"] = {
                    "known": [d.canonical_name for d in self.dependency_registry.known_dependencies()][:15],
                    "unresolved": [d.canonical_name for d in self.dependency_registry.unresolved_dependencies()][:10],
                    "cross_role": [d.canonical_name for d in self.dependency_registry.cross_role_dependencies()][:10],
                    "gap_count": len(self.dependency_registry.dependency_gaps()),
                }
            except Exception:
                pass
        if self.knowledge_graph is not None:
            try:
                stats = self.knowledge_graph.statistics()
                snap["knowledge_graph"] = {
                    "graph_version": stats.graph_version,
                    "total_nodes": stats.total_nodes,
                    "total_edges": stats.total_edges,
                    "node_counts_by_type": dict(stats.node_counts_by_type),
                    "edge_counts_by_type": dict(stats.edge_counts_by_type),
                    "observed_edge_count": stats.observed_edge_count,
                    "inferred_edge_count": stats.inferred_edge_count,
                    "contradicted_edge_count": stats.contradicted_edge_count,
                    "stale_edge_count": stats.stale_edge_count,
                    "unresolved_reference_count": stats.unresolved_reference_count,
                    "consistency_issue_count": stats.consistency_issue_count,
                    "gap_count": stats.gap_count,
                    "connected_component_count": stats.connected_component_count,
                    "isolated_node_count": stats.isolated_node_count,
                }
            except Exception:
                pass
        if self.goal_engine is not None:
            try:
                stats = self.goal_engine.statistics()
                top = self.goal_engine.highest_priority_goals(limit=5)
                snap["goal_generation"] = {
                    "total_goals": stats.total_goals,
                    "goals_by_type": dict(stats.goals_by_type),
                    "goals_by_status": dict(stats.goals_by_status),
                    "average_priority": round(stats.average_priority, 3),
                    "high_priority_count": stats.high_priority_count,
                    "blocked_count": stats.blocked_count,
                    "group_count": stats.group_count,
                    "dependency_count": stats.dependency_count,
                    "top_goals": [{"goal_id": g.goal_id, "goal_type": g.goal_type, "priority_score": round(g.priority_score, 3)} for g in top],
                }
            except Exception:
                pass
        if self.scenario_engine is not None:
            try:
                stats = self.scenario_engine.statistics()
                snap["scenario_planning"] = {
                    "total_scenarios": stats.total_scenarios,
                    "scenarios_by_type": dict(stats.scenarios_by_type),
                    "scenarios_by_status": dict(stats.scenarios_by_status),
                    "scenarios_by_feasibility": dict(stats.scenarios_by_feasibility),
                    "scenarios_by_risk": dict(stats.scenarios_by_risk),
                    "read_only_count": stats.read_only_count,
                    "mutating_count": stats.mutating_count,
                    "cross_actor_count": stats.cross_actor_count,
                    "high_risk_count": stats.high_risk_count,
                    "average_complexity_score": round(stats.average_complexity_score, 3),
                    "average_confidence_gain": round(stats.average_confidence_gain, 3),
                    "dependency_count": stats.dependency_count,
                    "conflict_count": stats.conflict_count,
                    "gap_count": stats.gap_count,
                    "scenario_plan_version": stats.scenario_plan_version,
                }
            except Exception:
                pass
        if self.strategy_engine is not None:
            try:
                stats = self.strategy_engine.statistics()
                snap["qa_strategy"] = {
                    "total_candidates": stats.total_candidates,
                    "candidates_by_queue": dict(stats.candidates_by_queue),
                    "candidates_by_action": dict(stats.candidates_by_action),
                    "candidates_by_batch_type": dict(stats.candidates_by_batch_type),
                    "batch_count": stats.batch_count,
                    "dependency_count": stats.dependency_count,
                    "conflict_count": stats.conflict_count,
                    "average_priority": round(stats.average_priority, 3),
                    "average_risk": round(stats.average_risk, 3),
                    "policy_used": stats.policy_used,
                    "strategy_version": stats.strategy_version,
                }
            except Exception:
                pass
        if self.investigation_engine is not None:
            try:
                stats = self.investigation_engine.statistics()
                current = self.investigation_engine.query_engine.current_investigation()
                snap["autonomous_investigation"] = {
                    "total_investigations": stats.total_investigations,
                    "investigations_by_outcome": dict(stats.investigations_by_outcome),
                    "total_steps_executed": stats.total_steps_executed,
                    "total_assertions_evaluated": stats.total_assertions_evaluated,
                    "supported_assertion_count": stats.supported_assertion_count,
                    "contradicted_assertion_count": stats.contradicted_assertion_count,
                    "inconclusive_assertion_count": stats.inconclusive_assertion_count,
                    "total_recovery_attempts": stats.total_recovery_attempts,
                    "current_investigation_id": current.investigation_id if current is not None else None,
                    "current_state": current.state if current is not None else None,
                }
            except Exception:
                pass
        if self.understanding_engine is not None:
            try:
                stats = self.understanding_engine.statistics()
                latest = self.understanding_engine.query_engine.latest_assessment()
                snap["adaptive_understanding"] = {
                    "total_assessments": stats.total_assessments,
                    "total_reobservations": stats.total_reobservations,
                    "degraded_acceptances": stats.degraded_acceptances,
                    "assessments_by_state": dict(stats.assessments_by_state),
                    "assessments_by_decision": dict(stats.assessments_by_decision),
                    "average_readiness_score": round(stats.average_readiness_score, 3),
                    "average_observation_confidence": round(stats.average_observation_confidence, 3),
                    "contradiction_count": stats.contradiction_count,
                    "contradictions_by_type": dict(stats.contradictions_by_type),
                    "transition_count": stats.transition_count,
                    "unknown_state_count": stats.unknown_state_count,
                    "current_state": latest.state.primary_state if latest is not None else None,
                    "current_observation_confidence": (
                        round(latest.observation_confidence.value, 3) if latest is not None else None
                    ),
                }
            except Exception:
                pass
        return snap

    # ------------------------------------------------------------------
    # Entity Discovery — the Planner-facing query API. Thin, defensive
    # pass-throughs to the registry: every method degrades to an empty list
    # when no registry exists (e.g. entity discovery disabled or not yet run)
    # so callers never need their own None-guards.
    # ------------------------------------------------------------------

    def known_entities(self) -> list[Any]:
        """Confirmed entities (multi-source corroborated), plus confirmed-but-
        thin `incomplete` ones — everything the run positively knows exists."""
        return list(self.entity_registry.known_entities()) if self.entity_registry is not None else []

    def unknown_entities(self) -> list[Any]:
        """Candidate terms observed but not yet corroborated into entities."""
        return list(self.entity_registry.unknown_entities()) if self.entity_registry is not None else []

    def incomplete_entities(self) -> list[Any]:
        """Confirmed entities with no discovered operations or related pages."""
        return list(self.entity_registry.incomplete_entities()) if self.entity_registry is not None else []

    def entities_requiring_exploration(self) -> list[Any]:
        """Most-promising exploration targets first: incomplete entities,
        then uncorroborated candidates."""
        return (
            list(self.entity_registry.entities_requiring_exploration())
            if self.entity_registry is not None
            else []
        )

    # ------------------------------------------------------------------
    # Actor Discovery — the Planner-facing query API, mirroring the entity
    # one above. Every method degrades to [] with no actor_registry.
    # ------------------------------------------------------------------

    def known_actors(self) -> list[Any]:
        """Actually-authenticated sessions with some corroboration (confirmed
        + confirmed-but-thin `incomplete`)."""
        return list(self.actor_registry.known_actors()) if self.actor_registry is not None else []

    def unknown_actors(self) -> list[Any]:
        """Role terms observed (a dropdown option, a table cell, ...) but not
        yet corroborated."""
        return list(self.actor_registry.unknown_actors()) if self.actor_registry is not None else []

    def unverified_actors(self) -> list[Any]:
        """A role NAME evidenced well enough to corroborate, but never
        actually observed as an authenticated session under that identity."""
        return list(self.actor_registry.unverified_actors()) if self.actor_registry is not None else []

    def actors_requiring_exploration(self) -> list[Any]:
        """Most-promising exploration targets first: incomplete actors, then
        unverified ones, then uncorroborated candidates."""
        return list(self.actor_registry.actors_requiring_exploration()) if self.actor_registry is not None else []

    def actors_missing_permissions(self) -> list[Any]:
        """Known actors with no (or only low-confidence) permission evidence."""
        return list(self.actor_registry.actors_missing_permissions()) if self.actor_registry is not None else []

    def actors_missing_dashboard_understanding(self) -> list[Any]:
        """Known actors for whom no dashboard-classified page has been
        reached yet."""
        return (
            list(self.actor_registry.actors_missing_dashboard_understanding())
            if self.actor_registry is not None
            else []
        )

    # ------------------------------------------------------------------
    # Workflow Discovery — the Planner-facing query API, mirroring the
    # entity/actor ones above. Every method degrades to [] with no
    # workflow_registry.
    # ------------------------------------------------------------------

    def known_workflows(self) -> list[Any]:
        """Reconstructed workflows with real corroboration (confirmed +
        confirmed-but-thin `partial`)."""
        return list(self.workflow_registry.known_workflows()) if self.workflow_registry is not None else []

    def incomplete_workflows(self) -> list[Any]:
        return list(self.workflow_registry.incomplete_workflows()) if self.workflow_registry is not None else []

    def high_confidence_workflows(self) -> list[Any]:
        return list(self.workflow_registry.high_confidence_workflows()) if self.workflow_registry is not None else []

    def low_confidence_workflows(self) -> list[Any]:
        return list(self.workflow_registry.low_confidence_workflows()) if self.workflow_registry is not None else []

    def workflows_by_entity(self, entity_id: str) -> list[Any]:
        return list(self.workflow_registry.workflows_by_entity(entity_id)) if self.workflow_registry is not None else []

    def workflows_by_actor(self, actor_id: str) -> list[Any]:
        return list(self.workflow_registry.workflows_by_actor(actor_id)) if self.workflow_registry is not None else []

    def cross_role_workflows(self) -> list[Any]:
        """Workflows with 2+ distinct actor participants observed."""
        return list(self.workflow_registry.cross_role_workflows()) if self.workflow_registry is not None else []

    def workflows_with_unresolved_prerequisites(self) -> list[Any]:
        return (
            list(self.workflow_registry.workflows_with_unresolved_prerequisites())
            if self.workflow_registry is not None
            else []
        )

    def workflows_with_visible_outcomes_but_unknown_producers(self) -> list[Any]:
        return (
            list(self.workflow_registry.workflows_with_visible_outcomes_but_unknown_producers())
            if self.workflow_registry is not None
            else []
        )

    def workflows_requiring_actor_switching(self) -> list[Any]:
        """Workflows whose steps reference an actor that hasn't actually
        participated yet — an unresolved hand-off. Naming matches the task's
        Planner-integration wording; this milestone never performs the
        switch, only flags where one would be required."""
        return (
            list(self.workflow_registry.workflows_requiring_another_actor())
            if self.workflow_registry is not None
            else []
        )

    def workflow_gaps(self) -> list[Any]:
        return list(self.workflow_registry.workflow_gaps()) if self.workflow_registry is not None else []

    def workflow_graph_snapshot(self) -> Any:
        return self.workflow_registry.snapshot() if self.workflow_registry is not None else None

    # ------------------------------------------------------------------
    # Business Dependency Discovery — the Planner-facing query API,
    # mirroring the entity/actor/workflow ones above. Every method degrades
    # to [] with no dependency_registry.
    # ------------------------------------------------------------------

    def known_outputs(self) -> list[Any]:
        return list(self.dependency_registry.known_outputs()) if self.dependency_registry is not None else []

    def known_dependencies(self) -> list[Any]:
        return list(self.dependency_registry.known_dependencies()) if self.dependency_registry is not None else []

    def unresolved_dependencies(self) -> list[Any]:
        return list(self.dependency_registry.unresolved_dependencies()) if self.dependency_registry is not None else []

    def unresolved_kpi_dependencies(self) -> list[Any]:
        return list(self.dependency_registry.unresolved_kpi_dependencies()) if self.dependency_registry is not None else []

    def dependencies_by_entity(self, entity_id: str) -> list[Any]:
        return list(self.dependency_registry.dependencies_by_entity(entity_id)) if self.dependency_registry is not None else []

    def dependencies_by_workflow(self, workflow_id: str) -> list[Any]:
        return list(self.dependency_registry.dependencies_by_workflow(workflow_id)) if self.dependency_registry is not None else []

    def dependencies_by_actor(self, actor_id: str) -> list[Any]:
        return list(self.dependency_registry.dependencies_by_actor(actor_id)) if self.dependency_registry is not None else []

    def cross_role_dependencies(self) -> list[Any]:
        return list(self.dependency_registry.cross_role_dependencies()) if self.dependency_registry is not None else []

    def dependencies_requiring_actor_switch(self) -> list[Any]:
        return list(self.dependency_registry.dependencies_requiring_actor_switch()) if self.dependency_registry is not None else []

    def dependencies_with_unknown_producer(self) -> list[Any]:
        return list(self.dependency_registry.dependencies_with_unknown_producer()) if self.dependency_registry is not None else []

    def dependencies_with_unknown_consumer(self) -> list[Any]:
        return list(self.dependency_registry.dependencies_with_unknown_consumer()) if self.dependency_registry is not None else []

    def dependencies_with_contradictions(self) -> list[Any]:
        return list(self.dependency_registry.dependencies_with_contradictions()) if self.dependency_registry is not None else []

    def high_value_verification_requirements(self) -> list[Any]:
        return list(self.dependency_registry.high_value_verification_requirements()) if self.dependency_registry is not None else []

    def dependency_gaps(self) -> list[Any]:
        return list(self.dependency_registry.dependency_gaps()) if self.dependency_registry is not None else []

    def dependency_graph_snapshot(self) -> Any:
        return self.dependency_registry.snapshot() if self.dependency_registry is not None else None

    # ------------------------------------------------------------------
    # CRUD Discovery — the Planner/reporting-facing query API, mirroring the
    # entity/actor/workflow/dependency ones above. Every method degrades to
    # []/None with no crud_registry.
    # ------------------------------------------------------------------

    def crud_hypotheses(self) -> list[Any]:
        return list(self.crud_registry.all_hypotheses()) if self.crud_registry is not None else []

    def crud_hypotheses_by_operation(self, operation: str) -> list[Any]:
        return list(self.crud_registry.by_operation(operation)) if self.crud_registry is not None else []

    def crud_supported_hypotheses(self) -> list[Any]:
        return list(self.crud_registry.supported_or_better()) if self.crud_registry is not None else []

    def crud_destructive_hypotheses(self) -> list[Any]:
        return list(self.crud_registry.destructive_hypotheses()) if self.crud_registry is not None else []

    def crud_graph_snapshot(self) -> Any:
        return self.crud_registry.snapshot() if self.crud_registry is not None else None

    def temporary_record_snapshot(self) -> Any:
        return self.temporary_record_registry.snapshot() if self.temporary_record_registry is not None else None

    # ------------------------------------------------------------------
    # Adaptive Application Understanding — read-only query passthroughs.
    # Every method degrades to None/[] when no engine is attached, matching
    # every other engine's Planner-facing API in this file.
    # ------------------------------------------------------------------

    def latest_understanding(self) -> Any:
        return self.understanding_engine.query_engine.latest_assessment() if self.understanding_engine is not None else None

    def understanding_history(self) -> list[Any]:
        return list(self.understanding_engine.query_engine.assessment_history()) if self.understanding_engine is not None else []

    def understanding_statistics(self) -> Any:
        return self.understanding_engine.statistics() if self.understanding_engine is not None else None

    def understanding_contradictions(self) -> list[Any]:
        return list(self.understanding_engine.query_engine.contradictions()) if self.understanding_engine is not None else []

    def observed_state_transitions(self) -> list[Any]:
        return list(self.understanding_engine.query_engine.transitions()) if self.understanding_engine is not None else []

    def known_application_states(self) -> list[str]:
        return list(self.understanding_engine.query_engine.known_states()) if self.understanding_engine is not None else []

    def low_confidence_observations(self, threshold: float = 0.6) -> list[Any]:
        if self.understanding_engine is None:
            return []
        return list(self.understanding_engine.query_engine.low_confidence_assessments(threshold))

    def unknown_state_observations(self) -> list[Any]:
        return list(self.understanding_engine.query_engine.unknown_state_assessments()) if self.understanding_engine is not None else []

    def manual_cleanup_instructions(self) -> list[Any]:
        return (
            self.temporary_record_registry.manual_cleanup_report()
            if self.temporary_record_registry is not None
            else []
        )

    def remember_collections(self, model: Any) -> None:
        """Accumulates distinct `RecordCollection` element_ids from the
        Canonical Page Model across the whole run, the same pattern as
        `known_form_ids`/`known_table_ids` above. Best-effort and non-fatal —
        called from `AgentController._run_perception_engine`, which already
        treats perception as additive and never lets a failure here reach
        the exploration loop."""
        if model is None:
            return
        for c in getattr(model, "collections", None) or []:
            cid = getattr(c, "element_id", None) or getattr(c, "stable_id", None)
            if cid:
                self.known_collection_ids.add(cid)

    def investigation_stop_report(self) -> Any:
        """`app.intelligence.autonomous_investigation.schemas.StopReport` —
        the rich "why did autonomous investigation stop, and what should
        happen next" answer. None with no investigation_engine (standard
        mode, or autonomous investigation disabled)."""
        if self.investigation_engine is None:
            return None
        return self.investigation_engine.stop_report(self)

    # ------------------------------------------------------------------
    # Application Knowledge Graph — the Planner-facing query API. A
    # semantic PROJECTION of the five registries above; every method
    # degrades to []/None with no knowledge_graph attached. Synchronisation
    # itself is triggered from the controller's post-action hook, not here.
    # ------------------------------------------------------------------

    def synchronise_knowledge_graph(self) -> Any:
        if self.knowledge_graph is None:
            return None
        return self.knowledge_graph.synchronize(
            entity_registry=self.entity_registry, actor_registry=self.actor_registry,
            workflow_registry=self.workflow_registry, dependency_registry=self.dependency_registry,
            crud_registry=self.crud_registry, iteration=len(self.actions),
        )

    def knowledge_graph_snapshot(self) -> Any:
        return self.knowledge_graph.snapshot() if self.knowledge_graph is not None else None

    def knowledge_graph_statistics(self) -> Any:
        return self.knowledge_graph.statistics() if self.knowledge_graph is not None else None

    def knowledge_graph_gaps(self) -> list[Any]:
        return list(self.knowledge_graph.gaps()) if self.knowledge_graph is not None else []

    def knowledge_graph_consistency_issues(self) -> list[Any]:
        return list(self.knowledge_graph.consistency_issues()) if self.knowledge_graph is not None else []

    def query_knowledge_graph(self, query: Any) -> Any:
        return self.knowledge_graph.query(query) if self.knowledge_graph is not None else None

    def context_for_node(self, node_id: str, **kwargs) -> Any:
        return self.knowledge_graph.context_for_node(node_id, **kwargs) if self.knowledge_graph is not None else None

    def context_for_entity(self, entity_id: str, **kwargs) -> Any:
        return self.knowledge_graph.context_for_entity(entity_id, **kwargs) if self.knowledge_graph is not None else None

    def context_for_actor(self, actor_id: str, **kwargs) -> Any:
        return self.knowledge_graph.context_for_actor(actor_id, **kwargs) if self.knowledge_graph is not None else None

    def context_for_workflow(self, workflow_id: str, **kwargs) -> Any:
        return self.knowledge_graph.context_for_workflow(workflow_id, **kwargs) if self.knowledge_graph is not None else None

    def context_for_output(self, output_id: str, **kwargs) -> Any:
        return self.knowledge_graph.context_for_output(output_id, **kwargs) if self.knowledge_graph is not None else None

    # ------------------------------------------------------------------
    # Goal Generation Engine -- the Planner-facing query API for the FIRST
    # reasoning engine that consumes the Knowledge Graph. Every method
    # degrades to None/[] with no `goal_engine` attached. Generation is
    # triggered explicitly via `generate_goals()` (called by the controller
    # right after `synchronise_knowledge_graph()`), never implicitly.
    # ------------------------------------------------------------------

    def generate_goals(self) -> Any:
        if self.goal_engine is None or self.knowledge_graph is None:
            return None
        return self.goal_engine.generate(self.knowledge_graph, iteration=len(self.actions))

    def goal_generation_result(self) -> Any:
        return self.goal_engine.query_engine.all_goals() if self.goal_engine is not None else []

    def goal_statistics(self) -> Any:
        return self.goal_engine.statistics() if self.goal_engine is not None else None

    def goal_snapshot(self) -> Any:
        if self.goal_engine is None:
            return None
        return {
            "goals": [g.model_dump(mode="json") for g in self.goal_engine.query_engine.all_goals()],
            "groups": [g.model_dump(mode="json") for g in self.goal_engine.query_engine.all_groups()],
        }

    def goal_summary(self) -> Any:
        if self.goal_engine is None:
            return None
        stats = self.goal_engine.statistics()
        top = self.goal_engine.highest_priority_goals(limit=10)
        return {"statistics": stats.model_dump(mode="json"), "top_goals": [g.model_dump(mode="json") for g in top]}

    def highest_priority_goals(self, limit: int = 20) -> list[Any]:
        return list(self.goal_engine.highest_priority_goals(limit)) if self.goal_engine is not None else []

    def goals_for_actor(self, actor_id: str) -> list[Any]:
        return list(self.goal_engine.query_engine.goals_for_actor(actor_id)) if self.goal_engine is not None else []

    def goals_for_entity(self, entity_id: str) -> list[Any]:
        return list(self.goal_engine.query_engine.goals_for_entity(entity_id)) if self.goal_engine is not None else []

    def goals_for_workflow(self, workflow_id: str) -> list[Any]:
        return list(self.goal_engine.query_engine.goals_for_workflow(workflow_id)) if self.goal_engine is not None else []

    def goals_for_output(self, output_id: str) -> list[Any]:
        return list(self.goal_engine.query_engine.goals_for_output(output_id)) if self.goal_engine is not None else []

    def goals_for_gap(self, gap_id: str) -> list[Any]:
        return list(self.goal_engine.query_engine.goals_for_gap(gap_id)) if self.goal_engine is not None else []

    def goals_for_contradiction(self, contradiction_id: str) -> list[Any]:
        return list(self.goal_engine.query_engine.goals_for_contradiction(contradiction_id)) if self.goal_engine is not None else []

    def blocked_goals(self) -> list[Any]:
        return list(self.goal_engine.query_engine.blocked_goals()) if self.goal_engine is not None else []

    def completed_goals(self) -> list[Any]:
        return list(self.goal_engine.query_engine.completed_goals()) if self.goal_engine is not None else []

    def pending_goals(self) -> list[Any]:
        return list(self.goal_engine.query_engine.pending_goals()) if self.goal_engine is not None else []

    # ------------------------------------------------------------------
    # Scenario Planning Engine -- the Planner-facing query API for the
    # engine that converts goals into declarative investigation scenarios.
    # Every method degrades to None/[] with no `scenario_engine` attached.
    # Generation is triggered explicitly via `generate_scenarios()` (called
    # by the controller right after `generate_goals()`), never implicitly.
    # ------------------------------------------------------------------

    def generate_scenarios(self) -> Any:
        if self.scenario_engine is None or self.goal_engine is None:
            return None
        return self.scenario_engine.generate(self.goal_engine, self.knowledge_graph, iteration=len(self.actions))

    def scenario_planning_result(self) -> Any:
        return self.scenario_engine.query_engine.all_scenarios() if self.scenario_engine is not None else []

    def scenario_statistics(self) -> Any:
        return self.scenario_engine.statistics() if self.scenario_engine is not None else None

    def scenario_snapshot(self) -> Any:
        if self.scenario_engine is None:
            return None
        return self.scenario_engine.serializer.compact_snapshot()

    def scenario_summary(self) -> Any:
        if self.scenario_engine is None:
            return None
        stats = self.scenario_engine.statistics()
        return {"statistics": stats.model_dump(mode="json"), "summary": self.scenario_engine.serializer.human_readable_summary(stats.model_dump(mode="json"))}

    def scenario_by_id(self, scenario_id: str) -> Any:
        return self.scenario_engine.query_engine.scenario_by_id(scenario_id) if self.scenario_engine is not None else None

    def scenarios_for_goal(self, goal_id: str) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenarios_for_goal(goal_id)) if self.scenario_engine is not None else []

    def feasible_scenarios(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.feasible_scenarios()) if self.scenario_engine is not None else []

    def blocked_scenarios(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.blocked_scenarios()) if self.scenario_engine is not None else []

    def incomplete_scenarios(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.incomplete_scenarios()) if self.scenario_engine is not None else []

    def scenarios_for_actor(self, actor_id: str) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenarios_for_actor(actor_id)) if self.scenario_engine is not None else []

    def scenarios_for_entity(self, entity_id: str) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenarios_for_entity(entity_id)) if self.scenario_engine is not None else []

    def scenarios_for_workflow(self, workflow_id: str) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenarios_for_workflow(workflow_id)) if self.scenario_engine is not None else []

    def scenarios_for_output(self, output_id: str) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenarios_for_output(output_id)) if self.scenario_engine is not None else []

    def scenarios_requiring_actor_switch(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenarios_requiring_actor_switch()) if self.scenario_engine is not None else []

    def scenarios_requiring_mutation(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenarios_requiring_mutation()) if self.scenario_engine is not None else []

    def read_only_scenarios(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.read_only_scenarios()) if self.scenario_engine is not None else []

    def high_risk_scenarios(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.high_risk_scenarios()) if self.scenario_engine is not None else []

    def scenario_gaps(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenario_gaps()) if self.scenario_engine is not None else []

    def scenario_conflicts(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenario_conflicts()) if self.scenario_engine is not None else []

    def scenario_dependencies(self) -> list[Any]:
        return list(self.scenario_engine.query_engine.scenario_dependencies()) if self.scenario_engine is not None else []

    # ------------------------------------------------------------------
    # QA Strategy Engine -- the Planner-facing query API for the engine
    # that decides WHICH scenarios should execute, in what order, and why.
    # Every method degrades to None/[] with no `strategy_engine` attached.
    # Generation is triggered explicitly via `generate_strategy()` (called
    # by the controller right after `generate_scenarios()`), never implicitly.
    # ------------------------------------------------------------------

    def generate_strategy(self, *, policy_id: str = "balanced", weight_overrides: dict[str, float] | None = None) -> Any:
        if self.strategy_engine is None or self.scenario_engine is None:
            return None
        return self.strategy_engine.generate(
            self.scenario_engine, self.goal_engine, self.knowledge_graph, policy_id=policy_id, weight_overrides=weight_overrides,
        )

    def strategy_result(self) -> Any:
        return self.strategy_engine.query_engine.all_candidates() if self.strategy_engine is not None else []

    def execution_queue(self, queue_type: str) -> list[Any]:
        return list(self.strategy_engine.query_engine.candidates_by_queue(queue_type)) if self.strategy_engine is not None else []

    def execution_batches(self) -> list[Any]:
        return list(self.strategy_engine.query_engine.all_batches()) if self.strategy_engine is not None else []

    def execution_statistics(self) -> Any:
        return self.strategy_engine.statistics() if self.strategy_engine is not None else None

    def strategy_summary(self) -> Any:
        if self.strategy_engine is None:
            return None
        stats = self.strategy_engine.statistics()
        top = self.strategy_engine.query_engine.ready()[:10]
        return {
            "statistics": stats.model_dump(mode="json"),
            "top_candidate_ids": [c.candidate_id for c in top],
        }

    def next_execution(self) -> Any:
        return self.strategy_engine.query_engine.next_execution() if self.strategy_engine is not None else None

    def highest_value_candidates(self, limit: int = 20) -> list[Any]:
        return list(self.strategy_engine.query_engine.highest_value(limit)) if self.strategy_engine is not None else []

    def lowest_risk_candidates(self, limit: int = 20) -> list[Any]:
        return list(self.strategy_engine.query_engine.lowest_risk(limit)) if self.strategy_engine is not None else []

    def blocked_candidates(self) -> list[Any]:
        return list(self.strategy_engine.query_engine.blocked()) if self.strategy_engine is not None else []

    def deferred_candidates(self) -> list[Any]:
        return list(self.strategy_engine.query_engine.deferred()) if self.strategy_engine is not None else []

    def ready_candidates(self) -> list[Any]:
        return list(self.strategy_engine.query_engine.ready()) if self.strategy_engine is not None else []

    def batch_for_actor(self, actor_term: str) -> list[Any]:
        return list(self.strategy_engine.query_engine.batch_for_actor(actor_term)) if self.strategy_engine is not None else []

    def batch_for_workflow(self, workflow_term: str) -> list[Any]:
        return list(self.strategy_engine.query_engine.batch_for_workflow(workflow_term)) if self.strategy_engine is not None else []

    def recommended_sequence(self) -> list[str]:
        return list(self.strategy_engine.query_engine.recommended_sequence()) if self.strategy_engine is not None else []

    def coverage_forecast(self) -> Any:
        return self.strategy_engine.query_engine.coverage_forecast() if self.strategy_engine is not None else None

    def confidence_forecast(self) -> Any:
        return self.strategy_engine.query_engine.confidence_forecast() if self.strategy_engine is not None else None

    def risk_forecast(self) -> Any:
        return self.strategy_engine.query_engine.risk_forecast() if self.strategy_engine is not None else None

    # ------------------------------------------------------------------
    # Autonomous Investigation Engine -- the Planner-facing query API for
    # the execution brain. Every method degrades to None/[] with no
    # `investigation_engine` attached (i.e. autonomous investigation
    # disabled for this run). Execution itself is driven by the controller
    # calling `investigation_engine.next_action()`/`observe_step_result()`
    # directly each iteration, never through a method here -- these are
    # read-only query passthroughs only.
    # ------------------------------------------------------------------

    def current_investigation(self) -> Any:
        return self.investigation_engine.query_engine.current_investigation() if self.investigation_engine is not None else None

    def completed_investigations(self) -> list[Any]:
        return list(self.investigation_engine.query_engine.completed()) if self.investigation_engine is not None else []

    def failed_investigations(self) -> list[Any]:
        return list(self.investigation_engine.query_engine.failed()) if self.investigation_engine is not None else []

    def blocked_investigations(self) -> list[Any]:
        return list(self.investigation_engine.query_engine.blocked()) if self.investigation_engine is not None else []

    def paused_investigations(self) -> list[Any]:
        return list(self.investigation_engine.query_engine.paused()) if self.investigation_engine is not None else []

    def investigation_history(self) -> list[Any]:
        return list(self.investigation_engine.query_engine.history()) if self.investigation_engine is not None else []

    def latest_investigation_evidence(self) -> Any:
        return self.investigation_engine.query_engine.latest_evidence() if self.investigation_engine is not None else None

    def investigation_coverage(self) -> Any:
        return self.investigation_engine.query_engine.coverage() if self.investigation_engine is not None else None

    def investigation_confidence(self) -> Any:
        return self.investigation_engine.query_engine.confidence() if self.investigation_engine is not None else None

    def investigation_statistics(self) -> Any:
        return self.investigation_engine.statistics() if self.investigation_engine is not None else None

    def note_navigation_taken(self, action: Any) -> None:
        """Remember that a navigation item was followed, by its LABEL.

        Action signatures are keyed on the page fingerprint, which is right for a
        button but wrong for persistent chrome: a sidebar item appears on every
        page, so its count reset each time and the run returned to it between
        almost every module — 11 clicks on one link in 52 actions.

        Recorded HERE, where every action is already recorded, so the frontier's
        check and this record are always about the same event. Computing an
        identity in one place and counting it in another is what made the first
        attempt at this fix a no-op.
        """
        meta = getattr(action, "metadata", None) or {}
        candidate_id = str(meta.get("candidate_id") or "")
        if not candidate_id.startswith("nav_"):
            return
        label = str(meta.get("action_label") or "").strip().lower()
        if label:
            self.navigation_labels_taken.add(label)

    def has_taken_navigation(self, label: str | None) -> bool:
        text = (label or "").strip().lower()
        return bool(text) and text in self.navigation_labels_taken

    def has_visited(self, url: str | None) -> bool:
        """Has this destination already been seen this run?

        Canonicalised the same way `remember_page` canonicalises what it records,
        so the two agree — asking with a raw href while `visited_urls` holds
        normalized ones would always answer "no".
        """
        if not url:
            return False
        canonical = normalize_url(url, base_url=self.start_url) or url
        return canonical in self.visited_urls

    def same_domain(self, url: str, authorized_domain: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
            return host == authorized_domain.lower() or host.endswith(f".{authorized_domain.lower()}")
        except Exception:
            return False

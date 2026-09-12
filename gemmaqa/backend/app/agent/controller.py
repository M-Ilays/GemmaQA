"""Main agent controller — autonomous exploratory QA orchestration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.bug_analyzer import BugAnalyzer
from app.agent.auth_strategy import AuthenticationStrategy
from app.agent.creation_verifier import verify_creation
from app.agent.credentials import CredentialProfile, CredentialVault
from app.agent.cleanup_planner import plan_cleanup_action, plan_cleanup_navigation
from app.agent.temporary_record_registry import (
    CleanupPlan,
    TemporaryRecordRegistry,
    identity_matches_cells,
)
from app.agent.documenter import Documenter
from app.agent.explorer import Explorer
from app.agent.memory import RunMemory, action_signature
from app.agent.submission_outcome import classify_submission
from app.agent.planner import Planner, screenshot_budget_exhausted
from app.reporting.activity_log import ActivityLog
from app.reporting.live_action import live_action_fields
from app.agent.state_machine import AgentPhase, StateMachine
from app.agent.tester import Tester
from app.browser.adapters import create_browser_adapter
from app.browser.console_monitor import ConsoleMonitor
from app.browser.custom_controls import UNSATISFIED_REFERENCE_MARKER
from app.browser.evidence import EvidenceCollector
from app.browser.executor import ActionExecutor
from app.browser.pacing import ExecutionPacing, pacing_activity_line
from app.browser.locators import LocatorRegistry
from app.browser.manager import BrowserManager
from app.browser.network_monitor import NetworkMonitor
from app.browser.observer import PageObserver
from app.config import get_settings
from app.intelligence.actor_discovery import ActorDiscoveryEngine
from app.intelligence.entity_discovery import EntityDiscoveryEngine
from app.intelligence.dependency_discovery import DependencyDiscoveryEngine
from app.intelligence.knowledge_graph import ApplicationKnowledgeGraph
from app.intelligence.goal_generation import GoalGenerationEngine
from app.intelligence.scenario_planning import ScenarioPlanningEngine
from app.intelligence.qa_strategy import QAStrategyEngine
from app.intelligence.autonomous_investigation import AutonomousInvestigationEngine
from app.intelligence.crud_discovery import CRUDDiscoveryEngine
from app.intelligence.adaptive_understanding import AdaptiveApplicationUnderstandingEngine
from app.intelligence.adaptive_understanding.readiness_estimator import reobservation_interval_ms
from app.intelligence.adaptive_understanding.schemas import UNTRUSTWORTHY_ABSENCE_STATES
from app.intelligence.workflow_discovery import WorkflowDiscoveryEngine
from app.perception.engine import PerceptionEngine
from app.gemma import get_gemma_provider
from app.gemma.base import GemmaProvider
from app.runtime_info import browser_adapter_display, provider_capability_mode, provider_display_name
from app.models import ActionRecord, BugRecord, PageRecord, QARun
from app.reporting.exporters import ReportExporter
from app.reporting.report_builder import ReportBuilder
from app.schemas import (
    ActionCategory,
    ActionResult,
    ActionType,
    BrowserAction,
    CreateRunRequest,
    PageState,
    RiskLevel,
    RunConfiguration,
    RunStatusEnum,
)
from app.safety.audit import safety_audit
from app.safety.policies import BLOCKED_TEXT_PATTERNS, SafetyPolicy
from app.safety.sensitive import find_element
from app.safety.validator import ActionValidator
from app.utils.auth_trace import trace as auth_trace
from app.utils.exploration_trace import record as exploration_trace
from app.utils.ids import new_id
from app.utils.logging import get_logger
from app.utils.sanitization import mask_secret, sanitize_text

logger = get_logger("agent.controller")

# Cleanup bounds, per record. A full cleanup is at most: return to the list,
# open the record, click delete, confirm — so six steps leaves slack for a
# re-observation that comes back slightly different, while still guaranteeing
# the pass terminates. Navigations are capped separately so a page that keeps
# failing to show the record cannot become a navigation ping-pong.
CLEANUP_MAX_STEPS_PER_RECORD = 6
CLEANUP_MAX_NAVIGATIONS_PER_RECORD = 2
# Re-observations allowed while the application is still presenting itself.
CLEANUP_MAX_REOBSERVATIONS_PER_RECORD = 3

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentController:
    """
    Core loop:
      Observe → Classify → Plan → Validate → before evidence → Execute →
      after evidence → Compare → Detect issues → Update memory → Update docs
    """

    def __init__(
        self,
        run_id: str,
        request: CreateRunRequest,
        db_factory: Callable[[], AsyncSession],
        on_event: EventCallback | None = None,
        gemma: GemmaProvider | None = None,
    ) -> None:
        self.run_id = run_id
        self.request = request
        self.config: RunConfiguration = request.configuration
        self.db_factory = db_factory
        self.on_event = on_event
        self.settings = get_settings()
        self.sm = StateMachine()
        self.memory = RunMemory(run_id=run_id, start_url=request.url)
        self.memory.bootstrap_budgets(self.config)
        self._cancel = asyncio.Event()
        # Set = running. Cleared on operator Pause; set again on Continue or End.
        # The loop waits on this at the start of each iteration so the current
        # step can finish. Elapsed-time accounting pauses until resume; there is
        # no maximum runtime that stops the run.
        self._run_gate = asyncio.Event()
        self._run_gate.set()
        self._paused_total_s = 0.0
        self._pause_started_at: datetime | None = None

        self.gemma = gemma or get_gemma_provider()

        self.planner = Planner(self.gemma)
        self.explorer = Explorer(self.gemma)
        self.tester = Tester(self.gemma)
        self.bug_analyzer = BugAnalyzer(self.gemma)
        self.documenter = Documenter(self.gemma)
        self.report_builder = ReportBuilder()
        # Durable second-by-second record of the run, written from `emit` and
        # readable while the run is still going. See app.reporting.activity_log.
        self.activity_log = ActivityLog(run_id)
        self.pacing = ExecutionPacing()
        self.pacing.bind(
            is_cancelled=self._cancel.is_set,
            wait_if_paused=self._wait_if_paused,
        )

        self.authorized_domain = urlparse(request.url).hostname or ""
        patterns = tuple(
            dict.fromkeys([*BLOCKED_TEXT_PATTERNS, *self.settings.extra_blocked_patterns])
        )
        self.policy = SafetyPolicy(
            authorized_url=request.url,
            authorized_domain=self.authorized_domain,
            safe_mode=self.config.safe_mode,
            allow_controlled_writes=self.config.allow_controlled_writes,
            allow_login=getattr(self.config, "allow_login", self.settings.allow_login),
            allow_test_account_creation=getattr(
                self.config,
                "allow_test_account_creation",
                self.settings.allow_test_account_creation,
            ),
            allow_safe_test_data_creation=getattr(
                self.config,
                "allow_safe_test_data_creation",
                self.settings.allow_safe_test_data_creation,
            ),
            allow_destructive_actions=getattr(
                self.config,
                "allow_destructive_actions",
                self.settings.allow_destructive_actions,
            ),
            allow_financial_actions=getattr(
                self.config,
                "allow_financial_actions",
                self.settings.allow_financial_actions,
            ),
            allow_cross_domain=self.config.allow_cross_domain,
            allow_subdomains=self.config.allow_subdomains,
            allow_local_targets=self.settings.allow_local_targets,
            max_actions=self.config.max_actions or 10**9,
            max_pages=self.config.max_pages or 10**9,
            max_runtime_seconds=self.config.max_runtime_seconds or 10**9,
            max_retries=getattr(self.config, "max_retries", self.settings.max_retries),
            max_screenshots=getattr(
                self.config, "max_screenshots", self.settings.max_screenshots
            ),
            max_prompt_chars=self.settings.max_prompt_chars,
            max_evidence_bytes=self.settings.max_evidence_bytes,
            no_progress_limit=self.settings.no_progress_limit,
            blocked_text_patterns=patterns,
            store_full_html=self.settings.store_full_html,
        )
        self.validator = ActionValidator(self.policy)
        self._started_at: datetime | None = None
        self._credentials_cleared = False
        self._credential_vault = CredentialVault()
        if request.username and request.password:
            self._credential_vault.store(
                CredentialProfile(
                    profile_id="primary",
                    username=request.username,
                    password=request.password,
                    source="supplied",
                    email=request.username if "@" in request.username else None,
                ),
                make_active=True,
            )
        self.memory.auth_strategy = AuthenticationStrategy(self._credential_vault)

    def request_cancel(self) -> None:
        self._cancel.set()
        self._run_gate.set()

    def request_pause(self) -> None:
        if self._cancel.is_set() or self.sm.is_terminal:
            return
        self._run_gate.clear()

    def request_resume(self) -> None:
        if self._cancel.is_set() or self.sm.is_terminal:
            return
        self._run_gate.set()

    def is_pause_requested(self) -> bool:
        return not self._run_gate.is_set()

    def set_execution_pacing(
        self,
        *,
        execution_speed: float | None = None,
        action_pause: float | None = None,
    ) -> dict[str, float]:
        """Operator speed/pause. Affects the next action; does not restart the run."""
        return self.pacing.update(
            execution_speed=execution_speed,
            action_pause=action_pause,
        )

    def _clear_credentials(self) -> None:
        """Drop plaintext credentials from the request object (vault retains run secrets)."""
        if self._credentials_cleared:
            return
        try:
            self.request.password = None
            # Keep username masked presence only
            if self.request.username:
                self.request.username = mask_secret(self.request.username) or "[redacted]"
        except Exception:
            pass
        self._credentials_cleared = True

    def _purge_vault(self) -> None:
        self._credential_vault.clear()
        if self.memory.auth_strategy:
            self.memory.auth_strategy.vault = self._credential_vault

    def _runtime_seconds(self) -> float:
        if not self._started_at:
            return 0.0
        elapsed = (_utc_now() - self._started_at).total_seconds()
        paused = self._paused_total_s
        if self._pause_started_at is not None:
            paused += (_utc_now() - self._pause_started_at).total_seconds()
        return max(0.0, elapsed - paused)

    async def _wait_if_paused(self) -> None:
        """Block the loop at a safe checkpoint. Time spent here is not billed."""
        if self._run_gate.is_set() or self._cancel.is_set():
            return
        self._pause_started_at = _utc_now()
        await self._update_run(
            status=RunStatusEnum.PAUSED,
            message="Paused — click Continue to resume",
        )
        await self.emit("run_paused", {"reason": "operator_pause"})
        try:
            await self._run_gate.wait()
        finally:
            if self._pause_started_at is not None:
                self._paused_total_s += (_utc_now() - self._pause_started_at).total_seconds()
                self._pause_started_at = None
        if self._cancel.is_set():
            return
        await self._update_run(status=self.sm.status, message="Resumed")
        await self.emit("run_resumed", {"reason": "operator_resume"})

    async def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        # Every event in the run passes through here, which is why the durable
        # activity log is teed off this one place rather than added at each call
        # site: nothing can be emitted and go unrecorded, and no future event
        # type has to remember to opt in. See app.reporting.activity_log.
        activity = self.activity_log.record(event_type, payload or {})
        event = {
            "type": event_type,
            "run_id": self.run_id,
            "timestamp": _utc_now().isoformat() + "Z",
            "payload": payload or {},
            # Carried on the wire so a live consumer shows the same readable line
            # and the same sequence number the stored log has — the UI must not
            # have to re-derive either, or the two would drift.
            "seq": activity["seq"],
            "elapsed_ms": activity["elapsed_ms"],
            "phase": activity["phase"],
            "summary": activity["summary"],
        }
        logger.info("event=%s run=%s %s", event_type, self.run_id, activity["summary"])
        if self.on_event:
            # Support both legacy (event, data) and structured consumers
            await self.on_event(event_type, event)

    def _record_span(self, phase: str, started: float, **detail: Any) -> None:
        """Record how long a stretch of the loop took.

        A complete list of events still cannot answer "why did that take nine
        seconds?" — only a duration can, and durations are what turn the activity
        log from a transcript into a diagnosis. Never raises: timing that can
        break a run is worse than no timing.
        """
        try:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            self.activity_log.record(
                "phase_timing",
                {**detail, "phase": phase, "duration_ms": int(elapsed_ms)},
                phase=phase,
                duration_ms=elapsed_ms,
            )
        except Exception:  # pragma: no cover - defensive
            pass

    async def run(self) -> None:
        browser = BrowserManager(run_id=self.run_id, headless=self.config.headless)
        console = ConsoleMonitor()
        network = NetworkMonitor()
        adapter = create_browser_adapter(
            self.run_id,
            headless=self.config.headless,
            manager=browser,
            console=console,
            network=network,
        )
        self.memory.browser_adapter_id = self.settings.normalized_browser_adapter
        self.memory.provider_type = self.settings.normalized_gemma_provider
        self.memory.adapter_connection_status = "starting"
        evidence = EvidenceCollector(
            self.run_id, max_screenshots=self.policy.max_screenshots
        )
        registry = LocatorRegistry()
        observer = PageObserver(console, network, registry=registry)
        # Visual analysis is only ever wired in for a provider/model already
        # configured to accept images — otherwise the engine stays fully
        # deterministic (no visual-model call is ever attempted), exactly as
        # before this feature existed. Even when wired in, VisualObservationPolicy
        # still gates every individual call — this does not make screenshot
        # analysis mandatory after every action.
        perception_engine = PerceptionEngine(
            visual_provider=self.gemma if self.settings.effective_gemma_supports_images else None,
        )
        # Entity discovery accumulates across the whole run — one engine
        # instance whose registry lives on RunMemory for Planner queries.
        entity_engine = EntityDiscoveryEngine()
        self.memory.entity_registry = entity_engine.registry
        self._entity_engine = entity_engine
        # Actor discovery accumulates across the whole run too — same
        # single-instance-on-RunMemory pattern as entity discovery.
        actor_engine = ActorDiscoveryEngine()
        self.memory.actor_registry = actor_engine.registry
        self._actor_engine = actor_engine
        # Workflow discovery needs a before/after CanonicalPageModel pair
        # plus the executed action — unlike entity/actor discovery, it is
        # NOT driven from _run_perception_engine (which only ever sees one
        # observation at a time). See _run_workflow_engine, called from the
        # main loop once both snapshots and the action are available.
        workflow_engine = WorkflowDiscoveryEngine()
        self.memory.workflow_registry = workflow_engine.registry
        self._workflow_engine = workflow_engine
        # CRUD Discovery needs the SAME before/after pair workflow discovery
        # needs — called from the same post-action hook. Strictly a
        # discovery/hypothesis engine: it produces CRUDWorkflowHypothesis
        # records (application-neutral, evidence-gated — see
        # app.intelligence.crud_discovery) and never executes a browser
        # action itself, exactly like workflow/dependency discovery above.
        # Always instantiated (never gated behind enable_autonomous_
        # investigation): discovery/reporting must work whether or not
        # execution is opted into.
        crud_engine = CRUDDiscoveryEngine()
        self.memory.crud_registry = crud_engine.registry
        self._crud_engine = crud_engine
        # Adaptive Application Understanding
        # (app.intelligence.adaptive_understanding) — judges, from observable
        # evidence alone, whether an observation is trustworthy enough to
        # reason from, and what kind of screen it shows. Always on: it
        # replaces an assumption (domcontentloaded means ready) that every
        # run previously made silently, so there is nothing to opt into.
        # Never navigates, clicks, waits, or calls a model.
        self.memory.understanding_engine = AdaptiveApplicationUnderstandingEngine()
        self._last_understanding: Any | None = None
        # Tracks which prepared model request has already been recorded, so a
        # deterministically-planned iteration is not credited with the previous
        # iteration's model call. See _record_model_context.
        self._last_generation_sequence: int = 0
        self._understanding_before_action: Any | None = None
        # Temporary Record Registry (app.agent.temporary_record_registry) —
        # the run-scoped store of every record GenericFormWorkflow actually
        # creates and verifies (see the post-submit hook below). Always
        # instantiated: cleanup planning must be available whenever safe
        # test-data creation itself is available, not gated behind a
        # separate opt-in.
        self.memory.temporary_record_registry = TemporaryRecordRegistry(run_id=self.run_id)
        # Dependency discovery needs the SAME before/after pair workflow
        # discovery needs (before/after correlation is meaningless from a
        # single observation) -- called from the same post-action hook,
        # right after workflow discovery, reusing its output.
        dependency_engine = DependencyDiscoveryEngine()
        self.memory.dependency_registry = dependency_engine.registry
        self._dependency_engine = dependency_engine
        # The Application Knowledge Graph is a semantic PROJECTION of the
        # four registries above -- synchronised after dependency discovery,
        # from the same post-action hook, never an independent rediscovery.
        self.memory.knowledge_graph = ApplicationKnowledgeGraph()
        # Goal Generation is the first reasoning engine that CONSUMES the
        # Knowledge Graph (rather than projecting into it) -- synchronised
        # right after it, same post-action hook. It only decides WHAT to
        # investigate next; it never executes a browser action.
        self.memory.goal_engine = GoalGenerationEngine()
        # Scenario Planning converts each current goal into declarative,
        # browser-independent investigation scenarios -- synchronised right
        # after Goal Generation, same post-action hook. It decides HOW a
        # goal COULD be investigated; it never executes a browser action,
        # never switches actors, and never replaces the runtime Planner
        # below (which continues to select immediate browser actions).
        self.memory.scenario_engine = ScenarioPlanningEngine()
        # QA Strategy decides WHICH of the current scenarios should execute,
        # in what order, and why -- synchronised right after Scenario
        # Planning, same post-action hook. It never executes a browser
        # action, never switches actors, and never replaces the runtime
        # Planner below; it only ranks and groups what Scenario Planning
        # already produced.
        self.memory.strategy_engine = QAStrategyEngine()
        # Autonomous Investigation is the execution brain -- it selects the
        # next executable scenario from QA Strategy's queues and drives it
        # through the SAME runtime Planner/SafetyValidator/ActionExecutor/
        # BrowserAdapter pipeline every other action already uses. Strictly
        # opt-in (default False): default runtime behaviour for every
        # existing exploration run is unchanged unless a caller explicitly
        # requests it.
        self.enable_autonomous_investigation = bool(getattr(self.config, "enable_autonomous_investigation", False))
        self.memory.investigation_engine = (
            AutonomousInvestigationEngine() if self.enable_autonomous_investigation else None
        )
        # Option A (see app.gemma.mock_provider.MockGemmaProvider docstring /
        # app.runtime_info.provider_capability_mode): Mock is EXPLORATION-
        # ONLY and never claims scenario-execution capability. Surfaced here
        # (once, at startup) rather than only in the final report, so a
        # caller watching live events sees the limitation immediately, not
        # after the run already spent its whole action budget on scenarios
        # Mock cannot actually drive to completion.
        self._capability_mode = provider_capability_mode(
            getattr(self.gemma, "name", None), enable_autonomous_investigation=self.enable_autonomous_investigation,
        )
        if self.enable_autonomous_investigation and not getattr(self.gemma, "supports_scenario_execution", True):
            logger.warning(
                "enable_autonomous_investigation=True with provider=%s: this provider is "
                "exploration_only and cannot make general semantic CRUD decisions — scenarios "
                "will still be generated/classified but are unlikely to reach a passed outcome. "
                "Configure a real reasoning provider for scenario-execution-capable runs.",
                getattr(self.gemma, "name", "unknown"),
            )
        self._last_entity_summary: dict[str, Any] = {}
        self._last_actor_summary: dict[str, Any] = {}
        executor = ActionExecutor(
            self.run_id,
            registry=registry,
            evidence=evidence,
            console_monitor=console,
            network_monitor=network,
            observer=observer,
            adapter=adapter,
            credential_vault=self._credential_vault,
        )
        executor.pacing = self.pacing

        async def _observe_once(screenshot_path: str | None = None) -> PageState:
            """Exactly one observation pass: scrape, then build the canonical
            model. Unchanged from the original single-shot behaviour."""
            started = time.monotonic()
            if adapter.supports_native_page and adapter.native_page is not None:
                state = await observer.observe(
                    adapter.native_page, screenshot_path=screenshot_path
                )
            else:
                state = await observer.observe_adapter(
                    adapter, screenshot_path=screenshot_path
                )
            await self._run_perception_engine(
                perception_engine, adapter, state, screenshot_path=screenshot_path
            )
            # Timed per PASS, not per `_observe` call: the adaptive loop may
            # sample several times, and "one observation took 4s" and "four
            # observations took 1s each" are different problems.
            self._record_span("observation", started, url=state.url,
                              elements=len(state.interactive_elements or []))
            return state

        async def _observe(screenshot_path: str | None = None) -> PageState:
            """Observe adaptively: sample, judge the evidence, and sample
            again only while the evidence is too weak to reason from.

            This replaces the previous assume-and-proceed model (navigate ->
            domcontentloaded -> treat as ready), which silently produced an
            empty observation whenever an application rendered its content
            after load. The decision to look again is made by the Adaptive
            Application Understanding Engine from observable evidence alone
            -- never from a fixed wait, and never from any knowledge of which
            technology rendered the page.

            Cost when the first observation is already good: one extra
            engine call and zero extra browser work, because the loop exits
            immediately on an `accept` decision.
            """
            state = await _observe_once(screenshot_path=screenshot_path)

            engine = self.memory.understanding_engine
            if engine is None:
                return state

            previous_model = None
            observation_pass = 1
            while True:
                model = self.memory.canonical_page_model
                if model is None:
                    return state
                try:
                    assessment = engine.assess(
                        model,
                        page_state=state,
                        previous_model=previous_model,
                        observation_pass=observation_pass,
                    )
                except Exception as exc:  # never let a judgement failure break observation
                    logger.warning("Adaptive understanding assessment failed (%s); using observation as-is", exc)
                    return state

                self._last_understanding = assessment
                if assessment.decision != "reobserve":
                    return state
                # Empty document.title on a live form/detail page is not a
                # reason to wait. The next CRUD step is already visible.
                if (state.forms and any(getattr(f, "fields", None) for f in state.forms)) or len(
                    state.interactive_elements or []
                ) >= 3:
                    logger.info(
                        "Skipping reobserve; page already has an actionable surface"
                    )
                    return state

                engine.note_reobservation()
                previous_model = model
                # Space the next sample out. This interval does NOT decide
                # readiness -- the evidence does -- it exists only because
                # two samples cannot be taken at the same instant. It grows
                # geometrically so a genuinely slow deployment still gets
                # observed, while an already-rendered page never reaches this
                # line at all and therefore waits zero milliseconds.
                try:
                    await adapter.wait(reobservation_interval_ms(observation_pass))
                except Exception:
                    pass
                observation_pass += 1
                state = await _observe_once(screenshot_path=screenshot_path)

        try:
            self._started_at = _utc_now()
            await self._phase(AgentPhase.INITIALIZING, "Initializing run")
            await self.emit(
                "run_started",
                {
                    "url": self.request.url,
                    "provider": provider_display_name(self.memory.provider_type),
                    "browser_adapter": browser_adapter_display(
                        self.memory.browser_adapter_id
                    ),
                    "capability_mode": self._capability_mode,
                    "enable_autonomous_investigation": self.enable_autonomous_investigation,
                },
            )
            await self.emit("execution_pacing", self.pacing.snapshot())
            await self._phase(AgentPhase.OPENING_BROWSER, "Opening browser")
            try:
                await adapter.start_session()
            except Exception:
                self.memory.adapter_connection_status = "failed"
                self.memory.adapter_capabilities = adapter.capabilities().to_dict()
                raise
            page = adapter.native_page  # may be None in MCP mode
            caps = adapter.capabilities()
            self.memory.adapter_connection_status = "healthy"
            self.memory.adapter_capabilities = caps.to_dict()
            unsupported: list[str] = []
            if not caps.screenshots:
                unsupported.append("screenshots")
            if not caps.console_events:
                unsupported.append("console_events")
            if not caps.network_events:
                unsupported.append("network_events")
            self.memory.unsupported_evidence_features = unsupported
            await self.emit(
                "browser_opened",
                {
                    "headless": self.config.headless,
                    "adapter": browser_adapter_display(self.memory.browser_adapter_id),
                    "capabilities": caps.to_dict(),
                },
            )

            if self.request.username and self.request.password:
                await self._phase(AgentPhase.AUTHENTICATING, "Authenticating")
                login_url = self.config.login_url or self.request.url
                username = self.request.username
                password = self.request.password
                if adapter.supports_native_page:
                    login_result = await browser.login(
                        url=login_url,
                        username=username,
                        password=password,
                        username_selector=self.config.username_selector,
                        password_selector=self.config.password_selector,
                        submit_selector=self.config.submit_selector,
                        wait_after_ms=self.config.wait_after_login_ms,
                        evidence=evidence,
                    )
                else:
                    # MCP mode: navigate only; form login via structured actions later
                    from app.schemas import LoginResult

                    await adapter.navigate(login_url)
                    login_result = LoginResult(
                        success=False,
                        method="skipped",
                        message=(
                            "Credential form login requires Direct Playwright; "
                            "navigated to login URL via adapter"
                        ),
                        before_url=login_url,
                        after_url=await adapter.get_current_url(),
                    )
                self._clear_credentials()
                del username, password
                if login_result.success and self.memory.auth_strategy is not None:
                    # See AuthenticationStrategy.mark_authenticated: this
                    # direct-credentials login never runs through
                    # evaluate_after_submit(), so without this call
                    # `auth_strategy.authenticated` stays False forever even
                    # though the real browser session IS logged in —
                    # starving every Autonomous Investigation precondition
                    # check gated on an authenticated actor.
                    self.memory.auth_strategy.mark_authenticated(
                        method=login_result.method, checkpoint_url=login_result.after_url,
                    )
                    self.memory.sync_auth_public()
                await self.emit(
                    "authentication_finished",
                    {
                        "success": login_result.success,
                        "method": login_result.method,
                        "error": sanitize_text(login_result.error or ""),
                    },
                )
            else:
                await self._phase(AgentPhase.NAVIGATING, "Navigating to start URL")
                current = await adapter.navigate(self.request.url)
                await self.emit("navigated", {"url": current})
                self._clear_credentials()

            page_state: PageState | None = None
            while not self._cancel.is_set():
                await self._wait_if_paused()
                if self._cancel.is_set():
                    break

                if evidence.total_bytes() >= self.policy.max_evidence_bytes:
                    self.memory.stop_reason = "Evidence storage budget exceeded"
                    await self.emit("run_stopping", {"reason": self.memory.stop_reason})
                    break

                def _build_frontier_for_stop_check() -> list[Any]:
                    # `page_state` is the previous iteration's OBSERVE result — should_stop
                    # is called before this iteration's OBSERVE, so it reflects where the
                    # run currently stands. On the very first iteration it's never read:
                    # should_stop returns early (nothing stalled/looping yet) before this
                    # closure would ever be invoked.
                    return self.planner._build_full_frontier(
                        page_state,
                        self.memory,
                        {
                            "allow_login": self.policy.allow_login,
                            "allow_test_account_creation": self.policy.allow_test_account_creation,
                            "allow_safe_test_data_creation": self.policy.allow_safe_test_data_creation,
                            "authorized_domain": self.authorized_domain,
                        },
                    )

                stop, reason = self.memory.should_stop(
                    self.settings.no_progress_limit,
                    build_frontier=_build_frontier_for_stop_check,
                )
                exploration_trace(
                    "iteration.stop_policy",
                    stop=stop,
                    reason=reason,
                    no_progress_streak=self.memory.no_progress_streak,
                    loop_recovery_count=self.memory.loop_recovery_count,
                    remaining_action_budget=self.memory.remaining_action_budget,
                )
                if stop:
                    self.memory.stop_reason = reason
                    await self.emit("run_stopping", {"reason": reason})
                    break

                # OBSERVE
                await self._phase(AgentPhase.OBSERVING, "Observing page")
                if adapter.supports_native_page and page is not None:
                    before_shot = await evidence.screenshot(page, "observe")
                else:
                    before_shot = await evidence.screenshot_via_adapter(adapter, "observe")
                page_state = await _observe(screenshot_path=before_shot.path)
                page_state = await self.explorer.classify(page_state)
                # Captured now (before the post-action observe overwrites it)
                # so _run_workflow_engine has a real before/after pair once an
                # action executes this iteration.
                before_workflow_model = self.memory.canonical_page_model
                # Snapshot how this screen was understood BEFORE acting, so the
                # resulting application-state transition can be recorded.
                self._understanding_before_action = self._last_understanding
                self.memory.remember_page(page_state, explored=False)
                if self.memory.auth_strategy:
                    self.memory.auth_strategy.note_page(page_state)
                    self.memory.auth_strategy.detect_forms(page_state)
                    self.memory.sync_auth_public()
                auth_trace(
                    "controller.iteration",
                    iteration=len(self.memory.actions) + 1,
                    url=page_state.url,
                    page_kind=(
                        self.memory.auth_strategy.classify_page(page_state)
                        if self.memory.auth_strategy
                        else None
                    ),
                    authenticated=self.memory.authenticated,
                    auth_status=self.memory.auth_status,
                    auth_blocker=self.memory.auth_blocker,
                    remaining_action_budget=self.memory.remaining_action_budget,
                )
                self.memory.set_modules(self.explorer.module_list())
                self.memory.note_modal_state(page_state.modals or page_state.dialogs)
                try:
                    await self._persist_page(page_state)
                except Exception as persist_exc:
                    # Never abort a run over page DB upsert races / schema drift
                    logger.warning(
                        "Page persist failed (non-fatal): %s",
                        type(persist_exc).__name__,
                    )
                await self.emit(
                    "page_observed",
                    {
                        "url": page_state.url,
                        "title": page_state.title,
                        "page_type": page_state.classification.page_type
                        if page_state.classification
                        else "unknown",
                        "elements": len(page_state.interactive_elements),
                        "fingerprint": page_state.state_fingerprint,
                    },
                )

                # Tests catalog (safe)
                scenarios = await self.tester.propose_for_page(
                    page_state,
                    self.run_id,
                    allow_controlled_writes=self.config.allow_controlled_writes
                    and not self.config.safe_mode,
                    app_store=self.memory.app_store,
                    # Deterministic scenarios stay; Gemma's ~60s generate_test_scenarios
                    # per new page is what operators see as "waiting" after load,
                    # signup, and add-contact. Do not block the live loop on it.
                    skip_llm=True,
                )
                self.memory.scenarios = self.tester.scenarios
                self.memory.executions = self.tester.executions
                if scenarios:
                    await self.emit("tests_proposed", {"count": len(scenarios)})

                # PLAN
                await self._phase(AgentPhase.PLANNING, "Planning next action")
                await self.emit("action_planning", {"budget": self.memory.remaining_action_budget})
                planning_started = time.monotonic()
                plan_context = {
                    "max_actions": self.config.max_actions,
                    "actions_taken": len(self.memory.actions),
                    "remaining_action_budget": self.memory.remaining_action_budget,
                    "remaining_page_budget": self.memory.remaining_page_budget,
                    "safe_mode": self.config.safe_mode,
                    "allow_controlled_writes": self.config.allow_controlled_writes,
                    "allow_login": self.policy.allow_login,
                    "allow_test_account_creation": self.policy.allow_test_account_creation,
                    "allow_safe_test_data_creation": self.policy.allow_safe_test_data_creation,
                    "authorized_domain": self.authorized_domain,
                    "visited_states": list(self.memory.page_fingerprints),
                    "screenshot_path": page_state.screenshot_path,
                    # The operator's own words, carried verbatim. `None`
                    # stays `None` all the way to the prompt so the model is
                    # told "no objective was provided" rather than handed an
                    # invented one (docs/MODEL_CONTEXT_AUDIT.md, J-3).
                    "testing_objective": self.config.testing_objective,
                    "run_id": self.run_id,
                    "canonical_page_model": self.memory.canonical_page_model,
                    "screenshots_taken": evidence.screenshot_count(),
                    "max_screenshots": self.policy.max_screenshots,
                    "allow_destructive_actions": self.policy.allow_destructive_actions,
                }
                cleanup_action = self.planner.next_cleanup_action(
                    page_state, self.memory, plan_context
                )
                investigation_action = None
                skip_investigation = cleanup_action is not None or self._cleanup_holds_the_turn()
                if (
                    not skip_investigation
                    and self.memory.investigation_engine is not None
                ):
                    try:
                        investigation_action = self.memory.investigation_engine.next_action(
                            page_state, self.memory, plan_context, planner=self.planner,
                        )
                    except Exception as exc:
                        logger.warning("Autonomous investigation next_action failed (%s); falling back to normal planning", exc)
                if cleanup_action is not None:
                    action = cleanup_action
                elif investigation_action is not None:
                    action = investigation_action
                else:
                    action = await self.planner.next_action(
                        page_state=page_state,
                        previous_actions=self.memory.previous_actions_payload(),
                        unexplored=[
                            u
                            for u in self.memory.unexplored_urls
                            if self.memory.same_domain(u, self.authorized_domain)
                            or self.config.allow_cross_domain
                        ][:15],
                        context=plan_context,
                        memory=self.memory,
                    )
                if (
                    action.action == ActionType.TAKE_SCREENSHOT
                    and screenshot_budget_exhausted(plan_context)
                ):
                    action = self.planner.plan_by_priority(
                        page_state, self.memory, plan_context
                    )
                    if action.action == ActionType.TAKE_SCREENSHOT:
                        action = self.planner._finish(
                            "Screenshot budget exhausted and no other safe candidates remain",
                            code="exploration_complete",
                        )
                self._record_model_context()
                self._record_span(
                    "planning", planning_started,
                    action=action.action.value,
                    element_id=action.element_id,
                    provider=getattr(self.gemma, "name", "unknown"),
                )
                await self.emit(
                    "action_planned",
                    {
                        "action": action.action.value,
                        "reason": action.reason,
                        "element_id": action.element_id,
                        "category": action.category.value if action.category else None,
                        **live_action_fields(action, page_state),
                    },
                )
                if (action.metadata or {}).get("ai_failure"):
                    await self.emit(
                        "ai_failure",
                        {
                            "fallback": bool((action.metadata or {}).get("fallback")),
                            "provider_exhausted": bool(
                                (action.metadata or {}).get("provider_exhausted")
                            ),
                            "reason": (action.reason or "")[:240],
                            "provider": getattr(self.gemma, "name", "unknown"),
                        },
                    )

                if action.action == ActionType.FINISH:
                    auth_trace(
                        "controller.finish_produced",
                        reason=action.reason,
                        authenticated=bool(
                            self.memory.auth_strategy and self.memory.auth_strategy.authenticated
                        ),
                        auth_status=self.memory.auth_status,
                    )
                    # Never accept frontier exhaustion while auth remains unresolved
                    if self.memory.auth_strategy and not self.memory.auth_strategy.authenticated:
                        blocker = self.memory.auth_strategy.unresolved_auth_blocker(
                            page_state,
                            allow_login=self.policy.allow_login,
                            allow_registration=self.policy.allow_test_account_creation,
                        )
                        reason = action.reason or ""
                        if blocker or any(
                            k in reason.lower()
                            for k in (
                                "no remaining",
                                "no safe",
                                "frontier",
                                "exhausted",
                            )
                        ):
                            self.memory.stop_reason = blocker or reason or "authentication_required"
                            self.memory.auth_blocker = self.memory.stop_reason
                            auth_trace(
                                "controller.finish_converted_to_blocker",
                                blocker=self.memory.stop_reason,
                            )
                            self.memory.finalize_workflow()
                            break
                    stop_code = (action.metadata or {}).get("stop_reason_code")
                    self.memory.stop_reason = stop_code or action.reason or "finish"
                    auth_trace("controller.finish_accepted", stop_reason=self.memory.stop_reason)
                    self.memory.finalize_workflow()
                    break

                # VALIDATE
                validation = self.validator.validate(
                    action,
                    page_state=page_state,
                    actions_taken=len(self.memory.actions),
                    pages_visited=len(self.memory.visited_urls),
                    screenshots_taken=evidence.screenshot_count(),
                    runtime_seconds=self._runtime_seconds(),
                )
                audit = safety_audit.record(
                    run_id=self.run_id,
                    proposed_action=action.action.value,
                    action_level=validation.action_level.value,
                    safety_decision=validation.safety_decision,
                    execution_decision=validation.execution_decision,
                    block_reason=validation.reason if not validation.allowed else "",
                    matched_pattern=validation.matched_pattern,
                    element_id=action.element_id,
                    target_url=action.url or action.value,
                    details={"risk": action.risk.value if action.risk else None},
                )
                await self.emit("safety_decision", audit.to_payload())

                if not validation.allowed:
                    self.memory.decisions_rejected += 1
                    if "cross-domain" in validation.reason.lower() or "out-of-scope" in validation.reason.lower():
                        blocked_el = find_element(page_state, action.element_id)
                        if blocked_el and blocked_el.href:
                            self.memory.remember_blocked_href(blocked_el.href)
                    await self.emit(
                        "action_blocked",
                        {
                            "reason": validation.reason,
                            "action": action.action.value,
                            "action_level": validation.action_level.value,
                            "matched_pattern": validation.matched_pattern,
                            "policy_rule": validation.policy_rule,
                            "audit_id": audit.audit_id,
                            **live_action_fields(action, page_state),
                        },
                    )
                    # Count as failed/no-progress attempt without executing
                    current_url = page_state.url or await adapter.get_current_url()
                    blocked = ActionResult(
                        action_id=new_id(),
                        run_id=self.run_id,
                        action=action,
                        success=False,
                        message=f"Blocked: {validation.reason}",
                        error=validation.reason,
                        before_url=current_url,
                        after_url=current_url,
                        before_fingerprint=page_state.state_fingerprint,
                        after_fingerprint=page_state.state_fingerprint,
                    )
                    self.memory.remember_action(
                        blocked,
                        before_fingerprint=page_state.state_fingerprint,
                        value_category=(action.metadata or {}).get("value_category"),
                        made_progress=False,
                    )
                    await self._persist_action(blocked)
                    if (
                        action.action == ActionType.TAKE_SCREENSHOT
                        and "screenshot budget exhausted" in (validation.reason or "").lower()
                    ):
                        self.memory.stop_reason = "screenshot_budget_exhausted"
                        auth_trace(
                            "controller.finish_accepted",
                            stop_reason=self.memory.stop_reason,
                        )
                        self.memory.finalize_workflow()
                        break
                    continue

                action = validation.sanitized_action or action
                self.memory.decisions_validated += 1
                value_category = (action.metadata or {}).get("value_category")

                # EXECUTE (+ evidence inside executor; also emit explicit events)
                await self._phase(AgentPhase.EXECUTING, f"Executing {action.action.value}")
                await self.emit(
                    "action_started",
                    {
                        "action": action.action.value,
                        "element_id": action.element_id,
                        "url": page_state.url or await adapter.get_current_url(),
                        **live_action_fields(action, page_state),
                    },
                )
                before_fp = page_state.state_fingerprint
                before_modals = list(page_state.modals or page_state.dialogs)

                execution_started = time.monotonic()
                result = await executor.execute(
                    action=action,
                    page=page,
                    page_state=page_state,
                    capture_evidence=True,
                )
                self._record_span(
                    "execution", execution_started,
                    action=action.action.value,
                    element_id=action.element_id,
                    success=bool(result.success),
                )
                # The outcome was never emitted — only the intention
                # (`action_planned`) was, so a live watcher saw GemmaQA decide to
                # click and never learned whether the click worked.
                await self.emit(
                    "action_executed",
                    {
                        "action": action.action.value,
                        "element_id": action.element_id,
                        "success": bool(result.success),
                        "error": (result.error or result.message or "") if not result.success else "",
                        "url": result.after_url,
                        **live_action_fields(action, page_state),
                    },
                )
                self.memory.adapter_execution_failures = (
                    executor.adapter_execution_failures
                )

                # Post-navigation scope enforcement
                after_url = result.after_url or page_state.url or await adapter.get_current_url()
                nav_check = self.validator.validate_navigation_result(after_url)
                if not nav_check.allowed:
                    safety_audit.record(
                        run_id=self.run_id,
                        proposed_action=action.action.value,
                        action_level=nav_check.action_level.value,
                        safety_decision="block",
                        execution_decision="skip",
                        block_reason=nav_check.reason,
                        matched_pattern=nav_check.matched_pattern,
                        target_url=result.after_url,
                    )
                    await self.emit(
                        "action_blocked",
                        {"reason": nav_check.reason, "action": "navigation_result"},
                    )
                    try:
                        await adapter.navigate(self.request.url)
                    except Exception:
                        pass

                await self.emit(
                    "action_finished",
                    {
                        "action": action.action.value,
                        "success": result.success,
                        "message": sanitize_text(result.message),
                        "url": result.after_url,
                        "evidence_ids": result.evidence_ids,
                    },
                )

                # COMPARE — re-observe after action
                await self._phase(AgentPhase.ANALYZING, "Analyzing outcome")
                if adapter.supports_native_page and page is not None:
                    after_shot = await evidence.screenshot(page, "after_compare")
                else:
                    after_shot = await evidence.screenshot_via_adapter(
                        adapter, "after_compare"
                    )
                after_state = await _observe(screenshot_path=after_shot.path)
                after_state = await self.explorer.classify(after_state)
                self.memory.remember_page(after_state, explored=True)
                self.memory.set_modules(self.explorer.module_list())
                self.memory.note_modal_state(after_state.modals or after_state.dialogs)
                self._run_workflow_engine(before_model=before_workflow_model, action=action, result=result)
                self._run_dependency_engine(before_model=before_workflow_model, action=action, result=result)
                self._run_crud_discovery(before_model=before_workflow_model, action=action, result=result)
                self._run_knowledge_graph_sync()
                self._run_goal_generation()
                self._run_scenario_planning()
                self._run_qa_strategy()
                self._run_autonomous_investigation(before_state=page_state, after_state=after_state, action=action, result=result)
                self._record_state_transition(action=action, result=result)

                if self.memory.auth_strategy:
                    auth = self.memory.auth_strategy
                    if action.action == ActionType.INSPECT_FORM and action.element_id:
                        auth.note_form_inspected(action.element_id)
                        if self.memory.app_store:
                            self.memory.app_store.mark_form_inspected(action.element_id)
                    auth.on_action_result(action, result.success)
                    meta = action.metadata or {}
                    if meta.get("auth_submit") or (
                        meta.get("auth_write")
                        and auth.active_workflow is None
                        and meta.get("auth_method")
                    ):
                        evaluation = auth.evaluate_after_submit(
                            page_state,
                            after_state,
                            method=str(meta.get("auth_method") or "login"),
                        )
                        await self.emit(
                            "authentication_evaluated",
                            {
                                "authenticated": evaluation.authenticated,
                                "method": evaluation.method,
                                "confidence": evaluation.confidence,
                                "signals": evaluation.signals,
                                "rejected": evaluation.rejected,
                            },
                        )
                        # Registration may land on login — immediately plan login next loop
                        if (
                            not evaluation.authenticated
                            and meta.get("auth_method") == "registration"
                            and auth.classify_page(after_state) == "login"
                            and auth.vault.get()
                        ):
                            forms = [
                                f for f in auth.detect_forms(after_state) if f.kind == "login"
                            ]
                            if forms:
                                auth.build_login_workflow(after_state, forms[0])
                    auth.note_page(after_state)
                    self.memory.sync_auth_public()

                form_wf = self.memory.active_form_workflow
                if form_wf is not None and (action.metadata or {}).get("form_workflow_write"):
                    if (action.metadata or {}).get("form_workflow_submit"):
                        # Judge the submit by what the APPLICATION did with the
                        # data, not by whether the click executed. A submit that
                        # the server answered with 4xx/5xx is not a completed
                        # write, however cleanly the button was pressed.
                        outcome = None
                        try:
                            outcome = classify_submission(
                                before_state=page_state,
                                after_state=after_state,
                                form_id=form_wf.form_id,
                                fields=form_wf.fields,
                                click_succeeded=bool(result.success),
                            )
                            self.memory.record_submission_outcome(form_wf.form_id, outcome)
                        except Exception as exc:  # pragma: no cover - defensive
                            logger.warning(
                                "Submission-outcome classification failed (%s); falling back to click result",
                                type(exc).__name__,
                            )
                        form_wf.note_result(success=result.success, outcome=outcome)
                        if form_wf.state == "verified":
                            self.memory.safe_writes_completed += 1
                            self._verify_and_register_temporary_record(form_wf, before_state=page_state, after_state=after_state)
                            self._advance_record_lifecycle_on_update(form_wf, after_state=after_state)
                        # `submitted_unverified` belongs here too. It ends the
                        # workflow but used to leave the form eligible to START
                        # AGAIN, so a form whose submissions can never be proven
                        # accepted was restarted indefinitely: 27 fill+submit
                        # cycles per run, ~60% of the action budget, with a fresh
                        # workflow each time so no per-workflow limit ever applied.
                        # Unprovable is not a reason to keep trying.
                        if form_wf.state in {"failed", "blocked", "skipped", "submitted_unverified"}:
                            self.memory.failed_form_workflow_counts[form_wf.form_id] += 1
                            await self._emit_form_abandoned(form_wf)
                        if form_wf.state in {"verified", "failed", "blocked", "skipped", "submitted_unverified"}:
                            self.memory.note_form_workflow_finished(form_wf)
                            self.memory.active_form_workflow = None
                    elif not result.success:
                        # A fill/select/check step failed. Record the FIELD and
                        # carry on with the others — routing this through
                        # `note_result` sent the workflow to `ready_to_submit`,
                        # so one unfillable field skipped every field after it
                        # and submitted the form half-empty.
                        if UNSATISFIED_REFERENCE_MARKER in (result.error or ""):
                            # Not a flaky fill. The field names a record the
                            # application does not hold, discovered by typing into
                            # it (a lookup is indistinguishable from a text box
                            # until then). Retrying cannot conjure the record, so
                            # this is a finding to report, not an attempt to spend.
                            form_wf.note_unsatisfied_reference(
                                action.element_id or "", result.error or ""
                            )
                        else:
                            form_wf.note_field_failed(action.element_id or "")
                        if form_wf.state in {"failed", "blocked", "skipped"}:
                            self.memory.failed_form_workflow_counts[form_wf.form_id] += 1
                            await self._emit_form_abandoned(form_wf)
                            self.memory.note_form_workflow_finished(form_wf)
                            self.memory.active_form_workflow = None

                meta = action.metadata or {}
                if result.success and meta.get("decision") == "safe_test_data_creation":
                    self.memory.safe_writes_completed += 1
                if result.success:
                    self._advance_cleanup_from_live_action(action, after_state)

                progress = self._detect_progress(
                    before_state=page_state,
                    after_state=after_state,
                    result=result,
                    before_modals=before_modals,
                )
                result.page_state_changed = progress["changed"]
                result.before_fingerprint = before_fp
                result.after_fingerprint = after_state.state_fingerprint
                result.after_screenshot = result.after_screenshot or after_shot.path

                self.memory.remember_action(
                    result,
                    before_fingerprint=before_fp,
                    value_category=str(value_category) if value_category else None,
                    made_progress=progress["changed"],
                )
                self.memory.remember_evidence(evidence.items[-8:])
                await self._persist_action(result)

                exploration_trace(
                    "iteration.result",
                    action=action.action.value,
                    element_id=action.element_id,
                    success=result.success,
                    progress=progress,
                    state_transition={
                        "before_url": result.before_url,
                        "after_url": result.after_url,
                        "before_fingerprint": before_fp,
                        "after_fingerprint": after_state.state_fingerprint,
                    },
                    no_progress_streak=self.memory.no_progress_streak,
                    loop_recovery_count=self.memory.loop_recovery_count,
                )

                # DETECT ISSUES
                bugs = await self.bug_analyzer.analyze_page(
                    after_state,
                    self.run_id,
                    action_result=result,
                    before_state=page_state,
                )
                for bug in bugs:
                    self.memory.remember_bug(bug)
                    await self._persist_bug(bug)
                    await self.emit(
                        "bug_found",
                        {
                            "title": bug.title,
                            "severity": bug.severity.value,
                            "tags": bug.tags,
                        },
                    )
                if progress.get("new_bug"):
                    pass

                await self.emit(
                    "state_compared",
                    {
                        "progress": progress,
                        "no_progress_streak": self.memory.no_progress_streak,
                        "remaining_action_budget": self.memory.remaining_action_budget,
                    },
                )

                # DOCUMENT
                await self._phase(AgentPhase.DOCUMENTING, "Updating documentation")
                await self.documenter.sync(self.memory)
                await self.emit(
                    "documentation_updated",
                    {"sections": list(self.memory.doc_sections.keys())},
                )

                await self._update_run(
                    actions_taken=len(self.memory.actions),
                    pages_visited=len(self.memory.visited_urls),
                    bugs_found=len(self.memory.bugs),
                    current_url=after_state.url or await adapter.get_current_url(),
                    message=f"Last action: {action.action.value}",
                )

            if self._cancel.is_set():
                await self._phase(AgentPhase.CANCELLED, "Cancelled")
                self.memory.stop_reason = self.memory.stop_reason or "cancelled"
                await self._update_run(
                    status=RunStatusEnum.CANCELLED,
                    message="Run cancelled",
                    finished=True,
                )
                await self.emit("run_cancelled", {"reason": self.memory.stop_reason})
                return

            await self._run_cleanup_pass(
                executor=executor, page=page, observe=_observe, evidence=evidence, adapter=adapter,
            )

            # Final docs + report
            await self._phase(AgentPhase.DOCUMENTING, "Building final report")
            self.memory.finalize_workflow()
            await self.documenter.sync(self.memory)
            report = self.report_builder.build(self.memory)
            # One optional polish AFTER the final deterministic build. Do not
            # rebuild afterward — that would overwrite a successful polish.
            report = await self.documenter.polish_final_executive(self.memory, report)
            self.memory.doc_sections = dict(report.sections_markdown)
            exporter = ReportExporter(self.run_id)
            paths = exporter.export_all(report, report.sections_markdown)

            await self._phase(AgentPhase.COMPLETED, "Completed")
            # A stop code alone reads as "GemmaQA gave up" when the truth is
            # "GemmaQA was not permitted". If policy withheld work the
            # application was offering, that belongs in the headline, not only
            # in the config JSON the operator would have to go and read.
            completion_message = f"Completed ({self.memory.stop_reason or 'done'})"
            permission_gap = self.memory.write_permission_gap()
            if permission_gap:
                completion_message += " — " + str(permission_gap["explanation"])
            await self._update_run(
                status=RunStatusEnum.COMPLETED,
                message=completion_message,
                progress_pct=100.0,
                finished=True,
                report_json=report.model_dump_json(),
                application_json=(
                    self.memory.app_store.model.model_dump_json()
                    if self.memory.app_store
                    else None
                ),
                bugs_found=len(self.memory.bugs),
                pages_visited=(
                    len(self.memory.app_store.model.visited_pages())
                    if self.memory.app_store
                    else len(self.memory.visited_urls)
                ),
                actions_taken=len(self.memory.actions),
            )
            await self.emit(
                "run_completed",
                {
                    "report_paths": paths,
                    "bugs": len(self.memory.bugs),
                    "pages": len(self.memory.visited_urls),
                    "stop_reason": self.memory.stop_reason,
                    "evidence_dir": str(evidence.root),
                },
            )

        except Exception as exc:
            logger.exception("Run failed: %s", exc)
            public_error = (
                f"{type(exc).__name__}: {exc}"
                if self.settings.debug
                else "An internal error occurred during the run"
            )
            await self._phase(AgentPhase.FAILED, "Failed")
            await self._update_run(
                status=RunStatusEnum.FAILED,
                message="Run failed",
                error=public_error,
                finished=True,
            )
            await self.emit("run_failed", {"error": public_error})
        finally:
            self._purge_vault()
            try:
                await adapter.close_session()
            except Exception:
                await browser.stop()
            if browser.trace_path and browser.trace_path.exists():
                try:
                    evidence.record_trace(browser.trace_path)
                except Exception:
                    pass

    async def _run_perception_engine(
        self,
        perception_engine: PerceptionEngine,
        adapter: Any,
        state: PageState,
        *,
        screenshot_path: str | None,
    ) -> None:
        """Run the Universal Page Perception Engine alongside every
        observation (initial load, and after every action — which already
        covers navigation/modal-open/tab-change/dropdown-expand/form-submit,
        since each of those is just a kind of action followed by the SAME
        post-action observe). Best-effort and non-fatal: a perception failure
        must never break the exploration loop the way it currently works —
        Planner/FrontierBuilder keep reading `PageState` exactly as before.
        """
        try:
            if adapter.supports_native_page and adapter.native_page is not None:
                model = await perception_engine.observe(
                    adapter.native_page,
                    screenshot_path=screenshot_path,
                    network_entries=state.network_entries,
                )
            else:
                model = await perception_engine.observe_adapter(adapter, state)
            self.memory.canonical_page_model = model
            # Stamped from the SAME observation cycle's PageState — this, not
            # model.state_fingerprint (a deliberately different, richer
            # algorithm), is what FrontierBuilder compares against to detect a
            # stale model. See RunMemory.canonical_page_model_source_fingerprint.
            self.memory.canonical_page_model_source_fingerprint = state.state_fingerprint
            self.memory.remember_collections(model)
            # Entity discovery over the fresh model — best-effort, same
            # non-fatal discipline as perception itself.
            entity_engine = getattr(self, "_entity_engine", None)
            if entity_engine is not None:
                try:
                    self._last_entity_summary = entity_engine.observe(model, iteration=len(self.memory.actions))
                except Exception as entity_exc:
                    logger.warning(
                        "Entity discovery failed for %s (%s); continuing", state.url, entity_exc
                    )
            # Actor discovery — best-effort, same non-fatal discipline,
            # separate try/except so a failure here never blocks entity
            # discovery (or vice versa).
            actor_engine = getattr(self, "_actor_engine", None)
            if actor_engine is not None:
                try:
                    auth = self.memory.auth_strategy
                    self._last_actor_summary = actor_engine.observe(
                        model,
                        iteration=len(self.memory.actions),
                        authenticated=bool(auth and auth.authenticated),
                        login_method=(auth.method if auth else None),
                        entity_registry=self.memory.entity_registry,
                    )
                except Exception as actor_exc:
                    logger.warning(
                        "Actor discovery failed for %s (%s); continuing", state.url, actor_exc
                    )
            # Form intent classification — best-effort, same non-fatal
            # discipline. `model.form_intents` was already computed by
            # PerceptionEngine (app.perception.form_intent_classifier); this
            # just records each classification onto the matching AppForm so
            # the CLASSIFIED lifecycle step (app.agent.form_lifecycle) and
            # coverage reporting can see WHY a form was labelled create vs
            # search vs edit, never a re-classification of its own.
            if self.memory.app_store:
                try:
                    for intent in model.form_intents:
                        self.memory.app_store.mark_form_classified(
                            intent.form_id,
                            intent=intent.intent,
                            confidence=intent.confidence.value,
                            target_entity=intent.target_entity_hypothesis,
                            operation=intent.operation_hypothesis,
                            evidence=intent.evidence,
                            alternatives=[f"{a.intent}:{a.confidence}:{a.reason}" for a in intent.alternatives],
                        )
                except Exception as intent_exc:
                    logger.warning(
                        "Form intent classification recording failed for %s (%s); continuing", state.url, intent_exc
                    )
        except Exception as exc:
            logger.warning("Perception engine failed for %s (%s); continuing with PageState only", state.url, exc)

    def _run_workflow_engine(
        self,
        *,
        before_model: Any | None,
        action: BrowserAction,
        result: ActionResult,
    ) -> None:
        """Workflow discovery needs a before/after CanonicalPageModel pair
        plus the action actually executed between them — unlike entity/actor
        discovery, it cannot run from a single observation, so it is called
        directly from the main loop (see `run()`) rather than from
        `_run_perception_engine`. Best-effort and non-fatal: a failure here
        must never break exploration, matching entity/actor discovery's own
        discipline exactly."""
        workflow_engine = getattr(self, "_workflow_engine", None)
        after_model = self.memory.canonical_page_model
        if workflow_engine is None or before_model is None or after_model is None:
            return
        try:
            auth = self.memory.auth_strategy
            workflow_engine.observe(
                before_model=before_model,
                after_model=after_model,
                executed_element_id=action.element_id,
                action_succeeded=bool(result.success),
                iteration=len(self.memory.actions),
                authenticated=bool(auth and auth.authenticated),
                current_actor_term=(self._last_actor_summary or {}).get("session_actor"),
                primary_entity_term=(self._last_entity_summary or {}).get("page_context_entity"),
                entity_registry=self.memory.entity_registry,
                actor_registry=self.memory.actor_registry,
            )
        except Exception as exc:
            logger.warning("Workflow discovery failed for %s (%s); continuing", after_model.url, exc)

    def _run_dependency_engine(
        self,
        *,
        before_model: Any | None,
        action: BrowserAction,
        result: ActionResult,
    ) -> None:
        """Dependency discovery needs the same before/after
        `CanonicalPageModel` pair as workflow discovery (see
        `_run_workflow_engine`) plus the Entity/Actor/Workflow registries as
        cross-reference input — so it runs from this same post-action hook,
        right after workflow discovery. Best-effort and non-fatal, identical
        discipline to every other intelligence engine hook."""
        dependency_engine = getattr(self, "_dependency_engine", None)
        after_model = self.memory.canonical_page_model
        if dependency_engine is None or before_model is None or after_model is None:
            return
        try:
            dependency_engine.observe(
                before_model=before_model,
                after_model=after_model,
                executed_element_id=action.element_id,
                action_succeeded=bool(result.success),
                iteration=len(self.memory.actions),
                current_actor_term=(self._last_actor_summary or {}).get("session_actor"),
                entity_registry=self.memory.entity_registry,
                actor_registry=self.memory.actor_registry,
                workflow_registry=self.memory.workflow_registry,
            )
        except Exception as exc:
            logger.warning("Dependency discovery failed for %s (%s); continuing", after_model.url, exc)

    def _run_crud_discovery(
        self,
        *,
        before_model: Any | None,
        action: BrowserAction,
        result: ActionResult,
    ) -> None:
        """CRUD Discovery needs the same before/after `CanonicalPageModel`
        pair as workflow/dependency discovery — called from the same
        post-action hook. Best-effort and non-fatal, identical discipline
        to every other intelligence engine hook. Produces hypotheses only;
        never executes a browser action."""
        crud_engine = getattr(self, "_crud_engine", None)
        after_model = self.memory.canonical_page_model
        if crud_engine is None or before_model is None or after_model is None:
            return
        try:
            crud_engine.observe(
                before_model=before_model,
                after_model=after_model,
                executed_element_id=action.element_id,
                action_succeeded=bool(result.success),
                iteration=len(self.memory.actions),
                current_actor_term=(self._last_actor_summary or {}).get("session_actor"),
            )
        except Exception as exc:
            logger.warning("CRUD discovery failed for %s (%s); continuing", after_model.url, exc)

    def _run_knowledge_graph_sync(self) -> None:
        """Synchronises the Application Knowledge Graph from whatever the
        Entity/Actor/Workflow/Dependency registries currently hold — a
        semantic PROJECTION, never an independent rediscovery. Runs last in
        the per-action intelligence pipeline so every registry has already
        been updated this iteration. Best-effort and non-fatal, identical
        discipline to every other intelligence engine hook."""
        graph = self.memory.knowledge_graph
        if graph is None:
            return
        try:
            graph.synchronize(
                entity_registry=self.memory.entity_registry, actor_registry=self.memory.actor_registry,
                workflow_registry=self.memory.workflow_registry, dependency_registry=self.memory.dependency_registry,
                crud_registry=self.memory.crud_registry, iteration=len(self.memory.actions),
            )
        except Exception as exc:
            logger.warning("Knowledge graph synchronisation failed (%s); continuing", exc)

    def _run_goal_generation(self) -> None:
        """Runs Goal Generation from whatever the Knowledge Graph currently
        holds -- the first reasoning engine that CONSUMES the graph rather
        than projecting into it. Runs immediately after the knowledge graph
        sync so it always reasons over the freshest graph version this
        iteration. Best-effort and non-fatal, identical discipline to every
        other intelligence engine hook. Never executes a browser action."""
        goal_engine = self.memory.goal_engine
        graph = self.memory.knowledge_graph
        if goal_engine is None or graph is None:
            return
        try:
            goal_engine.generate(graph, iteration=len(self.memory.actions))
        except Exception as exc:
            logger.warning("Goal generation failed (%s); continuing", exc)

    def _run_scenario_planning(self) -> None:
        """Converts whatever goals Goal Generation currently holds into
        declarative, browser-independent investigation scenarios. Runs
        immediately after goal generation so it always plans against the
        freshest goal set this iteration. Best-effort and non-fatal,
        identical discipline to every other intelligence engine hook.
        Never executes a browser action, never switches actors, never
        touches ActionExecutor/BrowserAdapter, and never modifies the
        runtime Planner's behaviour."""
        scenario_engine = self.memory.scenario_engine
        goal_engine = self.memory.goal_engine
        if scenario_engine is None or goal_engine is None:
            return
        try:
            scenario_engine.generate(goal_engine, self.memory.knowledge_graph, iteration=len(self.memory.actions))
        except Exception as exc:
            logger.warning("Scenario planning failed (%s); continuing", exc)

    def _run_qa_strategy(self) -> None:
        """Converts whatever scenarios Scenario Planning currently holds
        into a prioritised, queued, batched execution strategy. Runs
        immediately after scenario planning so it always strategises over
        the freshest scenario set this iteration. Best-effort and
        non-fatal, identical discipline to every other intelligence engine
        hook. Never executes a browser action, never switches actors,
        never touches ActionExecutor/BrowserAdapter, and never modifies
        the runtime Planner's behaviour -- it only ranks and groups what
        Scenario Planning already produced."""
        strategy_engine = self.memory.strategy_engine
        scenario_engine = self.memory.scenario_engine
        if strategy_engine is None or scenario_engine is None:
            return
        try:
            strategy_engine.generate(scenario_engine, self.memory.goal_engine, self.memory.knowledge_graph)
        except Exception as exc:
            logger.warning("QA strategy generation failed (%s); continuing", exc)

    def _record_model_context(self) -> None:
        """Record how THIS iteration was planned, and how its context was built.

        Not every iteration reaches the model: GemmaQA plans deterministically
        from the unified frontier whenever it can, and only asks the model when
        no deterministic candidate dispatches. Both outcomes are recorded, so a
        report can state how many decisions were model-driven rather than
        leaving the reader to assume. Non-fatal like every observability hook.
        """
        try:
            provider = self.planner.gemma
            sequence = int(getattr(provider, "generation_sequence", 0) or 0)
            prepared = getattr(provider, "last_generation_request", None)

            if prepared is None or sequence == self._last_generation_sequence:
                # No model call was prepared this iteration. Any lingering
                # `last_generation_request` belongs to an earlier one.
                self.memory.record_model_context(
                    {
                        "provider": provider.name,
                        "decision_path": "deterministic_frontier",
                        "real_prompt_construction_exercised": False,
                    }
                )
                return

            self._last_generation_sequence = sequence
            metadata = prepared.context_metadata()
            metadata["provider"] = provider.name
            metadata["decision_path"] = "model"
            metadata["real_prompt_construction_exercised"] = True
            registry = getattr(prepared, "evidence", None)
            if registry is not None:
                metadata["rejected_evidence_ids"] = len(getattr(registry, "rejected_ids", []) or [])
            self.memory.record_model_context(metadata)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Model-context recording skipped (%s)", type(exc).__name__)

    def _record_state_transition(self, *, action: BrowserAction, result: ActionResult) -> None:
        """Record `application state A --action--> application state B` from
        how the screen was understood before and after this action.

        This is the behaviour axis: not "URL A led to URL B", but "a
        collection screen led to a detail screen when this control was
        used". Repeated observations of the same transition raise confidence
        in it; the same action leading somewhere different than last time is
        raised as a behaviour contradiction rather than silently overwriting
        the earlier belief. Best-effort and non-fatal, identical discipline
        to every other intelligence hook."""
        engine = self.memory.understanding_engine
        before = self._understanding_before_action
        after = self._last_understanding
        if engine is None or before is None or after is None:
            return
        try:
            engine.note_transition(
                before=before,
                after=after,
                action_type=action.action.value if action.action else "",
                action_label=str((action.metadata or {}).get("action_label") or ""),
            )
            engine.note_navigation_result(
                str(result.after_url or ""),
                reachable=bool(result.success),
                failure_detail=str(result.error or ""),
            )
        except Exception as exc:
            logger.warning("Adaptive understanding transition recording failed (%s); continuing", exc)

    def _run_autonomous_investigation(
        self, *, before_state: PageState, after_state: PageState, action: BrowserAction, result: ActionResult,
    ) -> None:
        """Reconciles whatever in-flight investigation the Autonomous
        Investigation Engine's PLAN-phase hook (`investigation_engine.
        next_action()`, called above the normal Planner path this same
        iteration) may have just driven: advances to the next step,
        retries, or finalises it -- using the SAME `ActionResult` this
        iteration's VALIDATE -> EXECUTE -> COMPARE pipeline already
        produced, unchanged. A no-op whenever autonomous investigation is
        disabled, or when this iteration's action came from normal
        exploration rather than an in-flight investigation (the engine
        checks the action's own `investigation_step_id` metadata tag
        before touching anything). Best-effort and non-fatal, identical
        discipline to every other intelligence engine hook."""
        investigation_engine = self.memory.investigation_engine
        if investigation_engine is None:
            return
        try:
            investigation_engine.observe_step_result(
                before_state=before_state, after_state=after_state, result=result, run_memory=self.memory, action=action,
            )
        except Exception as exc:
            logger.warning("Autonomous investigation step reconciliation failed (%s); continuing", exc)

    async def _emit_form_abandoned(self, form_wf: Any) -> None:
        """Say WHY a form was given up on.

        `blocked` alone tells an operator nothing actionable, and a run that
        quietly stops working on a form is indistinguishable from one that never
        found it. One helper because a form can now be abandoned at two points —
        a refused submit, and a fill that hit an unsatisfiable reference — and
        only the first used to report anything.
        """
        references = list(getattr(form_wf, "unsatisfied_references", []) or [])
        self.memory.note_unsatisfied_references(form_wf.form_id, references)
        await self.emit(
            "form_abandoned",
            {
                "form_id": form_wf.form_id,
                "state": form_wf.state,
                "reason": getattr(form_wf, "blocked_reason", "")
                or form_wf.last_error
                or "no reason recorded",
                "attempts": form_wf.attempts,
                "unsatisfied_references": references,
            },
        )

    def _verify_and_register_temporary_record(
        self, form_wf: Any, *, before_state: PageState, after_state: PageState,
    ) -> None:
        """The direct fix for "do not treat a successful click as successful
        creation": `form_wf.state == "verified"` (set by `note_result`) only
        means the SUBMIT action itself didn't error — it is NOT evidence a
        record was actually created. This looks for independent evidence
        (toast/redirect/record-detail/list-row — see
        app.agent.creation_verifier) before registering anything in the
        Temporary Record Registry. A form whose submit succeeded but whose
        creation could not be verified is intentionally left UNREGISTERED
        (and therefore never a cleanup candidate) rather than silently
        assumed to have worked. Best-effort and non-fatal, identical
        discipline to every other post-action hook."""
        registry = self.memory.temporary_record_registry
        if registry is None or form_wf.purpose != "safe_test_data_creation":
            return
        try:
            model = self.memory.canonical_page_model
            collections = list(model.collections) if model is not None else None
            verification = verify_creation(
                before_url=before_state.url, after_url=after_state.url,
                after_visible_text=after_state.visible_text_summary or "",
                after_toasts=list(after_state.toasts or []),
                expected_value=form_wf.primary_identity_value,
                after_collections=collections,
            )
            if not verification.verified:
                logger.info(
                    "Form workflow %s submitted but creation could not be independently verified "
                    "(no toast/redirect/record-detail/list-row evidence) — not registering a temporary record",
                    form_wf.workflow_id,
                )
                return
            collection_element_id = next(
                (c.element_id for c in (collections or []) if "list_row" in verification.signals), None
            )
            entry = registry.register_created(
                record_type=form_wf.primary_identity_semantic_type or "record",
                generated_identity=form_wf.primary_identity_value or "",
                creation_action_element_id=form_wf.submit_element_id,
                creation_form_id=form_wf.form_id,
                collection_element_id=collection_element_id,
                # Where the record was seen after creation — cleanup's way back
                # to it once the run has moved on somewhere else.
                list_url=after_state.url,
            )
            registry.mark_verified(entry.temporary_record_id, evidence=verification.evidence)
        except Exception as exc:
            logger.warning("Temporary-record verification/registration failed (%s); continuing", exc)

    def _advance_record_lifecycle_on_update(self, form_wf: Any, *, after_state: PageState) -> None:
        """`verified` -> `updated` in the Temporary Record Registry.

        Only reached when `note_result` set the workflow to `verified`, which
        (since the submission-outcome work) means the APPLICATION accepted the
        submit — an edit answered with 4xx never gets here, so a refused update
        is never recorded as one.

        Until now the registry never advanced past `verified`: nothing ever
        called `mark_updated`, so a record GemmaQA edited looked identical to one
        it had merely created.
        """
        registry = self.memory.temporary_record_registry
        if registry is None or form_wf.purpose != "safe_test_data_update":
            return
        element_id = getattr(form_wf, "updated_field_element_id", None)
        if not element_id:
            return
        try:
            after_value = getattr(form_wf, "updated_field_after_value", None) or ""
            # The new value being visible on the resulting page is independent
            # evidence the update persisted, as opposed to merely being typed.
            visible = after_value and after_value in (after_state.visible_text_summary or "")
            evidence = [f"submission_accepted:{form_wf.form_id}"]
            if visible:
                evidence.append("updated_value_visible_after_submit")

            for entry in registry.entries.values():
                if entry.current_state != "verified":
                    continue
                registry.mark_updated(
                    entry.temporary_record_id,
                    field_key=str(element_id),
                    before_value=getattr(form_wf, "updated_field_before_value", None),
                    after_value=after_value or None,
                    verified=bool(visible),
                    evidence=evidence,
                )
                logger.info(
                    "Temporary record %s advanced to 'updated' (field=%s verified=%s)",
                    entry.temporary_record_id, element_id, bool(visible),
                )
                break
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Record-lifecycle update advance failed (%s); continuing", exc)

    async def _reauthenticate_for_cleanup(
        self, *, page_state: PageState, executor: Any, page: Any, validator: Any,
        observe: Callable[..., Awaitable[PageState]],
    ) -> PageState | None:
        """Sign back in so cleanup can reach the record it must remove.

        Cleanup is an epilogue, so by the time it runs the session may be gone —
        and then navigating back to the record's list lands on a login screen and
        the collection is empty for a reason that has nothing to do with the
        record. GemmaQA was holding the credentials and standing outside the
        locked door.

        Reuses the SAME `AuthenticationStrategy` the run already used: its own
        form detection, its own fill plan, its own post-submit evaluation. No
        second login implementation — that is exactly how this codebase grew the
        duplicate-rule bugs it spent a session unpicking.

        Every step still goes through the caller's validator and executor, so the
        safety gates that apply to a login mid-run apply here identically.
        Returns the observation after a SUCCESSFUL sign-in, or None.
        """
        auth = self.memory.auth_strategy
        if auth is None or auth.vault.get() is None:
            return None

        login_form = next(
            (f for f in auth.detect_forms(page_state) if f.kind == "login"), None
        )
        if login_form is None:
            return None
        workflow = auth.build_login_workflow(page_state, login_form)
        if workflow is None:
            return None

        logger.info("Cleanup is re-authenticating to reach the records it must remove")
        before = page_state
        # Bounded by the plan itself: fill each field, then submit. A login that
        # needs more than its own steps is not one cleanup should keep pushing on.
        for _ in range(len(workflow.steps)):
            action = workflow.next_action()
            if action is None:
                break
            validation = validator.validate(
                action, page_state=page_state, actions_taken=0, pages_visited=0,
                screenshots_taken=0, runtime_seconds=self._runtime_seconds(),
            )
            if not validation.allowed:
                logger.info("Cleanup re-authentication refused by policy: %s", validation.reason)
                return None
            result = await executor.execute(
                action=validation.sanitized_action or action, page=page,
                page_state=page_state, capture_evidence=False,
            )
            if not result.success:
                return None
            workflow.advance()
            page_state = await observe()

        evaluation = auth.evaluate_after_submit(before, page_state, method="login")
        if not evaluation.authenticated:
            logger.info("Cleanup re-authentication did not restore the session")
            return None
        self.memory.sync_auth_public()
        logger.info("Cleanup re-authenticated successfully")
        return page_state

    def _cleanup_blocked_by_lost_session(self) -> bool:
        """Is cleanup standing in front of a sign-in screen?

        Cleanup runs as an epilogue, often long after the last action, and a
        navigation back to the record's list can land unauthenticated — the
        collection is then empty for a reason that has nothing to do with the
        record. Asking the Adaptive Application Understanding Engine rather than
        guessing from element counts: it already classifies the application state
        from structural evidence.
        """
        assessment = self._last_understanding
        if assessment is None:
            return False
        return assessment.state.primary_state == "authentication"

    def _observation_cannot_prove_absence(self) -> bool:
        """Would concluding "the record is not here" from the CURRENT observation
        be concluding from a page that never rendered?

        Asks the Adaptive Application Understanding Engine rather than guessing
        from element counts — it already classifies the application state from
        structural evidence, and it already said `frontend_failure` on the
        observation that made a live cleanup pass give up.
        """
        assessment = self._last_understanding
        if assessment is None:
            return False
        return assessment.state.primary_state in UNTRUSTWORTHY_ABSENCE_STATES

    @staticmethod
    def _same_cleanup_location(left: str | None, right: str | None) -> bool:
        def _key(url: str | None) -> str:
            return (url or "").split("#", 1)[0].rstrip("/").lower()

        return bool(left) and _key(left) == _key(right)

    async def _open_list_to_verify_cleanup(
        self,
        entry: Any,
        *,
        executor: Any,
        page: Any,
        observe: Callable[..., Awaitable[PageState]],
        validator: Any,
        page_state: PageState,
    ) -> PageState:
        """After a detail-page delete, open the list so absence can be confirmed."""
        action = BrowserAction(
            action=ActionType.OPEN_URL,
            url=entry.list_url,
            reason=f"Confirm GemmaQA temporary record '{entry.generated_identity}' is gone from the list",
            expected_result="The record's list page loads without that row.",
            risk=RiskLevel.LOW,
            category=ActionCategory.NAVIGATION_TEST,
            metadata={
                "cleanup_temporary_record_id": entry.temporary_record_id,
                "cleanup_step": "verify_absence",
                "risk_class": "authorized_record_cleanup",
            },
        )
        validation = validator.validate(
            action,
            page_state=page_state,
            actions_taken=0,
            pages_visited=0,
            screenshots_taken=0,
            runtime_seconds=self._runtime_seconds(),
        )
        if not validation.allowed:
            return page_state
        await self.emit(
            "action_planned",
            {
                "action": action.action.value,
                "reason": action.reason,
                "element_id": action.element_id,
                "category": action.category.value if action.category else None,
                **live_action_fields(action, page_state),
            },
        )
        result = await executor.execute(
            action=validation.sanitized_action or action,
            page=page,
            page_state=page_state,
            capture_evidence=True,
        )
        if not result.success:
            return page_state
        return await observe()

    def _advance_cleanup_from_live_action(self, action: BrowserAction, after_state: PageState) -> None:
        """Advance the Temporary Record Registry after a cleanup click in the live loop.

        Native `window.confirm` is accepted on the click itself (see
        `cleanup_click_accepts_native_dialog`). In-DOM confirm buttons still
        use the `confirm_delete` step. Staying on the record page after a
        successful-looking click means the record was not removed.
        """
        registry = self.memory.temporary_record_registry
        if registry is None:
            return
        meta = action.metadata or {}
        record_id = meta.get("cleanup_temporary_record_id")
        step = meta.get("cleanup_step")
        if not record_id or not step:
            return
        entry = registry.entries.get(record_id)
        if entry is None:
            return
        dialogs = list(after_state.modals or after_state.dialogs or [])
        if step == "delete_control":
            if self._record_still_visible_after_delete(entry, after_state):
                registry.mark_cleanup_failed(
                    record_id,
                    error="Delete click did not remove the record from the page",
                )
                return
            if entry.current_state in {"verified", "updated", "cleanup_failed"}:
                registry.request_cleanup(
                    record_id,
                    plan=CleanupPlan(delete_control_element_id=action.element_id),
                )
            entry = registry.entries.get(record_id)
            if entry is not None and entry.current_state == "cleanup_requested" and not dialogs:
                registry.mark_delete_action_validated(record_id)
                registry.mark_deleted(record_id)
            self._confirm_absence_if_on_list(registry, record_id, after_state)
        elif step == "confirm_delete":
            if self._record_still_visible_after_delete(entry, after_state):
                registry.mark_cleanup_failed(
                    record_id,
                    error="Confirm-delete click did not remove the record from the page",
                )
                return
            if entry.current_state == "cleanup_requested":
                registry.mark_delete_action_validated(record_id)
            registry.mark_deleted(record_id)
            self._confirm_absence_if_on_list(registry, record_id, after_state)

    @staticmethod
    def _record_still_visible_after_delete(entry: Any, after_state: PageState) -> bool:
        """True when the delete click left the record's own page still showing.

        Contact List's Delete Contact navigates to the list. Staying on
        /contactDetails after a 'successful' click means the record was not
        removed — run 51b62f6c marked it deleted anyway and skipped a real edit.
        """
        url = (after_state.url or "").lower()
        if any(part in url for part in ("contactdetails", "editcontact", "recorddetails")):
            return True
        identity = (getattr(entry, "generated_identity", None) or "").strip().lower()
        if identity and len(identity) >= 4:
            haystack = " ".join(
                filter(
                    None,
                    [
                        after_state.visible_text_summary,
                        after_state.title,
                        " ".join(after_state.headings or []),
                    ],
                )
            ).lower()
            if identity in haystack:
                return True
        return False

    def _confirm_absence_if_on_list(self, registry: Any, record_id: str, after_state: PageState) -> None:
        """When Delete already returned to the list and the row is gone, count it.

        Contact List's empty list has no table. The epilogue required a
        collection to verify absence, so run b6509a34 stayed `deleted` /
        crud_delete_executed 0, then logged out and tried to sign in again.
        """
        entry = registry.entries.get(record_id)
        if entry is None or entry.current_state != "deleted":
            return
        if self._record_still_visible_after_delete(entry, after_state):
            return
        list_url = getattr(entry, "list_url", None) or ""
        if list_url and not self._same_cleanup_location(after_state.url, list_url):
            return
        registry.mark_absence_verified(record_id)

    def _cleanup_holds_the_turn(self) -> bool:
        """Skip investigation screenshots while cleanup still owns the session."""
        registry = getattr(self.memory, "temporary_record_registry", None)
        if registry is None:
            return False
        try:
            pending = registry.records_pending_cleanup()
            # After edit, investigation used to take screenshots and FINISH
            # instead of Delete Contact (run 55588f75). Hold the turn once the
            # record is ready to remove.
            if self.policy.allow_destructive_actions and any(
                e.current_state in {"updated", "cleanup_failed"} for e in pending
            ):
                return True
            if pending:
                return False
            return any(
                e.current_state in {"deleted", "absence_verified", "manual_cleanup_required"}
                for e in registry.entries.values()
            )
        except Exception:
            return False

    async def _run_cleanup_pass(
        self, *, executor: Any, page: Any, observe: Callable[..., Awaitable[PageState]], evidence: Any,
        adapter: Any,
    ) -> None:
        """Best-effort, bounded cleanup of records the Temporary Record
        Registry has marked pending — see app.agent.temporary_record_registry
        and app.agent.cleanup_planner. Runs once, at the end of `run()`,
        against whatever page is CURRENTLY open: it does not navigate to find
        a record's list page, so a record whose delete control isn't visible
        on the current page is simply left pending with cleanup instructions
        in the final report (a documented limitation, not a bug).

        Every delete/confirm action still goes through `self.validator` and
        `executor` -- the exact same gates every other action in this run
        goes through. `ActionValidator`'s own `destructive_actions_disabled`
        flag (true unless the run explicitly opts into
        `allow_destructive_actions`) means nothing here is ever destructive
        unless the run was already configured to allow it; this method adds
        no new authority.
        """
        registry = self.memory.temporary_record_registry
        if registry is None:
            return
        pending = registry.records_pending_cleanup()
        if not pending:
            return
        if not (self.policy.allow_destructive_actions and self.policy.allow_safe_test_data_creation):
            logger.info(
                "%d temporary record(s) pending cleanup but allow_destructive_actions=%s / "
                "allow_safe_test_data_creation=%s -- leaving pending for manual/next-run cleanup",
                len(pending), self.policy.allow_destructive_actions, self.policy.allow_safe_test_data_creation,
            )
            if not self.policy.allow_destructive_actions:
                self.memory.note_write_candidate_suppressed(
                    required_flag="allow_destructive_actions",
                    capability="removing the test records GemmaQA created",
                    evidence=f"{len(pending)} record(s) left on the application",
                )
            return

        page_state = await observe()
        await self.emit(
            "action_planning",
            {"budget": self.memory.remaining_action_budget, "cleanup": True},
        )

        # Cleanup is an obligatory epilogue, not more exploration — and a run
        # normally ends BECAUSE the action budget ran out, which made the
        # validator reject every cleanup action for "Action budget exhausted".
        # Delete was therefore never performed on a normally-terminating run.
        #
        # Rather than bypassing the gate (it also enforces destructive-action
        # policy, URL scope, and risk level, all of which must still apply), the
        # same validator runs against a policy whose budget is the cleanup
        # allowance. Everything else about the gate is unchanged.
        cleanup_policy = replace(
            self.policy,
            max_actions=CLEANUP_MAX_STEPS_PER_RECORD * max(1, len(pending)),
            max_screenshots=self.policy.max_screenshots + len(pending),
        )
        cleanup_validator = ActionValidator(cleanup_policy)
        cleanup_actions_taken = 0

        logger.info(
            "Cleanup pass starting for %d pending temporary record(s) from %s",
            len(pending), page_state.url,
        )

        for entry in pending:
            if not registry.eligible_for_cleanup(entry.temporary_record_id, current_run_id=self.run_id):
                logger.info(
                    "Temporary record %s (state=%s) is not eligible for cleanup in this run",
                    entry.temporary_record_id, entry.current_state,
                )
                continue
            try:
                attempts = 0
                navigations = 0
                reobservations = 0
                # One re-authentication per record: if signing back in does not
                # restore access, trying again cannot.
                reauth_attempted = False
                # `attempts` counts ACTIONS, not looks — re-observation passes
                # below must not consume the step budget, or waiting for a slow
                # page would spend the allowance needed to delete through it.
                while attempts < CLEANUP_MAX_STEPS_PER_RECORD:
                    if (
                        self._observation_cannot_prove_absence()
                        and reobservations < CLEANUP_MAX_REOBSERVATIONS_PER_RECORD
                    ):
                        # The application has not finished presenting itself, so
                        # "the record isn't here" would be read off a page that was
                        # never rendered. A live run navigated back to the list,
                        # observed 2 elements mid-boot, and abandoned the record.
                        #
                        # Look again rather than wait a fixed interval — the
                        # evidence decides when to stop, as everywhere else.
                        reobservations += 1
                        logger.info(
                            "Cleanup observation of %s is not a basis for concluding absence (%s); observing again",
                            page_state.url,
                            self._last_understanding.state.primary_state if self._last_understanding else "unknown",
                        )
                        # Space the samples out with the engine's own geometric
                        # backoff — the same one `_observe` uses. Three
                        # back-to-back observations all completed inside one
                        # second and gave a freshly-loaded application no chance
                        # to fetch its own data.
                        try:
                            await adapter.wait(reobservation_interval_ms(reobservations))
                        except Exception:
                            pass
                        page_state = await observe()
                        continue
                    action = plan_cleanup_action(entry, canonical_model=self.memory.canonical_page_model)
                    if action is None and navigations < CLEANUP_MAX_NAVIGATIONS_PER_RECORD:
                        # Nothing on THIS page can delete the record. Walk back to
                        # it — return to where it was last seen, then open its row
                        # — instead of giving up, which is what left delete
                        # discovered-but-never-performed on every run.
                        action = plan_cleanup_navigation(
                            entry,
                            canonical_model=self.memory.canonical_page_model,
                            current_url=page_state.url,
                        )
                        if action is not None:
                            navigations += 1
                    if action is None and reobservations < CLEANUP_MAX_REOBSERVATIONS_PER_RECORD:
                        # Nothing actionable here YET. Cleanup has usually just
                        # arrived, and a control that renders a moment later is
                        # not a control that does not exist — a live pass opened
                        # the record's own page and read it before its Delete
                        # button was there. Same bounded budget as above.
                        reobservations += 1
                        try:
                            await adapter.wait(reobservation_interval_ms(reobservations))
                        except Exception:
                            pass
                        page_state = await observe()
                        continue
                    if action is None:
                        if entry.current_state == "cleanup_requested":
                            registry.mark_delete_action_validated(entry.temporary_record_id)
                            registry.mark_deleted(entry.temporary_record_id)
                        elif self._cleanup_blocked_by_lost_session() and not reauth_attempted:
                            # Sign back in and try again — ONCE. A second failure
                            # means the credentials no longer work, and retrying
                            # would just spend the epilogue on a locked door.
                            reauth_attempted = True
                            restored = await self._reauthenticate_for_cleanup(
                                page_state=page_state, executor=executor, page=page,
                                validator=cleanup_validator, observe=observe,
                            )
                            if restored is not None:
                                page_state = restored
                                # The record was never unreachable; the session was.
                                # Give the walk-back its budget again.
                                navigations = 0
                                continue
                            registry.mark_cleanup_failed(
                                entry.temporary_record_id,
                                error=(
                                    "The session had expired and could not be restored, so the "
                                    "record could not be reached. Remove it manually."
                                ),
                            )
                        elif self._cleanup_blocked_by_lost_session():
                            # The application is asking to sign in again. Cleanup
                            # cannot re-authenticate (see task #161), but it must
                            # not stay silent about WHY: a record reported as
                            # merely `pending` reads as "nobody tried", when in
                            # fact GemmaQA reached the door and found it locked.
                            # `cleanup_failed` carries the reason into the report
                            # and the manual-cleanup instructions.
                            registry.mark_cleanup_failed(
                                entry.temporary_record_id,
                                error=(
                                    "The session was no longer valid when cleanup ran, so the "
                                    "record could not be reached. Remove it manually, or re-run "
                                    "cleanup with a live session."
                                ),
                            )
                            logger.warning(
                                "Cleanup for temporary record %s stopped at an authentication "
                                "barrier (%s); the record was NOT removed",
                                entry.temporary_record_id, page_state.url,
                            )
                        else:
                            # Report what cleanup could actually see. Without this,
                            # a record left pending is indistinguishable from one
                            # nobody tried to clean up.
                            model = self.memory.canonical_page_model
                            rows = [
                                {
                                    "element_id": row.element_id,
                                    "activatable": row.is_activatable,
                                    "basis": row.activation_basis,
                                    "cells": [str(c)[:60] for c in (row.cell_values or [])],
                                }
                                for c in (list(model.collections) if model is not None else [])
                                for row in (c.visible_rows or [])
                            ]
                            logger.info(
                                "No cleanup step available for temporary record %s "
                                "(state=%s, identity=%r, list_url=%r, current_url=%r, navigations=%d); "
                                "rows on page: %s",
                                entry.temporary_record_id, entry.current_state,
                                entry.generated_identity, entry.list_url, page_state.url, navigations,
                                rows[:6],
                            )
                        break
                    attempts += 1
                    step = (action.metadata or {}).get("cleanup_step")
                    if step == "delete_control" and entry.current_state in {
                        "verified", "updated", "cleanup_failed",
                    }:
                        # Only the actual delete control requests cleanup. A
                        # navigation step must not advance the lifecycle, or the
                        # entry would be recorded as cleanup_requested against a
                        # row/URL rather than a delete control.
                        registry.request_cleanup(
                            entry.temporary_record_id,
                            plan=CleanupPlan(delete_control_element_id=action.element_id),
                        )
                    validation = cleanup_validator.validate(
                        action,
                        page_state=page_state,
                        actions_taken=cleanup_actions_taken,
                        pages_visited=0,
                        screenshots_taken=evidence.screenshot_count(),
                        runtime_seconds=self._runtime_seconds(),
                    )
                    cleanup_actions_taken += 1
                    logger.info(
                        "Cleanup step '%s' for temporary record %s: %s el=%s allowed=%s (%s)",
                        step, entry.temporary_record_id, action.action.value, action.element_id,
                        validation.allowed, validation.reason,
                    )
                    await self.emit(
                        "action_planned",
                        {
                            "action": action.action.value,
                            "reason": action.reason,
                            "element_id": action.element_id,
                            "category": action.category.value if action.category else None,
                            **live_action_fields(action, page_state),
                        },
                    )
                    if not validation.allowed:
                        registry.mark_cleanup_failed(entry.temporary_record_id, error=validation.reason)
                        break
                    await self.emit(
                        "action_started",
                        {
                            "action": action.action.value,
                            "element_id": action.element_id,
                            "url": page_state.url,
                            **live_action_fields(action, page_state),
                        },
                    )
                    result = await executor.execute(
                        action=validation.sanitized_action or action,
                        page=page,
                        page_state=page_state,
                        capture_evidence=True,
                    )
                    if not result.success:
                        registry.mark_cleanup_failed(
                            entry.temporary_record_id, error=result.error or result.message or "cleanup action failed",
                        )
                        break
                    await self.emit(
                        "action_finished",
                        {
                            "action": action.action.value,
                            "success": result.success,
                            "message": sanitize_text(result.message),
                            "url": result.after_url,
                            "evidence_ids": result.evidence_ids,
                        },
                    )
                    page_state = await observe()
                    self._advance_cleanup_from_live_action(action, page_state)

                if entry.current_state == "deleted":
                    # Absence needs a collection that actually LISTS records. A
                    # detail page has no collection at all, so the old check
                    # passed vacuously there and reported "absence verified" for
                    # a record nobody had looked for. When there is nothing to
                    # look in, the record stays `deleted` — attempted, not
                    # confirmed — which is what the report should say.
                    model = self.memory.canonical_page_model
                    collections = list(model.collections) if model is not None else []
                    if (
                        not collections
                        and entry.list_url
                        and not self._same_cleanup_location(page_state.url, entry.list_url)
                    ):
                        page_state = await self._open_list_to_verify_cleanup(
                            entry,
                            executor=executor,
                            page=page,
                            observe=observe,
                            validator=cleanup_validator,
                            page_state=page_state,
                        )
                        model = self.memory.canonical_page_model
                        collections = list(model.collections) if model is not None else []
                    if not collections:
                        logger.info(
                            "Temporary record %s was deleted but its absence could not be confirmed "
                            "(no record collection observable at %s)",
                            entry.temporary_record_id, page_state.url,
                        )
                    elif not any(
                        identity_matches_cells(entry.generated_identity, row.cell_values)
                        for c in collections
                        for row in (c.visible_rows or [])
                    ):
                        registry.mark_absence_verified(entry.temporary_record_id)
            except Exception as exc:
                logger.warning(
                    "Cleanup pass failed for temporary record %s (%s); leaving pending", entry.temporary_record_id, exc,
                )
                registry.mark_cleanup_failed(entry.temporary_record_id, error=str(exc))

        manual = registry.manual_cleanup_report()
        if manual:
            logger.warning("%d temporary record(s) require manual cleanup: %s", len(manual), manual)

    def _detect_progress(
        self,
        *,
        before_state: PageState,
        after_state: PageState,
        result: ActionResult,
        before_modals: list[str],
    ) -> dict[str, Any]:
        reasons: list[str] = []
        if result.before_url and result.after_url and result.before_url != result.after_url:
            reasons.append("url_changed")
        if (after_state.state_fingerprint or "") != (before_state.state_fingerprint or ""):
            if after_state.state_fingerprint not in (
                self.memory.page_fingerprints - {after_state.state_fingerprint}
            ):
                reasons.append("new_fingerprint")
            else:
                reasons.append("fingerprint_changed")
        after_modals = after_state.modals or after_state.dialogs
        if set(after_modals) - set(before_modals):
            reasons.append("new_modal")
        if set(after_state.tabs) - set(before_state.tabs):
            reasons.append("new_tab")
        if len(after_state.forms) > len(before_state.forms):
            reasons.append("new_form")
        if len(after_state.tables) > len(before_state.tables):
            reasons.append("new_table")
        if after_state.alerts and after_state.alerts != before_state.alerts:
            reasons.append("validation_or_alert")
        if result.new_console_errors or result.new_network_errors:
            reasons.append("new_bug_evidence")

        # Deduplicate fingerprint_changed vs new — simplify
        changed = bool(reasons)
        return {"changed": changed, "reasons": reasons, "new_bug": "new_bug_evidence" in reasons}

    async def _phase(self, phase: AgentPhase, message: str) -> None:
        self.sm.transition(phase)
        if self.is_pause_requested() and not self._cancel.is_set():
            await self._update_run(
                message=f"Pause requested — finishing current step ({message})"
            )
            await self.emit(
                "state_changed",
                {"state": phase.value, "message": message, "pause_requested": True},
            )
            return
        await self._update_run(status=self.sm.status, message=message)
        await self.emit("state_changed", {"state": phase.value, "message": message})

    async def _update_run(self, **fields: Any) -> None:
        async with self.db_factory() as session:
            result = await session.execute(select(QARun).where(QARun.id == self.run_id))
            run = result.scalar_one_or_none()
            if not run:
                return
            if "status" in fields and fields["status"] is not None:
                status = fields["status"]
                run.status = status.value if hasattr(status, "value") else str(status)
            for key in (
                "message",
                "current_url",
                "pages_visited",
                "actions_taken",
                "bugs_found",
                "progress_pct",
                "error",
                "report_json",
                "application_json",
            ):
                if key in fields and fields[key] is not None:
                    setattr(run, key, fields[key])
            if fields.get("finished"):
                run.finished_at = _utc_now()
            if run.started_at is None and run.status not in {"created", "pending"}:
                run.started_at = _utc_now()
            await session.commit()

    async def _persist_page(self, page_state: PageState) -> None:
        """Upsert by (run_id, canonical_url) — never insert duplicate page rows."""
        from datetime import datetime

        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from app.application.url_normalize import normalize_url

        canonical = normalize_url(page_state.url, base_url=self.request.url) or page_state.url
        page_type = (
            page_state.classification.page_type
            if page_state.classification
            else "unknown"
        )
        explored = False
        visit_count = 1
        page_id = page_state.page_id
        if self.memory.app_store:
            app_page = self.memory.app_store.model.page_by_url(canonical)
            if app_page:
                page_id = app_page.id
                visit_count = app_page.visit_count
                explored = app_page.exploration_status.value == "explored"
                page_type = app_page.page_type or page_type

        status = "explored" if explored else "discovered"
        now = datetime.utcnow()

        async with self.db_factory() as session:
            result = await session.execute(
                select(PageRecord).where(
                    PageRecord.run_id == self.run_id,
                    PageRecord.url == canonical,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.title = page_state.title or existing.title
                existing.page_type = page_type or existing.page_type
                existing.state_json = page_state.model_dump_json()
                if page_state.screenshot_path:
                    existing.screenshot_path = page_state.screenshot_path
                if hasattr(existing, "visit_count"):
                    existing.visit_count = visit_count
                if hasattr(existing, "exploration_status"):
                    existing.exploration_status = status
                if hasattr(existing, "last_seen_at"):
                    existing.last_seen_at = now
                await session.commit()
                return

            row = PageRecord(
                id=page_id,
                run_id=self.run_id,
                url=canonical,
                title=page_state.title or "",
                page_type=page_type,
                state_json=page_state.model_dump_json(),
                screenshot_path=page_state.screenshot_path,
            )
            if hasattr(row, "visit_count"):
                row.visit_count = visit_count
            if hasattr(row, "exploration_status"):
                row.exploration_status = status
            if hasattr(row, "last_seen_at"):
                row.last_seen_at = now
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                result = await session.execute(
                    select(PageRecord).where(
                        PageRecord.run_id == self.run_id,
                        PageRecord.url == canonical,
                    )
                )
                existing = result.scalar_one_or_none()
                if not existing:
                    raise
                existing.title = page_state.title or existing.title
                existing.page_type = page_type or existing.page_type
                existing.state_json = page_state.model_dump_json()
                if page_state.screenshot_path:
                    existing.screenshot_path = page_state.screenshot_path
                if hasattr(existing, "visit_count"):
                    existing.visit_count = visit_count
                if hasattr(existing, "exploration_status"):
                    existing.exploration_status = status
                if hasattr(existing, "last_seen_at"):
                    existing.last_seen_at = now
                await session.commit()

    async def _persist_action(self, result: ActionResult) -> None:
        async with self.db_factory() as session:
            session.add(
                ActionRecord(
                    id=result.action_id,
                    run_id=self.run_id,
                    action_type=result.action.action.value,
                    payload_json=result.model_dump_json(),
                    success=result.success,
                    message=result.message,
                    error=result.error,
                    duration_ms=result.duration_ms,
                )
            )
            await session.commit()

    async def _persist_bug(self, bug: Any) -> None:
        async with self.db_factory() as session:
            session.add(
                BugRecord(
                    id=bug.bug_id,
                    run_id=self.run_id,
                    title=bug.title,
                    description=bug.description,
                    severity=bug.severity.value,
                    status=bug.status.value,
                    payload_json=bug.model_dump_json(),
                )
            )
            await session.commit()


ACTIVE_RUNS: dict[str, AgentController] = {}


async def create_and_start_run(
    request: CreateRunRequest,
    session: AsyncSession,
    on_event: EventCallback | None = None,
    gemma: GemmaProvider | None = None,
) -> str:
    """Persist a new run and start it via the process-local RunManager.

    ``on_event`` / ``gemma`` are honored for scripted demos that inject a mock provider.
    """
    from app.agent.run_manager import run_manager
    from app.database import AsyncSessionLocal

    req = request.model_copy(update={"auto_start": False})
    run_id = await run_manager.create_run(req, session)
    prepared = run_manager._prepared.get(run_id)
    if prepared is None:
        raise RuntimeError("Failed to prepare run")

    if gemma is None and on_event is None:
        await run_manager.start_run(run_id, session)
        return run_id

    # Custom gemma/on_event path (tests / demos)
    async def _wrapped_on_event(event_type: str, data: dict[str, Any]) -> None:
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
        if not isinstance(payload, dict):
            payload = {"value": payload}
        event = await __import__("app.agent.events", fromlist=["event_store"]).event_store.append(
            run_id, event_type, payload
        )
        from app.api.websocket import ws_manager

        await ws_manager.publish(run_id, event)
        if on_event:
            await on_event(event_type, data)

    async with run_manager._lock_for(run_id):
        if run_id in run_manager._tasks and not run_manager._tasks[run_id].done():
            raise RuntimeError("Run already started")
        controller = AgentController(
            run_id=run_id,
            request=prepared.request,
            db_factory=AsyncSessionLocal,
            on_event=_wrapped_on_event,
            gemma=gemma,
        )
        ACTIVE_RUNS[run_id] = controller
        run_row = (await session.execute(select(QARun).where(QARun.id == run_id))).scalar_one()
        run_row.status = RunStatusEnum.INITIALIZING.value
        run_row.started_at = _utc_now()
        await session.commit()

        async def _runner() -> None:
            try:
                await controller.run()
            finally:
                ACTIVE_RUNS.pop(run_id, None)
                run_manager._prepared.pop(run_id, None)
                run_manager._tasks.pop(run_id, None)

        run_manager._tasks[run_id] = asyncio.create_task(_runner())
    return run_id

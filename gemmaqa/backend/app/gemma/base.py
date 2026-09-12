"""Gemma provider abstraction — mock, OpenAI-compatible, or transformers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.gemma.context_sanitizer import sanitize_page_state_for_model
from app.gemma.evidence_retrieval import (
    retrieve_for_targets,
    retrieve_technical_evidence,
    retrieve_transition_evidence,
)
from app.gemma.health import ProviderHealth, get_active_health, set_active_health
from app.gemma.model_context import ContextProvenance, EvidenceRegistry, build_model_context
from app.gemma.page_projection import (
    PAGE_SOURCE_LEGACY,
    control_element_ids,
    project_page,
)
from app.gemma.parser import (
    ActionParseError,
    bug_analysis_to_defect,
    fallback_safe_action,
    parse_and_validate_action,
    parse_bug_analysis,
    parse_classification,
    parse_scenarios,
)
from app.gemma.prompts import (
    ACTION_SYSTEM_PROMPT,
    ALLOWED_ACTIONS,
    BUG_SYSTEM_PROMPT,
    CANDIDATE_RANKING_SYSTEM_PROMPT,
    FINAL_REPORT_SYSTEM_PROMPT,
    FORM_TEST_SYSTEM_PROMPT,
    GOAL_RANKING_SYSTEM_PROMPT,
    PAGE_CLASSIFY_SYSTEM_PROMPT,
    PRODUCT_DOMAIN_SYSTEM_PROMPT,
    WORKFLOW_SYSTEM_PROMPT,
    build_action_prompt,
    build_bug_prompt,
    build_candidate_ranking_prompt,
    build_classify_prompt,
    build_correction_prompt,
    build_final_report_prompt,
    build_form_test_prompt,
    build_goal_ranking_prompt,
    build_product_domain_prompt,
    build_workflow_prompt,
    render_action_prompt,
)
from app.intelligence.knowledge_graph.graph_context_projector import project_model_context
from app.schemas import (
    ActionCategory,
    ActionType,
    BrowserAction,
    BugAnalysisResult,
    Defect,
    PageClassification,
    PageState,
    RiskLevel,
)
from app.utils.logging import get_logger
from app.utils.sanitization import sanitize_dict

logger = get_logger("gemma.base")

# How many page controls get per-element evidence retrieved for one call. The
# evidence registry is what the model is allowed to cite, so it must cover the
# controls actually in play — but retrieving for every control on a 500-element
# page would defeat the point of bounding context at all.
MAX_EVIDENCE_TARGETS = 12


@dataclass
class ActionGenerationRequest:
    """Inputs for next-action selection."""

    page_state: PageState
    memory: dict[str, Any] = field(default_factory=dict)
    recent_actions: list[dict[str, Any]] = field(default_factory=list)
    visited_states: list[str] = field(default_factory=list)
    unexplored: list[str] = field(default_factory=list)
    testing_objective: str = "Explore safely and discover defects"
    remaining_action_budget: int = 20
    remaining_page_budget: int = 10
    safe_mode: bool = True
    allowed_actions: list[str] = field(default_factory=lambda: list(ALLOWED_ACTIONS))
    authorized_domain: str = ""
    screenshot_path: str | None = None
    # Deterministic candidates from the unified frontier (FrontierBuilder), as plain
    # dicts (FrontierCandidate.to_dict()). When present, any deterministic fallback the
    # provider uses must select from these rather than re-deriving its own candidate
    # set from raw page elements — there is exactly one candidate-generation system.
    frontier_candidates: list[dict[str, Any]] = field(default_factory=list)

    # -- typed aggregation boundary (see app/gemma/model_context.py) ----------
    # Additive and all-optional, so every existing caller keeps working. These
    # are what turn this DTO from "arguments for one prompt" into the single
    # aggregation boundary the audit's section G said was missing.
    run_id: str = ""
    mode: str = "exploration"
    # Distinguishes "the operator asked for this" from "nobody set one". An
    # invented generic objective is indistinguishable, to the model, from a real
    # instruction — so absence is reported as absence, never papered over.
    testing_objective_provided: bool = False
    # Universal Page Perception output. When present the model finally sees
    # canonical perception rather than the legacy PageState (audit finding J-4).
    canonical_page_model: Any = field(default=None, repr=False)
    # The live RunMemory, read through its existing query API so engine findings
    # (not just engine statistics) can reach the prompt. Optional: absent for
    # older callers and unit tests, which simply get a thinner context.
    run_memory: Any = field(default=None, repr=False)


@dataclass
class GenerationRequest:
    """Everything one model call needs, produced by the shared normalized path.

    Every provider consumes this same object. Transport formatting may differ
    between an HTTP endpoint and a local pipeline; the CONTEXT may not. That
    equivalence is what `prepare_generation_request` exists to guarantee, and is
    the direct answer to audit question 20.
    """

    system: str
    user: str
    images: list[str] | None = None
    temperature: float = 0.0
    known_element_ids: set[str] = field(default_factory=set)
    evidence: Any = None
    projection: dict[str, Any] = field(default_factory=dict)
    reduction: Any = None

    def context_metadata(self) -> dict[str, Any]:
        """Safe, secret-free description of what was actually sent."""
        metadata = dict((self.projection.get("projection_metadata") or {}))
        if self.reduction is not None:
            metadata.update(self.reduction.to_dict())
        metadata["prompt_chars"] = len(self.user)
        metadata["images_attached"] = len(self.images or [])
        return metadata


class GemmaProvider(ABC):
    """Abstract Gemma provider with retry / fallback for action generation."""

    name: str = "base"
    health: ProviderHealth | None = None
    max_provider_failures: int = 3
    # The most recent normalized request, kept so the controller and report
    # builder can describe how context was built WITHOUT re-running the
    # pipeline or reaching into prompt text. Read-only observability.
    last_generation_request: "GenerationRequest | None" = None
    # Increments once per prepared request. Lets an observer tell "this
    # iteration prepared a model call" from "this iteration was planned
    # deterministically and the previous request is simply still lying around".
    generation_sequence: int = 0
    # Whether THIS provider can make the general semantic decisions
    # Autonomous Investigation's scenario execution needs for an arbitrary
    # target application (never a synthetic-fixture-only claim — see
    # app.gemma.mock_provider.MockGemmaProvider, the one override). True by
    # default for every real, model-backed provider. Purely declarative —
    # `app.runtime_info.provider_capability_mode` is what the report/API
    # actually reads; this attribute exists so code holding a live provider
    # instance (rather than just a provider_type string) has a direct,
    # non-string-matching way to ask the same question.
    supports_scenario_execution: bool = True
    # Whether `health_check()` can establish REACHABILITY, or only configuration.
    # True for every provider with a free liveness endpoint (an OpenAI-compatible
    # `/models`, a local pipeline). False when the only probe available is a
    # billable inference request — Bedrock Runtime has no free alternative — in
    # which case `health_check()` reports configuration validity ONLY, and
    # `reachable` must stay None until a real call proves it either way.
    #
    # Declarative and defaulted so existing providers are unaffected: without it
    # `/api/health/gemma` reads a True from `health_check()` as evidence of
    # reachability, and reports a provider as reachable that has never once been
    # called.
    supports_liveness_probe: bool = True

    @abstractmethod
    async def _generate(
        self,
        system: str,
        user: str,
        *,
        images: list[str] | None = None,
        temperature: float | None = None,
    ) -> str:
        """Return raw model text. Must not log secrets."""

    def _health(self) -> ProviderHealth:
        if self.health is None:
            self.health = ProviderHealth(provider_type=self.name, configured=True)
            set_active_health(self.health)
        return self.health

    def provider_exhausted(self) -> bool:
        h = self._health()
        return h.consecutive_failures >= max(1, self.max_provider_failures)

    def _record_ai_failure(self, exc: BaseException, *, context: str) -> None:
        summary = f"{context}: {type(exc).__name__}: {exc}"[:300]
        self._health().record_failure(summary)
        logger.error("AI failure recorded (%s) consecutive=%s", context, self._health().consecutive_failures)

    def _record_ai_success(self) -> None:
        self._health().record_success()

    def prepare_generation_request(self, request: ActionGenerationRequest) -> GenerationRequest:
        """The shared normalized path: aggregate → project → retrieve → render → reduce.

        Every provider — mock, OpenAI-compatible, transformers — goes through
        this exact method, which is what makes their contexts equivalent rather
        than merely similar. Audit question 20 recorded that the default mock
        provider bypassed all of it; nothing bypasses it now.

        Deliberately synchronous and side-effect-free: it only reads already-
        computed state. That makes it callable from a test, from a report
        builder, or from a provider, without a browser or an event loop.
        """
        settings = get_settings()

        # 1. Aggregate — normalize engine outputs into one typed context.
        context = build_model_context(request, provider_name=self.name)

        # 2. Project the page, preferring canonical perception over legacy.
        context.page = project_page(
            page_state=request.page_state,
            canonical_model=request.canonical_page_model,
        )
        context.page_source = str(context.page.get("source") or PAGE_SOURCE_LEGACY)

        # 3. Retrieve bounded, citable evidence for the controls actually in play.
        observation_version = str(context.page.get("observation_version") or "")
        page_ids = control_element_ids(context.page)
        try:
            retrieve_for_targets(
                registry=context.evidence,
                target_element_ids=sorted(page_ids)[:MAX_EVIDENCE_TARGETS],
                canonical_model=request.canonical_page_model,
                page_state=request.page_state,
                observation_version=observation_version,
            )
            context.technical_evidence = retrieve_technical_evidence(
                registry=context.evidence,
                page_state=request.page_state,
                canonical_model=request.canonical_page_model,
                screenshot_path=request.screenshot_path or "",
                observation_version=observation_version,
            )
            retrieve_transition_evidence(registry=context.evidence, run_memory=request.run_memory)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Evidence retrieval failed (%s); continuing without it", type(exc).__name__)

        # 4. Bounded selection, relevance ranking, and deduplication.
        projection = project_model_context(context)

        # 5. Render through the centralized templates, reducing by importance.
        known_ids = self._known_element_ids(request) | page_ids
        user_prompt, reduction = render_action_prompt(
            projection,
            known_element_ids=sorted(known_ids),
            max_prompt_chars=settings.max_prompt_chars,
        )
        projection["projection_metadata"].update(reduction.to_dict())

        images: list[str] | None = None
        if request.screenshot_path and settings.effective_gemma_supports_images:
            images = [request.screenshot_path]

        self.generation_sequence += 1
        return GenerationRequest(
            system=ACTION_SYSTEM_PROMPT,
            user=user_prompt,
            images=images,
            # Deterministic action selection — never sampled.
            temperature=0.0,
            known_element_ids=known_ids,
            evidence=context.evidence,
            projection=projection,
            reduction=reduction,
        )

    @staticmethod
    def _known_element_ids(request: ActionGenerationRequest) -> set[str]:
        """Every id from the legacy PageState the model may legitimately name.

        Kept exactly as it was before the context upgrade so that a run with no
        canonical perception validates identically to how it always has.
        """
        known_ids = {el.element_id for el in request.page_state.interactive_elements}
        for form in request.page_state.forms:
            known_ids.add(form.form_id)
            for field_item in form.fields:
                if field_item.element_id:
                    known_ids.add(field_item.element_id)
        for table in request.page_state.tables:
            known_ids.add(table.table_id)
        return {i for i in known_ids if i}

    async def generate_action(self, request: ActionGenerationRequest) -> BrowserAction:
        """Return exactly one validated safe BrowserAction (deterministic decoding)."""
        settings = get_settings()
        self.max_provider_failures = int(settings.gemma_max_consecutive_failures)

        if self.provider_exhausted():
            logger.error("Provider exhausted after repeated failures — stopping exploration safely")
            return BrowserAction(
                action=ActionType.FINISH,
                reason=(
                    "AI provider failed repeatedly; stopping further model-driven actions. "
                    f"Last error: {self._health().last_error or 'unknown'}"
                ),
                expected_result="Run ends safely without further AI calls",
                risk=RiskLevel.LOW,
                category=ActionCategory.COMPLETION,
                metadata={"ai_failure": True, "provider_exhausted": True},
            )

        prepared = self.prepare_generation_request(request)
        known_ids = prepared.known_element_ids
        user_prompt = prepared.user
        images = prepared.images
        action_temperature = prepared.temperature
        self.last_generation_request = prepared

        raw = ""
        try:
            raw = await self._generate(
                ACTION_SYSTEM_PROMPT,
                user_prompt,
                images=images,
                temperature=action_temperature,
            )
            action = parse_and_validate_action(
                raw,
                known_element_ids=known_ids,
                allowed_actions=request.allowed_actions,
                reject_high_risk=True,
                evidence_registry=prepared.evidence,
            )
            self._record_ai_success()
            return action
        except Exception as first_exc:
            self._record_ai_failure(first_exc, context="action_generate")
            logger.warning(
                "Action generation failed (%s); retrying once with correction prompt",
                type(first_exc).__name__,
            )
            try:
                correction = build_correction_prompt(str(first_exc), raw)
                raw2 = await self._generate(
                    ACTION_SYSTEM_PROMPT,
                    correction,
                    images=None,
                    temperature=action_temperature,
                )
                action = parse_and_validate_action(
                    raw2,
                    known_element_ids=known_ids,
                    allowed_actions=request.allowed_actions,
                    reject_high_risk=True,
                    evidence_registry=prepared.evidence,
                )
                self._record_ai_success()
                return action
            except Exception as second_exc:
                self._record_ai_failure(second_exc, context="action_retry")
                logger.error(
                    "Action generation retry failed (%s); using safe deterministic fallback",
                    type(second_exc).__name__,
                )
                if self.provider_exhausted():
                    return BrowserAction(
                        action=ActionType.FINISH,
                        reason=(
                            "AI provider failed repeatedly; stopping. "
                            f"Last error: {self._health().last_error}"
                        ),
                        expected_result="Run ends safely",
                        risk=RiskLevel.LOW,
                        category=ActionCategory.COMPLETION,
                        metadata={"ai_failure": True, "provider_exhausted": True},
                    )
                visited: list[str] = []
                mem = request.memory or {}
                for key in ("visited_urls", "pages_visited"):
                    raw = mem.get(key)
                    if isinstance(raw, (list, set, tuple)):
                        visited.extend(str(u) for u in raw if u)
                blocked_hrefs_raw = mem.get("blocked_hrefs")
                blocked_hrefs = (
                    set(str(h) for h in blocked_hrefs_raw if h)
                    if isinstance(blocked_hrefs_raw, (list, set, tuple))
                    else None
                )
                action = fallback_safe_action(
                    page_state=request.page_state,
                    recent_actions=request.recent_actions,
                    remaining_action_budget=request.remaining_action_budget,
                    reason=f"Safe fallback after AI failure: {type(second_exc).__name__}",
                    unexplored_urls=list(request.unexplored or []),
                    visited_urls=visited,
                    blocked_hrefs=blocked_hrefs,
                    frontier_candidates=list(request.frontier_candidates or []),
                )
                action.metadata = {
                    **(action.metadata or {}),
                    "ai_failure": True,
                    "fallback": True,
                }
                return action

    async def analyze_page(self, page_state: PageState) -> PageClassification:
        safe = sanitize_page_state_for_model(page_state)
        try:
            raw = await self._generate(
                PAGE_CLASSIFY_SYSTEM_PROMPT,
                build_classify_prompt(safe),
                temperature=0.0,
            )
            result = parse_classification(raw)
            self._record_ai_success()
            return result
        except Exception as exc:
            self._record_ai_failure(exc, context="analyze_page")
            raise

    async def generate_test_scenarios(
        self,
        page_state: PageState,
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        safe = sanitize_page_state_for_model(page_state)
        try:
            raw = await self._generate(
                FORM_TEST_SYSTEM_PROMPT,
                build_form_test_prompt(safe, sanitize_dict(context or {})),
                temperature=0.0,
            )
            scenarios = parse_scenarios(raw)
            self._record_ai_success()
            return scenarios
        except Exception as exc:
            self._record_ai_failure(exc, context="generate_test_scenarios")
            logger.warning("Scenario generation failed: %s", type(exc).__name__)
            return []

    async def analyze_potential_bug(
        self,
        observation: dict[str, Any],
        context: dict[str, Any] | None = None,
        *,
        evidence_registry: Any = None,
    ) -> BugAnalysisResult:
        safe_obs = sanitize_dict(observation)
        safe_ctx = sanitize_dict(context or {})
        # A defect claim is the one place a model reference gets PERSISTED, so
        # it is the one place an unvalidated reference does lasting damage.
        # Build the registry from the very observation the model is shown, so
        # "cite only what you were given" is enforceable rather than advisory.
        registry = evidence_registry
        if registry is None:
            registry = EvidenceRegistry()
            # Alerts and dialogs are shown to the model in the observation but
            # were not registerable, so a finding ABOUT an alert had nothing it
            # was allowed to cite. Live on Nova Pro, the one genuine finding in a
            # 32-action run ("default credentials displayed in an alert") came
            # back with `evidence_ids: []` for exactly this reason — the evidence
            # was on screen and in the prompt, just not in the registry.
            for kind, key in (
                ("console_error", "console_errors"),
                ("network_failure", "network_failures"),
                ("ui_alert", "alerts"),
                ("ui_dialog", "dialogs"),
            ):
                for entry in list(safe_obs.get(key) or [])[:10]:
                    registry.register(
                        kind=kind,
                        summary=str(entry)[:240],
                        provenance=ContextProvenance(producer="PageObserver", source_ref=key),
                    )
        try:
            raw = await self._generate(
                BUG_SYSTEM_PROMPT,
                build_bug_prompt(safe_obs, safe_ctx, evidence_registry=registry),
                temperature=0.0,
            )
            result = parse_bug_analysis(raw, evidence_registry=registry)
            self._record_ai_success()
            return result
        except Exception as exc:
            self._record_ai_failure(exc, context="analyze_potential_bug")
            raise

    async def generate_final_report(self, run_data: dict[str, Any]) -> dict[str, Any]:
        safe = sanitize_dict(run_data)
        try:
            raw = await self._generate(
                FINAL_REPORT_SYSTEM_PROMPT,
                build_final_report_prompt(safe),
                temperature=0.0,
            )
            from app.gemma.parser import extract_json

            try:
                result = extract_json(raw)
            except ActionParseError:
                result = {"summary": raw[:2000], "raw_text": True}
            self._record_ai_success()
            return result
        except Exception as exc:
            self._record_ai_failure(exc, context="generate_final_report")
            raise

    async def analyze_product_domain(self, run_data: dict[str, Any]) -> dict[str, Any]:
        raw = await self._generate(
            PRODUCT_DOMAIN_SYSTEM_PROMPT,
            build_product_domain_prompt(sanitize_dict(run_data)),
            temperature=0.0,
        )
        from app.gemma.parser import extract_json

        try:
            return extract_json(raw)
        except ActionParseError:
            return {"summary": raw[:1000]}

    async def rank_goals(
        self,
        goals: list[dict[str, Any]],
        *,
        gaps: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[str]:
        """Structured, high-level goal prioritization only — never selectors, urls,
        credentials, or executable commands; the response schema has no field for
        any of those. Returns an ordered list of goal_id, validated to be a subset
        of the input ids (any invented/unknown id is dropped, never trusted).
        Failures are surfaced by returning [] — the caller (Planner) falls back to
        its existing deterministic priority order, never an unrelated strategy."""
        if not goals:
            return []
        known_ids = {str(g.get("goal_id")) for g in goals if g.get("goal_id")}
        try:
            raw = await self._generate(
                GOAL_RANKING_SYSTEM_PROMPT,
                build_goal_ranking_prompt(sanitize_dict({"goals": goals})["goals"], gaps or [], context),
                temperature=0.0,
            )
        except Exception as exc:
            logger.warning("rank_goals provider call failed (%s); no ranking applied", type(exc).__name__)
            return []
        from app.gemma.parser import extract_json

        try:
            data = extract_json(raw)
        except ActionParseError as exc:
            logger.warning("rank_goals response unparseable (%s); no ranking applied", exc)
            return []
        ranked_raw = data.get("ranked_goal_ids")
        if not isinstance(ranked_raw, list):
            return []
        ranked: list[str] = []
        seen: set[str] = set()
        for item in ranked_raw:
            gid = str(item)
            if gid in known_ids and gid not in seen:
                ranked.append(gid)
                seen.add(gid)
        return ranked

    async def rank_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[str]:
        """Advisory-only semantic tie-break among a SMALL, already near-tied
        set of deterministically-scored candidates
        (app.agent.priority_engine.PriorityDecision.near_tied_top) — never the
        full frontier, never able to introduce or elevate a candidate outside
        the given set. Returns [] on any failure (never raises) — the caller
        (Planner._apply_gemma_tie_break) simply keeps the deterministic score
        order when this returns nothing usable."""
        if not candidates or len(candidates) < 2:
            return []
        known_ids = {str(c.get("candidate_id")) for c in candidates if c.get("candidate_id")}
        try:
            raw = await self._generate(
                CANDIDATE_RANKING_SYSTEM_PROMPT,
                build_candidate_ranking_prompt(sanitize_dict({"candidates": candidates})["candidates"], context),
                temperature=0.0,
            )
        except Exception as exc:
            logger.warning("rank_candidates provider call failed (%s); no advisory ranking applied", type(exc).__name__)
            return []
        from app.gemma.parser import extract_json

        try:
            data = extract_json(raw)
        except ActionParseError as exc:
            logger.warning("rank_candidates response unparseable (%s); no advisory ranking applied", exc)
            return []
        ranked_raw = data.get("ranked_candidate_ids")
        if not isinstance(ranked_raw, list):
            return []
        ranked: list[str] = []
        seen: set[str] = set()
        for item in ranked_raw:
            cid = str(item)
            if cid in known_ids and cid not in seen:
                ranked.append(cid)
                seen.add(cid)
        return ranked

    async def extract_workflows(self, run_data: dict[str, Any]) -> list[dict[str, Any]]:
        raw = await self._generate(
            WORKFLOW_SYSTEM_PROMPT,
            build_workflow_prompt(sanitize_dict(run_data)),
            temperature=0.0,
        )
        from app.gemma.parser import extract_json

        try:
            data = extract_json(raw)
            return list(data.get("workflows") or data.get("items") or [])
        except ActionParseError:
            return []

    async def analyze_visual_elements(
        self,
        *,
        targets: list[dict[str, Any]],
        dom_evidence: dict[str, Any],
        accessibility_evidence: dict[str, Any],
        nearby_text: dict[str, Any],
        trigger_reasons: list[str],
        images: list[str],
        known_element_ids: set[str],
    ) -> list[Any]:
        """Bounded, structured-JSON-only visual analysis for a specific,
        policy-selected set of element IDs (see
        app.perception.visual_policy.VisualObservationPolicy — this is never
        called for every element on every page). Returns validated
        `VisualEvidence` records; any element_id the model invents outside
        `known_element_ids` is dropped by the parser, never trusted. Any
        provider failure degrades to an empty list — visual evidence is
        always additive and never blocks the rest of perception."""
        from app.gemma.parser import parse_visual_analysis
        from app.gemma.prompts import VISUAL_ANALYSIS_SYSTEM_PROMPT, build_visual_analysis_prompt

        user_prompt = build_visual_analysis_prompt(
            targets=targets,
            dom_evidence=dom_evidence,
            accessibility_evidence=accessibility_evidence,
            nearby_text=nearby_text,
            trigger_reasons=trigger_reasons,
        )
        try:
            raw = await self._generate(
                VISUAL_ANALYSIS_SYSTEM_PROMPT,
                user_prompt,
                images=images or None,
                temperature=0.0,
            )
        except Exception as exc:
            logger.warning(
                "Visual analysis provider call failed (%s); no visual evidence added", type(exc).__name__
            )
            return []
        return parse_visual_analysis(raw, known_element_ids=known_element_ids)

    async def health_check(self) -> bool:
        return True

    def public_health(self) -> dict[str, Any]:
        h = get_active_health() or self._health()
        return h.to_public_dict()

    # ------------------------------------------------------------------
    # Compatibility wrappers for existing agent modules
    # ------------------------------------------------------------------

    async def decide_action(
        self,
        page_state: PageState,
        previous_actions: list[dict[str, Any]],
        unexplored: list[str],
        context: dict[str, Any] | None = None,
    ) -> BrowserAction:
        ctx = context or {}
        request = ActionGenerationRequest(
            page_state=page_state,
            memory=ctx.get("memory") or {},
            recent_actions=previous_actions,
            visited_states=list(ctx.get("visited_states") or []),
            unexplored=unexplored,
            testing_objective=str(ctx.get("testing_objective") or "Explore safely and discover defects"),
            remaining_action_budget=int(
                ctx.get("remaining_action_budget")
                or max(0, int(ctx.get("max_actions", 50)) - int(ctx.get("actions_taken", 0)))
            ),
            remaining_page_budget=int(ctx.get("remaining_page_budget") or ctx.get("max_pages") or 10),
            safe_mode=bool(ctx.get("safe_mode", True)),
            authorized_domain=str(ctx.get("authorized_domain") or ""),
            screenshot_path=ctx.get("screenshot_path") or page_state.screenshot_path,
        )
        return await self.generate_action(request)

    async def generate_structured_decision(
        self,
        request: ActionGenerationRequest,
    ) -> BrowserAction:
        """Preferred name for constrained JSON QA decisions."""
        return await self.generate_action(request)

    async def classify_page(self, page_state: PageState) -> PageClassification:
        return await self.analyze_page(page_state)

    async def infer_module(self, page_state: PageState) -> PageClassification:
        return await self.analyze_page(page_state)

    async def analyse_result(
        self,
        observation: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> BugAnalysisResult:
        return await self.analyze_potential_bug(observation, context)

    async def summarise_application(self, run_data: dict[str, Any]) -> dict[str, Any]:
        return await self.analyze_product_domain(run_data)

    async def generate_report_content(self, run_data: dict[str, Any]) -> dict[str, Any]:
        return await self.generate_final_report(run_data)

    async def propose_tests(
        self,
        page_state: PageState,
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await self.generate_test_scenarios(page_state, context)

    async def analyze_bug(
        self,
        observation: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Defect | None:
        analysis = await self.analyze_potential_bug(observation, context)
        run_id = str((context or {}).get("run_id") or "")
        return bug_analysis_to_defect(analysis, run_id, page_url=observation.get("url"))

    async def generate_documentation(self, section: str, run_data: dict[str, Any]) -> str:
        report = await self.generate_final_report({**run_data, "section": section})
        if section and section in report:
            return f"## {section}\n\n{report[section]}"
        summary = report.get("summary") or report.get("product_overview") or ""
        return f"## {section}\n\n{summary}"

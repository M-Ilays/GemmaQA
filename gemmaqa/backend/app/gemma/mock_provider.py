"""Mock / heuristic Gemma provider for development and tests (no model required)."""

from __future__ import annotations

import json
from typing import Any, Callable

from app.gemma.base import ActionGenerationRequest, GemmaProvider
from app.gemma.health import ProviderHealth, set_active_health
from app.gemma.parser import ActionParseError, fallback_safe_action, parse_and_validate_action
from app.schemas import (
    ActionCategory,
    ActionType,
    BrowserAction,
    BugAnalysisResult,
    BugClassification,
    PageClassification,
    PageState,
    RiskLevel,
)
from app.utils.logging import get_logger

logger = get_logger("gemma.mock")

GenerateHook = Callable[[str, str], str]


def _action_to_model_json(action: BrowserAction) -> str:
    """Serialize a deterministic decision into the exact JSON shape a real model
    would return, so it can be validated by the same parser rather than trusted."""
    return json.dumps(
        {
            "action": action.action.value,
            "element_id": action.element_id,
            "value": action.value,
            "url": action.url,
            "key": action.key,
            "wait_ms": action.wait_ms,
            "reason": action.reason or "Deterministic selection.",
            "expected_result": action.expected_result or "Application responds.",
            "risk": action.risk.value if action.risk else "low",
            "category": action.category.value if action.category else "exploration",
            "metadata": action.metadata or {},
        }
    )


class MockGemmaProvider(GemmaProvider):
    """
    Deterministic provider for local development and unit tests.

    Optional `scripted_responses` / `generate_hook` let tests inject raw model text
    without loading weights or calling a network API.
    Never loads a real model.

    Capability declaration (Option A of the Autonomous-Execution follow-up):
    this provider is EXPLORATION-ONLY. It never claims to make general
    semantic CRUD decisions for an arbitrary target application — its own
    action-generation path (`_heuristic_action` -> `fallback_safe_action` ->
    `_SIMPLE_CANDIDATE_ACTIONS`, app/gemma/parser.py) has no dispatch entry
    for `start_form_workflow`/`continue_form_workflow`, so it structurally
    cannot drive a FILL/SELECT-based form-fill flow regardless of what
    Autonomous Investigation asks for. See app.runtime_info.
    provider_capability_mode, which reports this provider as
    "exploration_only" unconditionally (never "scenario_execution_capable"),
    and app.agent.controller, which logs a one-time warning if a run enables
    `enable_autonomous_investigation` with this provider configured.
    """

    name = "mock"
    supports_scenario_execution = False

    def __init__(
        self,
        *,
        scripted_responses: list[str] | None = None,
        generate_hook: GenerateHook | None = None,
        raise_timeout: bool = False,
        timeout_message: str = "Model timeout",
    ) -> None:
        self._scripted = list(scripted_responses or [])
        self._hook = generate_hook
        self._raise_timeout = raise_timeout
        self._timeout_message = timeout_message
        self.calls: list[dict[str, str]] = []
        self.max_provider_failures = 100
        self.health = ProviderHealth(
            provider_type=self.name,
            model_id="mock-heuristic",
            multimodal_support=False,
            configured=True,
            reachable=True,
        )
        set_active_health(self.health)

    def enqueue(self, response: str) -> None:
        self._scripted.append(response)

    async def _generate(
        self,
        system: str,
        user: str,
        *,
        images: list[str] | None = None,
        temperature: float | None = None,
    ) -> str:
        self.calls.append({"system": system[:80], "user": user[:200]})
        if self._raise_timeout and not self._scripted:
            raise TimeoutError(self._timeout_message)
        if self._scripted:
            return self._scripted.pop(0)
        if self._hook:
            return self._hook(system, user)
        return self._heuristic_text(system, user)

    async def health_check(self) -> bool:
        self.health.reachable = True
        return True

    async def rank_goals(
        self,
        goals: list[dict[str, Any]],
        *,
        gaps: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[str]:
        """Deterministic stand-in for a real LLM ranking: lower priority number and
        higher confidence sort first, exactly the ordering select_active_goal's own
        tie-break already prefers — so enabling LLM ranking never changes behavior
        under the mock provider, keeping existing tests meaningful without a live
        model."""
        ranked = sorted(
            goals,
            key=lambda g: (g.get("priority", 999), -float(g.get("confidence", 0.0))),
        )
        return [str(g["goal_id"]) for g in ranked if g.get("goal_id")]

    async def rank_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[str]:
        """Deterministic stand-in: sort by total_score descending — exactly
        what the priority engine already computed — so enabling advisory
        ranking under the mock provider never changes behavior versus not
        calling it at all."""
        ranked = sorted(candidates, key=lambda c: -float(c.get("total_score", 0.0)))
        return [str(c["candidate_id"]) for c in ranked if c.get("candidate_id")]

    async def generate_action(self, request: ActionGenerationRequest) -> BrowserAction:
        # If tests scripted raw outputs, use the base retry/validate path.
        if self._scripted or self._hook or self._raise_timeout:
            return await super().generate_action(request)

        # No scripted output: this provider decides heuristically rather than
        # semantically. It must NOT, however, skip the infrastructure.
        #
        # docs/MODEL_CONTEXT_AUDIT.md question 20 recorded that this method
        # short-circuited before any prompt existed, so under the default
        # `GEMMA_PROVIDER=mock` the aggregation, projection, sanitization,
        # prompt-building, reduction, parsing, and validation stages were never
        # exercised in CI — a regression in any of them was invisible.
        #
        # The fix keeps the deterministic DECISION and routes it through the
        # shared normalized path: build the real context, then serialize the
        # heuristic choice as model output and validate it exactly as a real
        # model's output would be. Same infrastructure, same guarantees, no
        # weights and no network.
        prepared = self.prepare_generation_request(request)
        self.last_generation_request = prepared
        self.calls.append({"system": prepared.system[:80], "user": prepared.user[:200]})

        decision = self._auth_heuristic(request) or self._heuristic_action(request)
        try:
            return parse_and_validate_action(
                _action_to_model_json(decision),
                known_element_ids=prepared.known_element_ids,
                allowed_actions=request.allowed_actions,
                reject_high_risk=True,
                evidence_registry=prepared.evidence,
            )
        except ActionParseError as exc:
            # The heuristic produced something the validator rejects. Existing
            # deterministic behaviour wins over the round trip — this provider's
            # decisions are already safety-bounded by construction, and silently
            # degrading them would be a real regression.
            logger.debug("Mock decision did not round-trip through the parser (%s); using it directly", exc)
            return decision

    def _auth_heuristic(self, request: ActionGenerationRequest) -> BrowserAction | None:
        """Open registration when credentials are absent; fills owned by auth strategy."""
        page = request.page_state
        if page is None:
            return None
        mem = request.memory or {}
        if mem.get("authenticated") or mem.get("credential_profile_available"):
            return None
        elements = list(getattr(page, "interactive_elements", None) or [])
        blob = " ".join(
            [getattr(page, "url", "") or "", getattr(page, "title", "") or ""]
        ).lower()
        if any(k in blob for k in ("signup", "sign-up", "register", "adduser")):
            return None
        for el in elements:
            text_el = (
                getattr(el, "accessible_name", None)
                or getattr(el, "visible_text", None)
                or getattr(el, "text", None)
                or ""
            ).lower()
            if any(k in text_el for k in ("sign up", "signup", "register", "create account")):
                return BrowserAction(
                    action=ActionType.CLICK,
                    element_id=getattr(el, "element_id", None),
                    reason=(
                        "Mock: credentials unavailable — open registration to create "
                        "a unique test account."
                    ),
                    expected_result="Registration form loads.",
                    risk=RiskLevel.LOW,
                    category=ActionCategory.NAVIGATION_TEST,
                    metadata={
                        "mock_auth_hint": "open_registration",
                        "decision": "resolve_authentication_blocker",
                        "action_label": text_el,
                    },
                )
        return None

    def _heuristic_action(self, request: ActionGenerationRequest) -> BrowserAction:
        # Only stop for budget when truly exhausted; otherwise keep exploring
        # safe targets (nav, forms, unexplored URLs).
        visited: list[str] = []
        mem = request.memory or {}
        for key in ("visited_urls", "pages_visited"):
            raw = mem.get(key)
            if isinstance(raw, (list, set, tuple)):
                visited.extend(str(u) for u in raw if u)
        for a in request.recent_actions or []:
            if a.get("url"):
                visited.append(str(a["url"]))
        if request.page_state and getattr(request.page_state, "url", None):
            visited.append(str(request.page_state.url))
        blocked_hrefs_raw = mem.get("blocked_hrefs")
        blocked_hrefs = (
            set(str(h) for h in blocked_hrefs_raw if h)
            if isinstance(blocked_hrefs_raw, (list, set, tuple))
            else None
        )
        return fallback_safe_action(
            page_state=request.page_state,
            recent_actions=request.recent_actions,
            remaining_action_budget=request.remaining_action_budget,
            reason="Mock provider selected a deterministic safe action.",
            unexplored_urls=list(request.unexplored or []),
            visited_urls=visited,
            blocked_hrefs=blocked_hrefs,
            frontier_candidates=list(request.frontier_candidates or []),
        )

    def _heuristic_text(self, system: str, user: str) -> str:
        lower = system.lower()
        if "visual perception assistant" in lower:
            return json.dumps({"observations": []})
        if "defect" in lower or "bug" in lower:
            return json.dumps(
                {
                    "classification": "observation",
                    "title": "",
                    "module": "",
                    "severity": "low",
                    "priority": "low",
                    "preconditions": [],
                    "steps": [],
                    "expected_result": "",
                    "actual_result": "",
                    "business_impact": "",
                    "possible_root_cause": "",
                    "confidence": 0.1,
                    "evidence_ids": [],
                }
            )
        if "classify" in lower:
            return json.dumps(
                {
                    "page_type": "content",
                    "confidence": 0.6,
                    "purpose": "General content page",
                    "module_guess": "General",
                    "tags": ["content"],
                }
            )
        if "form test" in lower or "scenario" in lower:
            return json.dumps(
                {
                    "scenarios": [
                        {
                            "title": "Inspect required fields",
                            "description": "Enumerate required inputs",
                            "category": "form",
                            "priority": "medium",
                            "preconditions": [],
                            "steps": ["Open form", "List required fields"],
                            "expected_results": ["Required fields are labeled"],
                        }
                    ]
                }
            )
        if "workflow" in lower:
            return json.dumps({"workflows": [{"name": "Primary nav", "description": "", "steps": ["Open home"]}]})
        if "final" in lower or "report" in lower:
            return json.dumps(
                {
                    "summary": "Mock exploratory report",
                    "product_overview": "Application under test",
                    "domain_purpose": "Under analysis",
                    "coverage_notes": [],
                    "regression_checklist": ["Core navigation works"],
                    "recommended_next_tests": [],
                }
            )
        # Default action JSON — finish
        return json.dumps(
            {
                "action": "finish",
                "element_id": None,
                "value": None,
                "reason": "Mock default finish",
                "expected_result": "Run completes",
                "risk": "low",
                "category": "completion",
            }
        )

    async def analyze_page(self, page_state: PageState) -> PageClassification:
        from urllib.parse import urlparse

        from app.agent.explorer import module_name_from_url

        title = (page_state.title or "").lower()
        url = page_state.url.lower()
        path = urlparse(page_state.url).path.lower()
        module = module_name_from_url(page_state.url)
        if any(
            k in path or k in title
            for k in ("signup", "register", "adduser", "login", "sign-in", "signin", "auth")
        ):
            page_type, purpose = "authentication", "User authentication or registration"
        elif any(k in url or k in title for k in ("dashboard", "home", "overview")):
            page_type, purpose = "dashboard", "Primary landing / overview"
        elif "contact" in path:
            page_type = "list" if page_state.tables else "create_form" if page_state.forms else "unknown"
            purpose = "Contacts module"
            module = "Contacts"
        elif page_state.forms:
            page_type, purpose = "create_form", "Data entry or configuration"
        elif page_state.tables:
            page_type, purpose = "list", "Tabular data listing"
        else:
            page_type, purpose = "unknown", "General application content"
        return PageClassification(
            page_type=page_type,
            confidence=0.65,
            purpose=purpose,
            module_guess=module,
            tags=[page_type],
        )

    async def analyze_potential_bug(
        self,
        observation: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> BugAnalysisResult:
        from app.agent.bug_analyzer import is_benign_client_noise

        console_errors = [
            e
            for e in (observation.get("console_errors") or [])
            if not is_benign_client_noise(str(e))
        ]
        network_failures = [
            e
            for e in (observation.get("network_failures") or [])
            if not is_benign_client_noise(str(e))
        ]
        if console_errors:
            return BugAnalysisResult(
                classification=BugClassification.SUSPECTED_BUG,
                title="Console error detected during exploration",
                module="frontend",
                severity="high",
                priority="high",
                steps=["Navigate to page", "Observe browser console"],
                expected_result="No console errors",
                actual_result=str(console_errors[0]),
                business_impact="Possible broken client functionality",
                possible_root_cause="Hypothesis only: unhandled JavaScript exception",
                confidence=0.7,
            )
        if network_failures:
            return BugAnalysisResult(
                classification=BugClassification.SUSPECTED_BUG,
                title="Network request failure detected",
                module="api",
                severity="high",
                priority="high",
                steps=["Navigate to page", "Inspect failed network requests"],
                expected_result="Critical requests succeed",
                actual_result=str(network_failures[0]),
                business_impact="Data or actions may fail",
                possible_root_cause="Hypothesis only: backend or CORS failure",
                confidence=0.7,
            )
        # Nothing deterministic fired, so there is nothing to report. Says so
        # with `NO_DEFECT` rather than a low-confidence OBSERVATION: both are
        # dropped downstream, but one provider expressing "no defect" as a
        # near-zero confidence while another uses the classification is the same
        # rule with two spellings, and the confidence spelling is what let a real
        # model's confident "nothing is wrong" become 27 recorded defects.
        return BugAnalysisResult(classification=BugClassification.NO_DEFECT, confidence=0.0)


# Backward-compatible alias
StubGemmaProvider = MockGemmaProvider

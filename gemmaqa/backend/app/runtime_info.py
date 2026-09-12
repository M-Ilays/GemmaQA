"""Runtime transparency helpers (provider, browser adapter, run mode). No secrets."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.gemma.health import get_active_health


def provider_display_name(provider_type: str | None = None) -> str:
    settings = get_settings()
    name = (provider_type or settings.normalized_gemma_provider).lower()
    if name == "mock":
        return "Mock"
    if name == "openai_compatible":
        base = (settings.effective_gemma_api_base or "").lower()
        if "11434" in base or "ollama" in base:
            return "Gemma via Ollama"
        return "Gemma via OpenAI-compatible API"
    if name == "transformers":
        return "Gemma via Transformers"
    if name == "bedrock":
        # Not "Gemma via Bedrock": the configured BEDROCK_MODEL_ID is frequently
        # not a Gemma model at all (Claude, Nova, Llama), and a display name that
        # asserts otherwise would misreport every run.
        return "Amazon Bedrock"
    if name == "gemini":
        return "Google Gemini"
    return f"Gemma ({name})"


def run_mode_label(provider_type: str | None = None) -> str:
    name = (provider_type or get_settings().normalized_gemma_provider).lower()
    if name == "mock":
        return "Mock / deterministic"
    if name == "bedrock":
        # Bedrock is a managed cloud service — prompts leave the machine. Calling
        # that "Local AI" would tell an operator the opposite of the truth about
        # where the page content they are testing actually goes.
        return "Cloud AI (Amazon Bedrock)"
    if name == "gemini":
        return "Cloud AI (Google Gemini)"
    return "Local AI"


CAPABILITY_MODES = frozenset({"exploration_only", "scenario_execution_capable", "real_model_reasoning"})


def provider_capability_mode(
    provider_type: str | None = None, *, enable_autonomous_investigation: bool = False
) -> str:
    """Which of the three modes the CURRENT provider+config combination
    actually is — never inferred from whether `enable_autonomous_
    investigation` is merely set, since the Mock provider's own dispatch
    table (`app.gemma.parser._SIMPLE_CANDIDATE_ACTIONS`) structurally cannot
    drive a `start_form_workflow`/`continue_form_workflow` step (no FILL/
    SELECT ever comes out of it) — so Mock is ALWAYS "exploration_only",
    regardless of the flag. This is the Option-A decision from the CRUD-
    execution-gap follow-up: Mock never claims scenario-execution capability
    it cannot actually provide for a general (non-synthetic-fixture)
    target, per "do not make Mock pretend to be a general reasoning model."

    - "exploration_only": Mock provider. Scenario generation/queuing/
      eligibility classification still run (see `qa_strategy`/
      `scenario_planning`), but semantic-step resolution for anything
      beyond the fixed navigation/inspect/open_url dispatch table cannot
      meaningfully proceed.
    - "scenario_execution_capable": a real, configured provider AND
      `enable_autonomous_investigation=True` — the Autonomous Investigation
      Engine can drive scenarios to completion via genuine model reasoning.
    - "real_model_reasoning": a real, configured provider but autonomous
      investigation is not enabled this run — real semantic reasoning is
      available for the standard exploration loop, but scenario execution
      itself was not opted into.
    """
    name = (provider_type or get_settings().normalized_gemma_provider).lower()
    if name == "mock":
        return "exploration_only"
    return "scenario_execution_capable" if enable_autonomous_investigation else "real_model_reasoning"


def capability_mode_explanation(mode: str) -> str:
    return {
        "exploration_only": (
            "Mock provider: deterministic heuristic action selection only. It can explore, "
            "navigate, and inspect forms/tables, but cannot make the general semantic CRUD "
            "decisions autonomous scenario execution needs for an arbitrary target application. "
            "Scenarios are still generated and classified for eligibility; they will not be "
            "driven to completion under this provider."
        ),
        "scenario_execution_capable": (
            "A real, configured reasoning provider with autonomous investigation enabled — "
            "generated scenarios can be selected, validated, and executed through the runtime "
            "Planner/Safety Validator/BrowserAdapter pipeline."
        ),
        "real_model_reasoning": (
            "A real, configured reasoning provider, but autonomous investigation is not enabled "
            "this run — standard exploration uses real model reasoning; scenario execution was "
            "not opted into."
        ),
    }.get(mode, "")


def reported_model_id(provider_type: str | None = None) -> str:
    """Which model id to show for the configured provider. One implementation.

    `/api/health/gemma` and `build_runtime_info` each derived this separately
    before, and the two could disagree — the exact shape of duplication that has
    produced real bugs in this codebase. Both now call here.

    Empty string means "no model id applies" (mock), which callers render as they
    see fit; it is never a guess at a default model.
    """
    settings = get_settings()
    provider = (provider_type or settings.normalized_gemma_provider).lower()
    if provider == "mock":
        return ""
    # Bedrock keeps its own setting and deliberately never falls back to
    # GEMMA_MODEL_ID — see config.effective_bedrock_model_id for why.
    configured = (
        settings.effective_bedrock_model_id
        if provider == "bedrock"
        else settings.effective_gemma_model_id
    )
    health = get_active_health()
    # The health record reflects what the provider actually resolved, so it wins —
    # but ONLY when it describes this provider. `_ACTIVE_HEALTH` is process-wide
    # and set by whichever provider was constructed last, so an unmatched record
    # is stale and reported "mock-heuristic" for a Bedrock run before this check.
    if health and health.model_id and health.provider_type == provider:
        return health.model_id
    return configured


def browser_adapter_display(adapter: str | None = None) -> str:
    settings = get_settings()
    name = (adapter or settings.normalized_browser_adapter).lower()
    if name in {"direct_playwright", "direct", "playwright"}:
        return "Direct Playwright"
    if name in {"playwright_mcp", "mcp"}:
        return "Playwright MCP"
    return name


def build_runtime_info(
    *,
    decisions_validated: int = 0,
    decisions_rejected: int = 0,
    actions_executed: int = 0,
    stop_reason: str | None = None,
    adapter_connection_status: str | None = None,
    adapter_capabilities: dict[str, bool] | None = None,
    adapter_execution_failures: int = 0,
    unsupported_evidence_features: list[str] | None = None,
    browser_adapter_id: str | None = None,
    enable_autonomous_investigation: bool = False,
) -> dict[str, Any]:
    """Public runtime snapshot for reports and /api/config/runtime."""
    settings = get_settings()
    provider = settings.normalized_gemma_provider
    health = get_active_health()
    model_id = reported_model_id(provider)
    adapter_id = browser_adapter_id or settings.normalized_browser_adapter
    caps = adapter_capabilities or {}
    unsupported = unsupported_evidence_features or []
    capability_mode = provider_capability_mode(provider, enable_autonomous_investigation=enable_autonomous_investigation)

    return {
        "provider": provider_display_name(provider),
        "provider_type": provider,
        "model_id": model_id or None,
        "model": model_id or ("None" if provider == "mock" else None),
        "api_base_url": settings.effective_gemma_api_base or None
        if provider != "mock"
        else None,
        "browser_adapter": browser_adapter_display(adapter_id),
        "browser_adapter_id": adapter_id,
        "adapter_connection_status": adapter_connection_status or "n/a",
        "adapter_capabilities": caps,
        "adapter_execution_failures": adapter_execution_failures,
        "unsupported_evidence_features": unsupported,
        "screenshots_supported": caps.get("screenshots", True) if caps else True,
        "console_capture_supported": caps.get("console_events", True) if caps else True,
        "network_capture_supported": caps.get("network_events", True) if caps else True,
        "run_mode": run_mode_label(provider),
        "is_mock_provider": provider == "mock",
        "capability_mode": capability_mode,
        "capability_mode_explanation": capability_mode_explanation(capability_mode),
        "enable_autonomous_investigation": enable_autonomous_investigation,
        "provider_configured": bool(
            provider == "mock"
            or (settings.effective_gemma_api_base and settings.effective_gemma_model_id)
            or (provider == "transformers" and (settings.effective_gemma_model_id or settings.gemma_local_model_path))
        ),
        "provider_reachable": health.reachable if health else None,
        "provider_health_error": (health.last_error or health.config_error or None)
        if health
        else None,
        "decisions_validated": decisions_validated,
        "decisions_rejected": decisions_rejected,
        "actions_executed": actions_executed,
        "stop_reason": stop_reason,
        "temperature": settings.gemma_temperature,
        "max_output_tokens": settings.effective_gemma_max_tokens,
        "timeout_seconds": settings.gemma_timeout_seconds,
    }

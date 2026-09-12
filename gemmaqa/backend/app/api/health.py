"""Platform health and runtime diagnostics (no secrets)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.browser.adapters import create_browser_adapter
from app.config import get_settings
from app.database import engine
from app.gemma import get_gemma_provider
from app.gemma.catalog import ProviderSwitchError, list_provider_catalog, switch_active_provider
from app.gemma.health import connection_status, get_active_health
from app.runtime_info import build_runtime_info, reported_model_id
from app.utils.logging import get_logger

logger = get_logger("api.health")

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def api_health() -> dict[str, Any]:
    settings = get_settings()
    from app.strands_agent.service import strands_status

    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "provider_type": settings.normalized_gemma_provider,
        "browser_adapter": settings.normalized_browser_adapter,
        "strands": strands_status(),
    }


def _provider_auth_fields(settings: Any) -> dict[str, Any]:
    """Region and auth posture, described without revealing any credential.

    `auth_configured` is a bool and `auth_mode` names a mechanism — neither is
    derived from the token's value beyond "is one present". `None` rather than
    `False` for non-Bedrock providers, because "not applicable" and "no auth
    configured" are different claims and only one of them is true here.
    """
    if settings.normalized_gemma_provider != "bedrock":
        return {"region": None, "auth_configured": None, "auth_mode": None}
    configured = settings.bedrock_auth_configured
    return {
        "region": settings.aws_region or None,
        "auth_configured": configured,
        "auth_mode": "bearer_token" if configured else "aws_credential_chain",
    }


@router.get("/health/gemma")
async def health_gemma() -> dict[str, Any]:
    """Gemma provider connectivity. Never exposes API keys."""
    settings = get_settings()
    try:
        provider = get_gemma_provider()
    except RuntimeError as exc:
        # The factory refused to build a provider, so there is nothing to probe.
        # `reachable: False` is kept for backward compatibility even though
        # "never attempted" would be more precise — `connection_status` carries
        # the accurate answer for clients that want it.
        return {
            "status": "misconfigured",
            "provider_type": settings.normalized_gemma_provider,
            "configured": False,
            "reachable": False,
            "connection_status": "misconfigured",
            "model_id": reported_model_id() or None,
            "api_base_url": (
                settings.effective_gemma_api_base or None
                if settings.normalized_gemma_provider != "mock"
                else None
            ),
            **_provider_auth_fields(settings),
            "error": str(exc),
        }

    try:
        reachable = await provider.health_check()
    except Exception as exc:
        reachable = False
        logger.warning("Gemma health check failed: %s", type(exc).__name__)
        h = getattr(provider, "health", None) or get_active_health()
        if h is not None:
            h.reachable = False
            h.last_error = f"{type(exc).__name__}: health check failed"

    snapshot = provider.public_health() if hasattr(provider, "public_health") else {}
    active = get_active_health()
    if active is not None:
        snapshot = active.to_public_dict()

    configured = bool(snapshot.get("configured"))
    # `health_check()` returning True means "reachable" ONLY for providers that
    # can actually probe liveness for free. For one that cannot (Bedrock), a True
    # means "configuration is valid" and must not be promoted to evidence of
    # reachability — otherwise a provider that has never been called once is
    # reported as reachable.
    probe_proves_reachability = getattr(provider, "supports_liveness_probe", True)
    resolved_reachable = (
        snapshot.get("reachable")
        if snapshot.get("reachable") is not None
        else (reachable if probe_proves_reachability else None)
    )
    return {
        "status": "ok" if configured else "misconfigured",
        "provider_type": snapshot.get("provider_type") or settings.normalized_gemma_provider,
        "configured": configured,
        # Meaning preserved exactly: True only after a real successful call,
        # False only after a real failure, None when never exercised. For Bedrock
        # this stays None until a run makes an inference call — `health_check()`
        # deliberately does not spend a billable Converse request to find out.
        "reachable": resolved_reachable,
        # Additive: the same tri-state as a label a client cannot misread as
        # "broken". Derived by the one rule in app.gemma.health.
        "connection_status": connection_status(
            configured=configured, reachable=resolved_reachable
        ),
        "model_id": snapshot.get("model_identifier") or reported_model_id() or None,
        # Unchanged and NOT repurposed: Bedrock has no operator-set base URL, so
        # this stays null for it. The region is reported separately below.
        "api_base_url": (
            settings.effective_gemma_api_base or None
            if settings.normalized_gemma_provider != "mock"
            else None
        ),
        **_provider_auth_fields(settings),
        "multimodal_support": bool(snapshot.get("multimodal_support")),
        "last_error_summary": snapshot.get("last_error_summary")
        or snapshot.get("config_error"),
        "consecutive_failures": int(snapshot.get("consecutive_failures") or 0),
        "is_mock": settings.normalized_gemma_provider == "mock",
    }


@router.get("/health/browser")
async def health_browser() -> dict[str, Any]:
    """Browser adapter health (Direct Playwright or Playwright MCP)."""
    settings = get_settings()
    adapter_id = settings.normalized_browser_adapter
    result: dict[str, Any] = {
        "adapter": adapter_id,
        "configured_adapter": adapter_id,
        "playwright_headless": settings.playwright_headless,
        "last_error": None,
    }

    if adapter_id == "direct_playwright":
        from app.browser.adapters.types import AdapterCapabilities

        result["capabilities"] = AdapterCapabilities(tabs=True).to_dict()
        try:
            from playwright.async_api import async_playwright

            result["playwright_import"] = True
            pw = await async_playwright().start()
            try:
                from app.browser.manager import _chromium_launch_args

                browser = await pw.chromium.launch(
                    headless=True, args=_chromium_launch_args()
                )
                await browser.close()
                result["chromium_launch"] = True
                result["available"] = True
                result["session_available"] = True
                result["status"] = "ok"
            finally:
                await pw.stop()
        except Exception as exc:
            result["available"] = False
            result["session_available"] = False
            result["status"] = "unavailable"
            result["last_error"] = f"{type(exc).__name__}: {exc}"
            result["error"] = result["last_error"]
        return result

    adapter = create_browser_adapter("health-probe", adapter_name="playwright_mcp")
    try:
        info = await adapter.health_check()
        result.update(info)
        result["capabilities"] = info.get("capabilities") or adapter.capabilities().to_dict()
        result["session_available"] = bool(info.get("session_active") or info.get("available"))
        result["last_error"] = info.get("last_error") or info.get("error")
        # Never echo MCP command args (may contain secrets/paths)
        result.pop("command", None)
        result.pop("args", None)
        result["status"] = "ok" if info.get("available") else "unavailable"
    except Exception as exc:
        result["available"] = False
        result["session_available"] = False
        result["status"] = "unavailable"
        result["last_error"] = f"{type(exc).__name__}: {exc}"
        result["error"] = result["last_error"]
        result["capabilities"] = adapter.capabilities().to_dict()
    return result


@router.get("/config/runtime")
async def config_runtime() -> dict[str, Any]:
    """Safe runtime configuration snapshot for operators and reports."""
    settings = get_settings()
    info = build_runtime_info()
    evidence_ok = settings.evidence_dir.exists() and settings.evidence_dir.is_dir()
    db_ok = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        logger.debug("DB health probe failed: %s", type(exc).__name__)

    return {
        **info,
        "evidence_dir": str(settings.evidence_dir),
        "evidence_storage_available": evidence_ok,
        "database_healthy": db_ok,
        "max_prompt_chars": settings.max_prompt_chars,
        "gemma_max_consecutive_failures": settings.gemma_max_consecutive_failures,
    }


def _active_run_count() -> int:
    from app.agent.controller import ACTIVE_RUNS

    return len(ACTIVE_RUNS)


class ProviderSwitchBody(BaseModel):
    provider: str = Field(..., min_length=1)


@router.get("/config/providers")
async def config_providers() -> dict[str, Any]:
    """Implemented LLM catalog for the UI. Config checks only — no live probes."""
    return list_provider_catalog(active_runs=_active_run_count())


@router.post("/config/provider")
async def config_set_provider(body: ProviderSwitchBody) -> dict[str, Any]:
    """Switch the active provider for the next New Run. In-flight runs keep theirs."""
    active_runs = _active_run_count()
    try:
        result = switch_active_provider(body.provider, active_runs=active_runs)
    except ProviderSwitchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if active_runs:
        result["warning"] = (
            f"{active_runs} run(s) already in progress will keep their current model. "
            "The new provider applies to the next New Run."
        )
    return result

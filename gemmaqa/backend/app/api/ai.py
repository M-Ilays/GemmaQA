"""AI / Gemma provider health endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.gemma import get_gemma_provider
from app.gemma.health import get_active_health
from app.schemas import AIHealthResponse
from app.utils.logging import get_logger

logger = get_logger("api.ai")

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/health", response_model=AIHealthResponse)
async def ai_health() -> AIHealthResponse:
    """
    Health of the configured Gemma provider.

    Never exposes API keys. Does not load transformers weights.
    """
    settings = get_settings()
    provider = get_gemma_provider()
    try:
        reachable = await provider.health_check()
    except Exception as exc:
        logger.warning("AI health check error: %s", type(exc).__name__)
        reachable = False
        h = getattr(provider, "health", None) or get_active_health()
        if h is not None:
            h.reachable = False
            h.last_error = f"{type(exc).__name__}: health check failed"

    snapshot = provider.public_health() if hasattr(provider, "public_health") else {}
    active = get_active_health()
    if active is not None:
        snapshot = active.to_public_dict()

    return AIHealthResponse(
        provider_type=snapshot.get("provider_type") or settings.normalized_gemma_provider,
        configured=bool(snapshot.get("configured")),
        reachable=snapshot.get("reachable") if snapshot.get("reachable") is not None else reachable,
        model_identifier=snapshot.get("model_identifier"),
        multimodal_support=bool(snapshot.get("multimodal_support")),
        last_error_summary=snapshot.get("last_error_summary") or snapshot.get("config_error"),
        consecutive_failures=int(snapshot.get("consecutive_failures") or 0),
    )

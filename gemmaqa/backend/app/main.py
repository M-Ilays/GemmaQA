"""GemmaQA FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import ai, health, reports, runs, websocket
from app.api.errors import register_exception_handlers
from app.config import get_settings
from app.frontend_static import frontend_dist_dir, mount_frontend
from app.database import init_db
from app.runtime_info import browser_adapter_display, provider_display_name
from app.schemas import HealthResponse
from app.utils.logging import get_logger, setup_logging

setup_logging()
logger = get_logger("main")
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    provider = settings.normalized_gemma_provider
    if provider == "gemini":
        model_log = settings.effective_gemini_model_id or "gemini-3.5-flash"
        base_log = "(google-genai)"
    else:
        model_log = settings.effective_gemma_model_id or "(none)"
        base_log = settings.effective_gemma_api_base or "(n/a)" if provider != "mock" else "(mock)"
    logger.info(
        "Gemma provider=%s display=%s model=%s base=%s (models never loaded at startup)",
        provider,
        provider_display_name(provider),
        model_log,
        base_log,
    )
    logger.info(
        "Browser adapter=%s (%s)",
        settings.normalized_browser_adapter,
        browser_adapter_display(),
    )
    if provider != "mock":
        if provider == "openai_compatible" and (
            not settings.effective_gemma_api_base or not settings.effective_gemma_model_id
        ):
            logger.error(
                "GEMMA_PROVIDER=%s is misconfigured — set GEMMA_API_BASE_URL and "
                "GEMMA_MODEL_ID. Runs will fail until fixed (no silent mock fallback).",
                provider,
            )
    await init_db()
    yield
    logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Autonomous Exploratory Testing Agent powered by Gemma + Playwright",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(runs.router)
app.include_router(reports.router)
app.include_router(websocket.router)
app.include_router(ai.router)
app.include_router(health.router)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", app=settings.app_name, version=settings.app_version)


if frontend_dist_dir(settings) is not None:
    # Production image: SPA at / (same origin as /api and /ws).
    mount_frontend(app, settings)
else:

    @app.get("/")
    async def root() -> dict[str, str]:
        """JSON service info when no SPA build is present (local API-only)."""
        return {
            "app": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "health": "/health",
        }

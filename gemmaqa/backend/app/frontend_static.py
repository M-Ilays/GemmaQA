"""Serve the Vite production build from the same FastAPI origin (Cloud Run).

Local development keeps using Vite on 5173 with the proxy. This module is a
no-op unless SERVE_FRONTEND is enabled (production image) and dist exists.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.utils.logging import get_logger

logger = get_logger("frontend.static")

_RESERVED_PREFIXES = (
    "api/",
    "ws/",
    "health",
    "docs",
    "redoc",
    "openapi.json",
)


def _serve_frontend_enabled() -> bool:
    return os.getenv("SERVE_FRONTEND", "").strip().lower() in {"1", "true", "yes"}


def frontend_dist_dir(settings: Settings) -> Path | None:
    """Return the Vite dist directory when production serving is enabled."""
    if not _serve_frontend_enabled():
        return None
    dist = settings.project_root / "frontend" / "dist"
    if (dist / "index.html").is_file():
        return dist
    return None


def mount_frontend(app: FastAPI, settings: Settings) -> None:
    dist = frontend_dist_dir(settings)
    if dist is None:
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="frontend-assets")

    index = dist / "index.html"

    @app.get("/")
    async def spa_root() -> FileResponse:
        return FileResponse(index)

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith(_RESERVED_PREFIXES) or full_path in {
            "health",
            "docs",
            "redoc",
            "openapi.json",
        }:
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (dist / full_path).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    logger.info("Serving frontend production build from %s", dist)

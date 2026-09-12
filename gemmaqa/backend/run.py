"""CLI entrypoint for the GemmaQA backend."""

from __future__ import annotations

import asyncio
import os
import sys

import uvicorn

from app.config import get_settings


def _configure_windows_event_loop() -> None:
    """Playwright needs subprocess support; SelectorEventLoop on Windows cannot do that."""
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _bind_host_port(settings) -> tuple[str, int]:
    """Local `python run.py` stays 127.0.0.1:8000. Cloud Run binds 0.0.0.0:$PORT.

    Cloud Run always sets K_SERVICE and PORT. Do not treat a local PORT=8000 as
    Cloud Run — that would unexpectedly expose the API on all interfaces.
    """
    if os.getenv("K_SERVICE"):
        return "0.0.0.0", int(os.getenv("PORT") or settings.port or 8080)
    return settings.host, settings.port


def main() -> None:
    _configure_windows_event_loop()
    settings = get_settings()
    host, port = _bind_host_port(settings)
    # Uvicorn --reload uses a watcher process that breaks Playwright subprocess launch
    # on Windows (NotImplementedError in asyncio subprocess). Opt in explicitly only.
    reload = os.getenv("UVICORN_RELOAD", "false").lower() in {"1", "true", "yes"}
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        loop="asyncio",
    )


if __name__ == "__main__":
    main()

"""
Launch a local autonomous GemmaQA run against the demo fixtures (or a URL).

Usage (from gemmaqa/backend, venv active):

    python scripts/run_autonomous_demo.py
    python scripts/run_autonomous_demo.py https://thinking-tester-contact-list.herokuapp.com/
"""

from __future__ import annotations

import asyncio
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
DEMO_DIR = REPO_ROOT / "tests" / "fixtures" / "agent_demo"
sys.path.insert(0, str(BACKEND_ROOT))

from app.agent.controller import AgentController
from app.database import AsyncSessionLocal, init_db
from app.gemma import get_gemma_provider
from app.models import QARun
from app.schemas import CreateRunRequest, RunConfiguration
from app.utils.ids import new_id
from app.utils.logging import setup_logging


def _serve_demo() -> tuple[ThreadingHTTPServer, str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(DEMO_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/index.html"


async def _run(url: str) -> None:
    setup_logging()
    await init_db()
    run_id = new_id()
    request = CreateRunRequest(
        url=url,
        configuration=RunConfiguration(
            max_pages=5,
            max_actions=15,
            safe_mode=True,
            allow_controlled_writes=False,
            headless=True,
        ),
    )

    async with AsyncSessionLocal() as session:
        session.add(
            QARun(
                id=run_id,
                url=url,
                status="created",
                config_json=request.configuration.model_dump_json(),
            )
        )
        await session.commit()

    async def on_event(event_type: str, data: dict) -> None:
        payload = data.get("payload") if isinstance(data, dict) else {}
        print(f"[event] {event_type}: {payload}")

    controller = AgentController(
        run_id=run_id,
        request=request,
        db_factory=AsyncSessionLocal,
        on_event=on_event,
        gemma=get_gemma_provider(),
    )
    await controller.run()
    print(
        f"\nDone. pages={len(controller.memory.visited_urls)} "
        f"actions={len(controller.memory.actions)} "
        f"bugs={len(controller.memory.bugs)} "
        f"stop={controller.memory.stop_reason}"
    )
    print(f"Evidence: {REPO_ROOT / 'evidence' / run_id}")


def main() -> None:
    server = None
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        server, url = _serve_demo()
        print(f"Serving demo fixtures at {url}")
    try:
        asyncio.run(_run(url))
    finally:
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    main()

"""Live verification capture harness for the Workflow Discovery Engine.

Runs a full AgentController against a target URL (no mocked browser, no
mocked perception/entity/actor/workflow engine — only the LLM is a
deterministic mock, exactly as production defaults to when no real Gemma
model is configured) and captures the final Entity/Actor/Workflow registry
snapshots plus every workflow_discovery.* trace event, for manual/report
review. Mirrors scripts/live_acceptance_capture.py's harness pattern.

Usage (from gemmaqa/backend, venv active):

    python scripts/workflow_live_capture.py <url> <output.json> \
        [--max-actions N] [--max-pages N] [--allow-safe-writes] \
        [--username U] [--password P]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("GEMMAQA_EXPLORATION_TRACE", "true")
os.environ.setdefault("GEMMA_SUPPORTS_IMAGES", "true")

from app.agent.controller import AgentController  # noqa: E402
from app.database import AsyncSessionLocal, init_db  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.models import QARun  # noqa: E402
from app.schemas import CreateRunRequest, RunConfiguration  # noqa: E402
from app.utils.ids import new_id  # noqa: E402
from app.utils.logging import setup_logging  # noqa: E402


class _TraceCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.args or len(record.args) < 2:
                return
            event_name, fields = record.args[0], record.args[1]
            if not isinstance(fields, dict):
                return
            self.events.append({"logger": record.name, "event": str(event_name), "fields": fields})
        except Exception:
            pass


async def _run(
    url: str,
    *,
    max_actions: int,
    max_pages: int,
    allow_safe_writes: bool,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    setup_logging()
    await init_db()

    capture = _TraceCapture()
    for logger_name in ("gemmaqa.exploration.trace", "gemmaqa.intelligence.workflow_discovery"):
        logging.getLogger(logger_name).addHandler(capture)
        logging.getLogger(logger_name).setLevel(logging.INFO)

    run_id = new_id()
    request = CreateRunRequest(
        url=url,
        username=username,
        password=password,
        configuration=RunConfiguration(
            max_pages=max_pages,
            max_actions=max_actions,
            safe_mode=True,
            allow_controlled_writes=False,
            allow_safe_test_data_creation=allow_safe_writes,
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
        pass

    controller = AgentController(
        run_id=run_id,
        request=request,
        db_factory=AsyncSessionLocal,
        on_event=on_event,
        gemma=MockGemmaProvider(),
    )
    await controller.run()

    mem = controller.memory

    def _safe_dict(obj: Any) -> Any:
        if obj is None:
            return None
        try:
            return obj.to_dict()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    entity_summary = _safe_dict(getattr(mem, "entity_registry", None))
    actor_summary = _safe_dict(getattr(mem, "actor_registry", None))
    workflow_summary = _safe_dict(getattr(mem, "workflow_registry", None))

    workflow_trace_events = [e for e in capture.events if e["event"] == "workflow_discovery.observation"]

    return {
        "run_id": run_id,
        "url": url,
        "pages_visited": sorted(mem.visited_urls),
        "page_count": len(mem.visited_urls),
        "actions_taken": len(mem.actions),
        "stop_reason": mem.stop_reason,
        "authenticated": bool(mem.auth_strategy and mem.auth_strategy.authenticated),
        "entity_registry": entity_summary,
        "actor_registry": actor_summary,
        "workflow_registry": workflow_summary,
        "workflow_trace_events": workflow_trace_events,
        "workflow_trace_event_count": len(workflow_trace_events),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-actions", type=int, default=25)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--allow-safe-writes", action="store_true")
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    args = parser.parse_args()

    result = asyncio.run(
        _run(
            args.url,
            max_actions=args.max_actions,
            max_pages=args.max_pages,
            allow_safe_writes=args.allow_safe_writes,
            username=args.username,
            password=args.password,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    wf = result["workflow_registry"] or {}
    print(
        f"\nDone. pages={result['page_count']} actions={result['actions_taken']} "
        f"stop={result['stop_reason']} workflows={wf.get('summary', {})}"
    )
    print(f"Captured: {args.output}")


if __name__ == "__main__":
    main()

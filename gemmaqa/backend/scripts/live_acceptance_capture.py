"""Live acceptance capture harness for the evidence-driven perception +
priority architecture.

Runs a full AgentController against a target URL (no mocked browser, no
mocked perception/frontier/priority engine — only the LLM is a deterministic
mock, exactly as production defaults to when no real Gemma model is
configured) and captures every structured per-iteration trace event already
emitted by the system (perception.observation, perception.visual_decision,
iteration.plan, iteration.stop_policy) into one JSON file for manual/report
review — no new instrumentation invented, this only listens to what the
system already records via GEMMAQA_EXPLORATION_TRACE.

Usage (from gemmaqa/backend, venv active):

    python scripts/live_acceptance_capture.py <url> <output.json> \
        [--max-actions N] [--max-pages N] [--allow-safe-writes]
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
# Without this, PerceptionEngine's visual layer is never wired in at all
# (app.agent.controller gates it on this setting regardless of which
# provider instance is passed in) — set it so live acceptance runs can
# actually exercise/observe visual-analysis activations.
os.environ.setdefault("GEMMA_SUPPORTS_IMAGES", "true")

from app.agent.controller import AgentController  # noqa: E402
from app.database import AsyncSessionLocal, init_db  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.models import QARun  # noqa: E402
from app.reporting.report_builder import ReportBuilder  # noqa: E402
from app.schemas import CreateRunRequest, RunConfiguration  # noqa: E402
from app.utils.ids import new_id  # noqa: E402
from app.utils.logging import setup_logging  # noqa: E402


class _TraceCapture(logging.Handler):
    """Collects every EXPLORATION_TRACE record's structured fields, verbatim,
    from the loggers that already emit them — no parsing, no re-derivation."""

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
    for logger_name in ("gemmaqa.exploration.trace", "gemmaqa.perception.engine"):
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

    events: list[dict[str, Any]] = []

    async def on_event(event_type: str, data: dict) -> None:
        events.append({"type": event_type, "data": data})

    controller = AgentController(
        run_id=run_id,
        request=request,
        db_factory=AsyncSessionLocal,
        on_event=on_event,
        gemma=MockGemmaProvider(),
    )
    await controller.run()

    coverage_dict: dict[str, Any] | None = None
    try:
        record = ReportBuilder().compute_coverage(controller.memory)
        coverage_dict = record.model_dump(mode="json") if hasattr(record, "model_dump") else None
    except Exception as exc:
        coverage_dict = {"error": f"{type(exc).__name__}: {exc}"}

    final_model = controller.memory.canonical_page_model
    final_model_dict = final_model.to_dict() if final_model is not None else None

    return {
        "run_id": run_id,
        "url": url,
        "pages_visited": sorted(controller.memory.visited_urls),
        "page_count": len(controller.memory.visited_urls),
        "actions_taken": len(controller.memory.actions),
        "stop_reason": controller.memory.stop_reason,
        "authenticated": bool(
            controller.memory.auth_strategy and controller.memory.auth_strategy.authenticated
        ),
        "bugs_found": len(controller.memory.bugs),
        "goal_counts": {
            status: sum(1 for g in controller.memory.goals if g.status == status)
            for status in {"proposed", "active", "completed", "blocked", "deferred", "abandoned"}
        },
        "coverage": coverage_dict,
        "final_canonical_page_model": final_model_dict,
        "trace_events": capture.events,
        "run_events_count": len(events),
        "evidence_dir": str(REPO_ROOT / "evidence" / run_id),
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
    print(
        f"\nDone. pages={result['page_count']} actions={result['actions_taken']} "
        f"stop={result['stop_reason']} trace_events={len(result['trace_events'])}"
    )
    print(f"Captured: {args.output}")
    print(f"Evidence: {result['evidence_dir']}")


if __name__ == "__main__":
    main()

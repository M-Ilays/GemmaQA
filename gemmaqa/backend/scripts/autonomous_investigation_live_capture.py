"""Live verification capture harness for the Autonomous Investigation Engine.

Runs a full AgentController against a target URL with autonomous
investigation ENABLED (no mocked browser, no mocked perception/entity/
actor/workflow/dependency/graph/goal/scenario/strategy/investigation
engine -- only the LLM is a deterministic mock) and captures the final
investigation statistics, sample investigations of each outcome, evidence/
knowledge/coverage/confidence updates, and a handful of consistency checks
(duplicate execution, infinite-loop detection, unsafe execution), for
manual/report review. Mirrors scripts/qa_strategy_live_capture.py's pattern.

Usage (from gemmaqa/backend, venv active):

    python scripts/autonomous_investigation_live_capture.py <url> <output.json> \
        [--max-actions N] [--max-pages N] [--allow-safe-writes] \
        [--username U] [--password P]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
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


def _sample(results: list, predicate) -> dict[str, Any] | None:
    match = next((r for r in results if predicate(r)), None)
    return match.model_dump(mode="json") if match is not None else None


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
            enable_autonomous_investigation=True,
        ),
    )

    async with AsyncSessionLocal() as session:
        session.add(QARun(id=run_id, url=url, status="created", config_json=request.configuration.model_dump_json()))
        await session.commit()

    async def on_event(event_type: str, data: dict) -> None:
        pass

    controller = AgentController(run_id=run_id, request=request, db_factory=AsyncSessionLocal, on_event=on_event, gemma=MockGemmaProvider())

    start = time.perf_counter()
    await controller.run()
    elapsed = time.perf_counter() - start

    mem = controller.memory
    investigation_engine = getattr(mem, "investigation_engine", None)
    strategy_engine = getattr(mem, "strategy_engine", None)

    investigation_stats = investigation_engine.statistics().model_dump(mode="json") if investigation_engine is not None else None
    history = list(investigation_engine.query_engine.history()) if investigation_engine is not None else []
    completed = [r for r in history if r.outcome == "completed"]
    blocked = [r for r in history if r.outcome == "blocked"]
    failed = [r for r in history if r.outcome == "failed"]

    # -- consistency checks -------------------------------------------------
    duplicate_candidate_ids: list[str] = []
    seen_candidate_ids: set[str] = set()
    for r in history:
        if r.candidate_id in seen_candidate_ids:
            duplicate_candidate_ids.append(r.candidate_id)
        seen_candidate_ids.add(r.candidate_id)

    unsafe_execution: list[str] = []
    for r in completed + failed:
        scenario = mem.scenario_engine.query_engine.scenario_by_id(r.scenario_id) if mem.scenario_engine else None
        if scenario is not None and scenario.risk_assessment and scenario.risk_assessment.risk_class == "prohibited":
            unsafe_execution.append(r.investigation_id)

    max_single_investigation_steps = max((r.steps_total for r in history), default=0)

    result = {
        "run_id": run_id,
        "url": url,
        "pages_visited": sorted(mem.visited_urls),
        "page_count": len(mem.visited_urls),
        "actions_taken": len(mem.actions),
        "stop_reason": mem.stop_reason,
        "total_run_seconds": round(elapsed, 3),
        "ready_candidates_at_end": len(strategy_engine.query_engine.ready()) if strategy_engine is not None else 0,
        "investigation_statistics": investigation_stats,
        "total_investigations": len(history),
        "completed_count": len(completed),
        "blocked_count": len(blocked),
        "failed_count": len(failed),
        "duplicate_candidate_ids": duplicate_candidate_ids,
        "unsafe_execution_investigation_ids": unsafe_execution,
        "max_single_investigation_steps": max_single_investigation_steps,
        "sample_completed_investigation": _sample(history, lambda r: r.outcome == "completed"),
        "sample_blocked_investigation": _sample(history, lambda r: r.outcome == "blocked"),
        "sample_failed_investigation": _sample(history, lambda r: r.outcome == "failed"),
        "knowledge_graph_statistics": mem.knowledge_graph.statistics().model_dump(mode="json") if mem.knowledge_graph is not None else None,
    }
    return result


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
        _run(args.url, max_actions=args.max_actions, max_pages=args.max_pages, allow_safe_writes=args.allow_safe_writes, username=args.username, password=args.password)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(
        f"\nDone. pages={result['page_count']} actions={result['actions_taken']} stop={result['stop_reason']} "
        f"investigations={result['total_investigations']} completed={result['completed_count']} "
        f"blocked={result['blocked_count']} failed={result['failed_count']} "
        f"ready_remaining={result['ready_candidates_at_end']} "
        f"duplicates={len(result['duplicate_candidate_ids'])} unsafe={len(result['unsafe_execution_investigation_ids'])} "
        f"max_steps={result['max_single_investigation_steps']} "
        f"sync_time={result['total_run_seconds']}s"
    )
    print(f"Captured: {args.output}")


if __name__ == "__main__":
    main()

"""Live verification capture harness for the Goal Generation Engine.

Runs a full AgentController against a target URL (no mocked browser, no
mocked perception/entity/actor/workflow/dependency/graph/goal engine --
only the LLM is a deterministic mock) and captures the final goal
generation statistics, the top-priority goals, groups, dependencies,
conflicts, and a crude duplicate/evidence-linkage check, for manual/report
review. Mirrors scripts/knowledge_graph_live_capture.py's pattern.

Usage (from gemmaqa/backend, venv active):

    python scripts/goal_generation_live_capture.py <url> <output.json> \
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

    sync_start = time.perf_counter()
    await controller.run()
    sync_elapsed = time.perf_counter() - sync_start

    mem = controller.memory
    goal_engine = getattr(mem, "goal_engine", None)

    # One final (idempotent) generation pass to capture the CURRENT
    # stop-condition flags alongside the statistics below -- generate()
    # itself doesn't persist stop_conditions anywhere, only returns them.
    final_result = mem.generate_goals() if goal_engine is not None else None
    stop_conditions = list(final_result.stop_conditions) if final_result is not None else []

    stats = goal_engine.statistics().model_dump(mode="json") if goal_engine is not None else None
    top_goals = [g.model_dump(mode="json") for g in goal_engine.highest_priority_goals(15)] if goal_engine is not None else []
    groups = [g.model_dump(mode="json") for g in goal_engine.query_engine.all_groups()] if goal_engine is not None else []
    dependencies = [d.model_dump(mode="json") for d in goal_engine.memory.dependencies.values()] if goal_engine is not None else []
    conflicts = [c.model_dump(mode="json") for c in goal_engine.memory.conflicts.values()] if goal_engine is not None else []

    duplicate_goal_ids: list[str] = []
    unlinked_evidence: list[str] = []
    self_dependencies: list[str] = []
    if goal_engine is not None:
        graph_node_ids = set(mem.knowledge_graph.memory.nodes.keys()) if mem.knowledge_graph is not None else set()
        graph_edge_ids = set(mem.knowledge_graph.memory.edges.keys()) if mem.knowledge_graph is not None else set()
        seen_ids = set()
        for g in goal_engine.query_engine.all_goals():
            if g.goal_id in seen_ids:
                duplicate_goal_ids.append(g.goal_id)
            seen_ids.add(g.goal_id)
            for node_id in g.supporting_graph_nodes:
                if node_id not in graph_node_ids:
                    unlinked_evidence.append(f"{g.goal_id} -> missing node {node_id}")
            for edge_id in g.supporting_graph_edges:
                if edge_id not in graph_edge_ids:
                    unlinked_evidence.append(f"{g.goal_id} -> missing edge {edge_id}")
        for d in dependencies:
            if d["goal_id"] == d["depends_on_goal_id"]:
                self_dependencies.append(d["goal_id"])

    result = {
        "run_id": run_id,
        "url": url,
        "pages_visited": sorted(mem.visited_urls),
        "page_count": len(mem.visited_urls),
        "actions_taken": len(mem.actions),
        "stop_reason": mem.stop_reason,
        "total_run_seconds": round(sync_elapsed, 3),
        "goal_statistics": stats,
        "stop_conditions": stop_conditions,
        "top_goals": top_goals,
        "groups": groups,
        "dependencies": dependencies,
        "conflicts": conflicts,
        "duplicate_goal_ids": duplicate_goal_ids,
        "unlinked_evidence": unlinked_evidence,
        "self_dependencies": self_dependencies,
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
    stats = result["goal_statistics"] or {}
    print(
        f"\nDone. pages={result['page_count']} actions={result['actions_taken']} "
        f"stop={result['stop_reason']} goals={stats.get('total_goals', 0)} "
        f"groups={len(result['groups'])} deps={len(result['dependencies'])} "
        f"conflicts={len(result['conflicts'])} duplicates={len(result['duplicate_goal_ids'])} "
        f"sync_time={result['total_run_seconds']}s"
    )
    print(f"Captured: {args.output}")


if __name__ == "__main__":
    main()

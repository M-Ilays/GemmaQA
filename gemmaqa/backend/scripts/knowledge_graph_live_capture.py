"""Live verification capture harness for the Application Knowledge Graph.

Runs a full AgentController against a target URL (no mocked browser, no
mocked perception/entity/actor/workflow/dependency/graph engine — only the
LLM is a deterministic mock) and captures the final knowledge-graph
statistics, a compact snapshot, gaps, consistency issues, and a handful of
sample bounded queries/context projections, for manual/report review.
Mirrors scripts/dependency_live_capture.py's pattern.

Usage (from gemmaqa/backend, venv active):

    python scripts/knowledge_graph_live_capture.py <url> <output.json> \
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
    graph = getattr(mem, "knowledge_graph", None)

    stats = graph.statistics().model_dump(mode="json") if graph is not None else None
    compact_snapshot = graph.compact_snapshot() if graph is not None else None
    gaps = [g.model_dump(mode="json") for g in graph.gaps()] if graph is not None else []
    consistency_issues = [i.model_dump(mode="json") for i in graph.consistency_issues()] if graph is not None else []

    sample_queries: dict[str, Any] = {}
    sample_context: dict[str, Any] = {}
    false_merges: list[str] = []
    if graph is not None:
        try:
            entity_nodes = graph.query_engine.nodes_by_type("entity")
            actor_nodes = graph.query_engine.nodes_by_type("actor")
            workflow_nodes = graph.query_engine.nodes_by_type("workflow")
            output_node_types = ("derived_output", "counter", "badge", "chart", "report", "queue", "notification", "alert")
            output_nodes = [n for t in output_node_types for n in graph.query_engine.nodes_by_type(t)]

            if entity_nodes:
                sample_queries["actors_for_first_entity"] = [a.node_id for a in graph.query_engine.actors_for_entity(entity_nodes[0].node_id)]
                sample_queries["workflows_for_first_entity"] = [w.node_id for w in graph.query_engine.workflows_for_entity(entity_nodes[0].node_id)]
                sample_context["context_for_first_entity"] = graph.context_for_node(entity_nodes[0].node_id)
            if actor_nodes:
                sample_queries["workflows_for_first_actor"] = [w.node_id for w in graph.query_engine.workflows_for_actor(actor_nodes[0].node_id)]
                sample_queries["permissions_for_first_actor"] = [p.node_id for p in graph.query_engine.permissions_for_actor(actor_nodes[0].node_id)]
            if workflow_nodes:
                sample_queries["steps_for_first_workflow"] = [s.node_id for s in graph.query_engine.workflow_steps(workflow_nodes[0].node_id)]
                sample_context["context_for_first_workflow"] = graph.context_for_node(workflow_nodes[0].node_id)
            if output_nodes:
                sample_queries["producers_for_first_output"] = [p.node_id for p in graph.query_engine.producers_for_output(output_nodes[0].node_id)]
                sample_queries["consumers_for_first_output"] = [c.node_id for c in graph.query_engine.consumers_for_output(output_nodes[0].node_id)]
                sample_context["context_for_first_output"] = graph.context_for_node(output_nodes[0].node_id)

            # crude false-merge signal: equivalent_to/alias_of edges connecting
            # nodes with different node_type (almost always wrong)
            for edge_type in ("equivalent_to", "alias_of"):
                for e in graph.query_engine.edges_by_type(edge_type):
                    src, tgt = graph.query_engine.node_by_id(e.source_node_id), graph.query_engine.node_by_id(e.target_node_id)
                    if src and tgt and src.node_type != tgt.node_type:
                        false_merges.append(f"{e.source_node_id} <-> {e.target_node_id} ({edge_type})")
        except Exception as exc:
            sample_queries["error"] = f"{type(exc).__name__}: {exc}"

    result = {
        "run_id": run_id,
        "url": url,
        "pages_visited": sorted(mem.visited_urls),
        "page_count": len(mem.visited_urls),
        "actions_taken": len(mem.actions),
        "stop_reason": mem.stop_reason,
        "authenticated": bool(mem.auth_strategy and mem.auth_strategy.authenticated),
        "total_run_seconds": round(sync_elapsed, 3),
        "statistics": stats,
        "compact_snapshot_node_edge_counts": {
            "node_count": compact_snapshot["node_count"] if compact_snapshot else 0,
            "edge_count": compact_snapshot["edge_count"] if compact_snapshot else 0,
        } if compact_snapshot else None,
        "gaps": gaps,
        "consistency_issues": consistency_issues,
        "sample_queries": sample_queries,
        "sample_context_projections": sample_context,
        "false_merges": false_merges,
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
    stats = result["statistics"] or {}
    print(
        f"\nDone. pages={result['page_count']} actions={result['actions_taken']} "
        f"stop={result['stop_reason']} nodes={stats.get('total_nodes', 0)} edges={stats.get('total_edges', 0)} "
        f"gaps={len(result['gaps'])} issues={len(result['consistency_issues'])} "
        f"sync_time={result['total_run_seconds']}s"
    )
    print(f"Captured: {args.output}")


if __name__ == "__main__":
    main()

"""Live verification capture harness for the Scenario Planning Engine.

Runs a full AgentController against a target URL (no mocked browser, no
mocked perception/entity/actor/workflow/dependency/graph/goal/scenario
engine -- only the LLM is a deterministic mock) and captures the final
scenario statistics, sample scenarios of each notable kind, dependencies,
conflicts, gaps, and a handful of consistency checks (duplicates, self-
dependencies, unlinked evidence, stale graph references), for manual/
report review. Mirrors scripts/goal_generation_live_capture.py's pattern.

Usage (from gemmaqa/backend, venv active):

    python scripts/scenario_planning_live_capture.py <url> <output.json> \
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


def _sample(scenarios: list, predicate) -> dict[str, Any] | None:
    match = next((s for s in scenarios if predicate(s)), None)
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
    scenario_engine = getattr(mem, "scenario_engine", None)
    goal_engine = getattr(mem, "goal_engine", None)
    graph = getattr(mem, "knowledge_graph", None)

    final_result = mem.generate_scenarios() if scenario_engine is not None else None

    goal_stats = goal_engine.statistics().model_dump(mode="json") if goal_engine is not None else None
    scenario_stats = scenario_engine.statistics().model_dump(mode="json") if scenario_engine is not None else None
    scenarios = list(scenario_engine.query_engine.all_scenarios()) if scenario_engine is not None else []

    goals_consumed = len(goal_engine.query_engine.all_goals()) if goal_engine is not None else 0
    scenarios_per_goal = round(len(scenarios) / goals_consumed, 2) if goals_consumed else 0.0
    average_step_count = round(sum(len(s.steps) for s in scenarios) / len(scenarios), 2) if scenarios else 0.0

    duplicate_scenario_ids: list[str] = []
    self_dependencies: list[str] = []
    unlinked_evidence: list[str] = []
    stale_references: list[str] = []
    if scenario_engine is not None:
        seen_ids = set()
        graph_node_ids = set(graph.memory.nodes.keys()) if graph is not None else set()
        for s in scenarios:
            if s.scenario_id in seen_ids:
                duplicate_scenario_ids.append(s.scenario_id)
            seen_ids.add(s.scenario_id)
            for node_id in s.source_graph_nodes:
                if node_id not in graph_node_ids:
                    unlinked_evidence.append(f"{s.scenario_id} -> missing node {node_id}")
                elif graph.memory.nodes[node_id].stale:
                    stale_references.append(f"{s.scenario_id} -> stale node {node_id}")
        for d in scenario_engine.memory.dependencies.values():
            if d.scenario_id == d.required_scenario_id:
                self_dependencies.append(d.scenario_id)

    result = {
        "run_id": run_id,
        "url": url,
        "pages_visited": sorted(mem.visited_urls),
        "page_count": len(mem.visited_urls),
        "actions_taken": len(mem.actions),
        "stop_reason": mem.stop_reason,
        "total_run_seconds": round(elapsed, 3),
        "goals_consumed": goals_consumed,
        "goal_statistics": goal_stats,
        "scenario_statistics": scenario_stats,
        "scenarios_per_goal": scenarios_per_goal,
        "average_step_count": average_step_count,
        "duplicate_scenario_ids": duplicate_scenario_ids,
        "self_dependencies": self_dependencies,
        "unlinked_evidence": unlinked_evidence,
        "stale_references": stale_references,
        "sample_workflow_scenario": _sample(scenarios, lambda s: s.scenario_type == "workflow_verification" and s.scenario_id.endswith(":primary")),
        "sample_dependency_scenario": _sample(scenarios, lambda s: s.scenario_type in {"dependency_verification", "metric_verification"} and s.scenario_id.endswith(":primary")),
        "sample_permission_scenario": _sample(scenarios, lambda s: s.scenario_type in {"permission_positive_verification", "permission_negative_verification"}),
        "sample_blocked_scenario": _sample(scenarios, lambda s: s.feasibility_status == "blocked"),
        "sample_alternative_pair": (
            {"primary": _sample(scenarios, lambda s: s.scenario_id.endswith(":observational")), "note": "primary counterpart shares the same goal_id"}
            if any(s.scenario_id.endswith(":observational") for s in scenarios)
            else None
        ),
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
    stats = result["scenario_statistics"] or {}
    print(
        f"\nDone. pages={result['page_count']} actions={result['actions_taken']} stop={result['stop_reason']} "
        f"goals={result['goals_consumed']} scenarios={stats.get('total_scenarios', 0)} "
        f"feasible={stats.get('scenarios_by_feasibility', {}).get('feasible', 0)} "
        f"conditionally_feasible={stats.get('scenarios_by_feasibility', {}).get('conditionally_feasible', 0)} "
        f"blocked={stats.get('scenarios_by_feasibility', {}).get('blocked', 0)} "
        f"incomplete={stats.get('scenarios_by_feasibility', {}).get('incomplete', 0)} "
        f"read_only={stats.get('read_only_count', 0)} mutating={stats.get('mutating_count', 0)} "
        f"cross_actor={stats.get('cross_actor_count', 0)} high_risk={stats.get('high_risk_count', 0)} "
        f"deps={stats.get('dependency_count', 0)} conflicts={stats.get('conflict_count', 0)} gaps={stats.get('gap_count', 0)} "
        f"duplicates={len(result['duplicate_scenario_ids'])} self_deps={len(result['self_dependencies'])} "
        f"unlinked={len(result['unlinked_evidence'])} stale_refs={len(result['stale_references'])} "
        f"sync_time={result['total_run_seconds']}s"
    )
    print(f"Captured: {args.output}")


if __name__ == "__main__":
    main()

"""Live verification capture harness for the QA Strategy Engine.

Runs a full AgentController against a target URL (no mocked browser, no
mocked perception/entity/actor/workflow/dependency/graph/goal/scenario/
strategy engine -- only the LLM is a deterministic mock) and captures the
final strategy statistics, queues, batches, sample candidates, and a
handful of consistency checks (duplicate candidates, dependencies/batches
referencing unknown candidates, candidates in more than one queue/batch,
non-deterministic re-ordering), for manual/report review. Mirrors
scripts/scenario_planning_live_capture.py's pattern.

Usage (from gemmaqa/backend, venv active):

    python scripts/qa_strategy_live_capture.py <url> <output.json> \
        [--max-actions N] [--max-pages N] [--allow-safe-writes] \
        [--username U] [--password P] [--policy POLICY_ID]
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


def _sample(candidates: list, predicate) -> dict[str, Any] | None:
    match = next((c for c in candidates if predicate(c)), None)
    return match.model_dump(mode="json") if match is not None else None


async def _run(
    url: str,
    *,
    max_actions: int,
    max_pages: int,
    allow_safe_writes: bool,
    policy_id: str,
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
    strategy_engine = getattr(mem, "strategy_engine", None)
    scenario_engine = getattr(mem, "scenario_engine", None)
    goal_engine = getattr(mem, "goal_engine", None)

    final_result = mem.generate_strategy(policy_id=policy_id) if strategy_engine is not None else None

    scenario_stats = scenario_engine.statistics().model_dump(mode="json") if scenario_engine is not None else None
    strategy_stats = strategy_engine.statistics().model_dump(mode="json") if strategy_engine is not None else None
    candidates = list(strategy_engine.query_engine.all_candidates()) if strategy_engine is not None else []
    queues = list(strategy_engine.query_engine.all_queues()) if strategy_engine is not None else []
    batches = list(strategy_engine.query_engine.all_batches()) if strategy_engine is not None else []

    scenarios_consumed = len(scenario_engine.query_engine.all_scenarios()) if scenario_engine is not None else 0

    # -- consistency checks -------------------------------------------------
    duplicate_candidate_ids: list[str] = []
    seen_ids = set()
    for c in candidates:
        if c.candidate_id in seen_ids:
            duplicate_candidate_ids.append(c.candidate_id)
        seen_ids.add(c.candidate_id)

    known_ids = {c.candidate_id for c in candidates}
    dangling_dependencies = [
        f"{d.dependency_id}: {d.candidate_id} -> {d.required_candidate_id}"
        for d in (strategy_engine.memory.dependencies.values() if strategy_engine is not None else [])
        if d.candidate_id not in known_ids or d.required_candidate_id not in known_ids
    ]

    queue_membership: dict[str, list[str]] = {}
    for q in queues:
        for cid in q.candidate_ids:
            queue_membership.setdefault(cid, []).append(q.queue_type)
    candidates_in_multiple_queues = [cid for cid, qs in queue_membership.items() if len(qs) > 1]

    batch_membership: dict[str, list[str]] = {}
    for b in batches:
        for cid in b.candidate_ids:
            batch_membership.setdefault(cid, []).append(b.batch_id)
    candidates_in_multiple_batches = [cid for cid, bs in batch_membership.items() if len(bs) > 1]

    blocked_without_reason = [
        c.candidate_id for c in candidates if c.recommended_action == "block" and not c.blocking_reasons and c.risk_class != "prohibited"
    ]

    # -- determinism check: regenerate once more, ordering must be identical
    first_ordering = final_result.ordering.ordered_candidate_ids if final_result is not None else []
    regenerated = mem.generate_strategy(policy_id=policy_id) if strategy_engine is not None else None
    second_ordering = regenerated.ordering.ordered_candidate_ids if regenerated is not None else []
    idempotent_ordering = first_ordering == second_ordering
    idempotent_version = (final_result.strategy_version == regenerated.strategy_version) if final_result is not None and regenerated is not None else None

    result = {
        "run_id": run_id,
        "url": url,
        "pages_visited": sorted(mem.visited_urls),
        "page_count": len(mem.visited_urls),
        "actions_taken": len(mem.actions),
        "stop_reason": mem.stop_reason,
        "total_run_seconds": round(elapsed, 3),
        "policy_used": policy_id,
        "scenarios_consumed": scenarios_consumed,
        "scenario_statistics": scenario_stats,
        "strategy_statistics": strategy_stats,
        "queue_sizes": {q.queue_type: len(q.candidate_ids) for q in queues},
        "batch_count": len(batches),
        "duplicate_candidate_ids": duplicate_candidate_ids,
        "dangling_dependencies": dangling_dependencies,
        "candidates_in_multiple_queues": candidates_in_multiple_queues,
        "candidates_in_multiple_batches": candidates_in_multiple_batches,
        "blocked_without_reason": blocked_without_reason,
        "idempotent_ordering_on_regenerate": idempotent_ordering,
        "idempotent_version_on_regenerate": idempotent_version,
        "sample_immediate_candidate": _sample(candidates, lambda c: c.queue_type == "immediate"),
        "sample_blocked_candidate": _sample(candidates, lambda c: c.queue_type == "blocked"),
        "sample_deferred_candidate": _sample(candidates, lambda c: c.queue_type == "deferred"),
        "sample_cross_actor_candidate": _sample(candidates, lambda c: c.queue_type == "cross_actor"),
        "top_recommended_sequence": strategy_engine.query_engine.recommended_sequence()[:10] if strategy_engine is not None else [],
        "coverage_forecast": (strategy_engine.query_engine.coverage_forecast().model_dump(mode="json") if strategy_engine is not None and strategy_engine.query_engine.coverage_forecast() else None),
        "confidence_forecast": (strategy_engine.query_engine.confidence_forecast().model_dump(mode="json") if strategy_engine is not None and strategy_engine.query_engine.confidence_forecast() else None),
        "risk_forecast": (strategy_engine.query_engine.risk_forecast().model_dump(mode="json") if strategy_engine is not None and strategy_engine.query_engine.risk_forecast() else None),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-actions", type=int, default=25)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--allow-safe-writes", action="store_true")
    parser.add_argument("--policy", default="balanced")
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    args = parser.parse_args()

    result = asyncio.run(
        _run(
            args.url, max_actions=args.max_actions, max_pages=args.max_pages, allow_safe_writes=args.allow_safe_writes,
            policy_id=args.policy, username=args.username, password=args.password,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    stats = result["strategy_statistics"] or {}
    print(
        f"\nDone. pages={result['page_count']} actions={result['actions_taken']} stop={result['stop_reason']} "
        f"scenarios={result['scenarios_consumed']} candidates={stats.get('total_candidates', 0)} "
        f"policy={result['policy_used']} "
        f"by_queue={result['queue_sizes']} batches={result['batch_count']} "
        f"by_action={stats.get('candidates_by_action', {})} "
        f"deps={stats.get('dependency_count', 0)} conflicts={stats.get('conflict_count', 0)} "
        f"duplicates={len(result['duplicate_candidate_ids'])} dangling_deps={len(result['dangling_dependencies'])} "
        f"multi_queue={len(result['candidates_in_multiple_queues'])} multi_batch={len(result['candidates_in_multiple_batches'])} "
        f"blocked_without_reason={len(result['blocked_without_reason'])} "
        f"idempotent_ordering={result['idempotent_ordering_on_regenerate']} idempotent_version={result['idempotent_version_on_regenerate']} "
        f"sync_time={result['total_run_seconds']}s"
    )
    print(f"Captured: {args.output}")


if __name__ == "__main__":
    main()

"""Run-manager operations used by Strands tools. No LLM dependency."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.agent.run_manager import run_manager
from app.database import AsyncSessionLocal
from app.models import QARun
from app.reporting.activity_log import ActivityLog
from app.strands_agent.context import active_run_request, last_launched_run_id
from app.utils.sanitization import sanitize_dict


def _run_summary(run: QARun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "status": run.status,
        "url": run.url,
        "message": run.message,
        "pages_visited": run.pages_visited,
        "actions_taken": run.actions_taken,
        "bugs_found": run.bugs_found,
        "error": run.error,
    }


async def launch_qa_run() -> dict[str, Any]:
    request = active_run_request.get()
    if request is None:
        return {"ok": False, "error": "No authorized run request is in scope."}
    if not request.authorization_ack:
        return {"ok": False, "error": "authorization_ack must be true."}

    note = (request.notes or "").strip()
    tag = "Coordinated by AWS Strands Agents SDK"
    request.notes = f"{note} · {tag}" if note else tag

    async with AsyncSessionLocal() as db:
        run_id = await run_manager.create_run(request, db)
        run = await run_manager.start_run(run_id, db)
        last_launched_run_id.set(run_id)
        return {"ok": True, **_run_summary(run)}


async def get_run_status(run_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(QARun).where(QARun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            return {"ok": False, "error": "Run not found"}
        return {"ok": True, **_run_summary(run)}


async def pause_qa_run(run_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        run = await run_manager.pause_run(run_id, db)
        return {"ok": True, **_run_summary(run)}


async def resume_qa_run(run_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        run = await run_manager.resume_run(run_id, db)
        return {"ok": True, **_run_summary(run)}


async def end_qa_run(run_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        run = await run_manager.cancel_run(run_id, db)
        return {"ok": True, **_run_summary(run)}


async def get_activity_log(run_id: str, since_seq: int = 0, limit: int = 40) -> dict[str, Any]:
    records = ActivityLog.read(run_id, since_seq=since_seq, limit=min(max(limit, 1), 100))
    return {
        "ok": True,
        "run_id": run_id,
        "entries": [sanitize_dict(record) for record in records],
    }


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)

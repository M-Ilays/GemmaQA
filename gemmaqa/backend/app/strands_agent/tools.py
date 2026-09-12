"""Strands @tool wrappers around existing GemmaQA run operations."""

from __future__ import annotations

from strands import tool

from app.strands_agent.operations import (
    dumps,
    end_qa_run,
    get_activity_log,
    get_run_status,
    launch_qa_run,
    pause_qa_run,
    resume_qa_run,
)


@tool
async def launch_authorized_qa_run() -> str:
    """Start an authorized exploratory QA run on the target already in scope.

    The operator already acknowledged authorization. Credentials stay in
    process memory and are never part of this tool's arguments. Uses the
    existing AgentController + Playwright engine.
    """
    return dumps(await launch_qa_run())


@tool
async def inspect_qa_run_status(run_id: str) -> str:
    """Get status, action counts, and bugs found for a GemmaQA run."""
    return dumps(await get_run_status(run_id))


@tool
async def pause_authorized_qa_run(run_id: str) -> str:
    """Pause a live QA run after the current browser step."""
    return dumps(await pause_qa_run(run_id))


@tool
async def resume_authorized_qa_run(run_id: str) -> str:
    """Continue a paused QA run."""
    return dumps(await resume_qa_run(run_id))


@tool
async def end_authorized_qa_run(run_id: str) -> str:
    """End a QA run now and keep the report collected so far."""
    return dumps(await end_qa_run(run_id))


@tool
async def read_qa_activity_log(run_id: str, since_seq: int = 0) -> str:
    """Read recent activity-log entries for a run (no secrets)."""
    return dumps(await get_activity_log(run_id, since_seq=since_seq))

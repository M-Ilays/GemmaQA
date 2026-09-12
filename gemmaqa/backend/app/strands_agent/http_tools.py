"""HTTP-based tools for AgentCore deployment.

When the Strands agent is deployed to AgentCore Runtime, these tools make
HTTP requests to the GemmaQA backend API instead of calling functions directly.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from strands import tool

from app.strands_agent.operations import dumps


def get_backend_url() -> str:
    """Get the backend URL from environment or use local default."""
    url = os.getenv("GEMMAQA_BACKEND_URL", "http://127.0.0.1:8000")
    return url.rstrip("/")


async def _http_post(endpoint: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make an HTTP POST request to the backend."""
    url = f"{get_backend_url()}{endpoint}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=json or {})
        response.raise_for_status()
        return response.json()


async def _http_get(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make an HTTP GET request to the backend."""
    url = f"{get_backend_url()}{endpoint}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, params=params or {})
        response.raise_for_status()
        return response.json()


@tool
async def launch_authorized_qa_run() -> str:
    """Start an authorized exploratory QA run on the target already in scope.

    The operator already acknowledged authorization. Credentials stay in
    process memory and are never part of this tool's arguments. Uses the
    existing AgentController + Playwright engine.
    """
    result = await _http_post("/api/runs/strands")
    return dumps(result)


@tool
async def inspect_qa_run_status(run_id: str) -> str:
    """Get status, action counts, and bugs found for a GemmaQA run."""
    result = await _http_get(f"/api/runs/{run_id}")
    return dumps(result)


@tool
async def pause_authorized_qa_run(run_id: str) -> str:
    """Pause a live QA run after the current browser step."""
    result = await _http_post(f"/api/runs/{run_id}/pause")
    return dumps(result)


@tool
async def resume_authorized_qa_run(run_id: str) -> str:
    """Continue a paused QA run."""
    result = await _http_post(f"/api/runs/{run_id}/resume")
    return dumps(result)


@tool
async def end_authorized_qa_run(run_id: str) -> str:
    """End a QA run now and keep the report collected so far."""
    result = await _http_post(f"/api/runs/{run_id}/end")
    return dumps(result)


@tool
async def read_qa_activity_log(run_id: str, since_seq: int = 0) -> str:
    """Read recent activity-log entries for a run (no secrets)."""
    result = await _http_get(f"/api/runs/{run_id}/activity", params={"since_seq": since_seq})
    return dumps(result)

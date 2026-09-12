"""Invoke the Strands coordinator for an authorized New Run payload."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.schemas import CreateRunRequest
from app.strands_agent.context import active_run_request, last_launched_run_id
from app.utils.logging import get_logger

logger = get_logger("strands.service")


def strands_status() -> dict[str, Any]:
    """Health snapshot. Never includes credentials."""
    settings = get_settings()
    installed = True
    try:
        import strands  # noqa: F401
    except ImportError:
        installed = False
    model_id = settings.effective_strands_model_id
    configured = bool(installed and model_id and settings.aws_region)
    return {
        "installed": installed,
        "enabled": settings.use_strands_orchestration,
        "configured": configured,
        "model_id": model_id or None,
        "aws_region": settings.aws_region or None,
        # /api/runs/strands works without USE_STRANDS_ORCHESTRATION.
        "ready": configured,
        "sdk": "strands-agents",
    }


def _operator_prompt(request: CreateRunRequest) -> str:
    cfg = request.configuration
    objective = (cfg.testing_objective or "").strip() or "Explore and exercise core workflows."
    return (
        "Coordinate an authorized exploratory QA run.\n"
        f"Target URL: {request.url}\n"
        f"Testing objective: {objective}\n"
        f"Allow login: {cfg.allow_login}\n"
        f"Allow safe test-data creation: {cfg.allow_safe_test_data_creation}\n"
        f"Allow deletion of GemmaQA test records: {cfg.allow_destructive_actions}\n"
        "Call launch_authorized_qa_run now, then inspect_qa_run_status.\n"
        "Return the run_id."
    )


async def run_with_strands_agent(
    request: CreateRunRequest,
    *,
    require_enabled_flag: bool = True,
) -> dict[str, Any]:
    """Run the Strands agent, which starts AgentController via a tool."""
    from app.strands_agent.qa_agent import build_qa_agent

    settings = get_settings()
    if require_enabled_flag and not settings.use_strands_orchestration:
        raise RuntimeError(
            "Strands orchestration is off. Set USE_STRANDS_ORCHESTRATION=true."
        )

    token = active_run_request.set(request)
    launched = last_launched_run_id.set(None)
    try:
        agent = build_qa_agent(callback_handler=None)
        result = await agent.invoke_async(_operator_prompt(request))
        run_id = last_launched_run_id.get()
        message = ""
        if hasattr(result, "message"):
            message = str(result.message)
        elif result is not None:
            message = str(result)
        logger.info("Strands coordinator finished run_id=%s", run_id)
        return {
            "run_id": run_id,
            "status": "initializing" if run_id else "failed",
            "message": "Strands Agent launched the QA run" if run_id else message,
            "agent_framework": "strands-agents",
            "agent_reply": message[:2000],
        }
    finally:
        active_run_request.reset(token)
        last_launched_run_id.reset(launched)

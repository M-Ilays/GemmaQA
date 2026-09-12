"""Build the Strands Agent used to coordinate GemmaQA runs."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.strands_agent.tools import (
    end_authorized_qa_run,
    inspect_qa_run_status,
    launch_authorized_qa_run,
    pause_authorized_qa_run,
    read_qa_activity_log,
    resume_authorized_qa_run,
)

SYSTEM_PROMPT = """You are GemmaQA's professional QA coordinator, built with the
AWS Strands Agents SDK.

You help QA testers and small teams with repetitive exploratory testing. You do
not replace human judgment on whether something is a real bug.

Rules:
- Only test the authorized URL already in scope. Never invent a different site.
- You MUST call launch_authorized_qa_run to start the browser QA engine.
- After launch, call inspect_qa_run_status with the returned run_id.
- Do not ask the operator for passwords. Credentials are already held in memory.
- Do not claim you clicked or typed yourself. AgentController + Playwright do that.
- Reply with the run_id and a short plan the tester can watch in the live UI.
"""


def build_qa_agent(*, callback_handler: Any = None) -> Any:
    """Create a Strands Agent. Imports the SDK only when called."""
    from strands import Agent
    from strands.models import BedrockModel

    settings = get_settings()
    model_id = settings.effective_strands_model_id
    if not model_id:
        raise RuntimeError(
            "Strands requires STRANDS_MODEL_ID or BEDROCK_MODEL_ID "
            "(Amazon Bedrock model id). Do not put secrets in git."
        )
    if not settings.aws_region:
        raise RuntimeError(
            "Strands requires AWS_REGION for Amazon Bedrock "
            "(for example us-east-1)."
        )

    model = BedrockModel(
        model_id=model_id,
        region_name=settings.aws_region,
        temperature=0.2,
    )
    return Agent(
        name="gemmaqa_strands_coordinator",
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            launch_authorized_qa_run,
            inspect_qa_run_status,
            pause_authorized_qa_run,
            resume_authorized_qa_run,
            end_authorized_qa_run,
            read_qa_activity_log,
        ],
        callback_handler=callback_handler,
    )

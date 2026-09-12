"""Print how to run the official Strands + GemmaQA path. Does not call AWS."""

from __future__ import annotations

print(
    """
GemmaQA + Strands Agents SDK
https://strandsagents.com/docs/user-guide/quickstart/python/

1. pip install -r requirements-strands.txt
2. Create an AWS account and request hackathon credits before 11 Sep 2026 12:00 PT.
3. Enable a Bedrock model in your region.
4. Put AWS credentials in a local .env only (never git):
     USE_STRANDS_ORCHESTRATION=true
     AWS_REGION=us-east-1
     BEDROCK_MODEL_ID=<your-bedrock-model-id>
     GEMMA_PROVIDER=bedrock
5. Start the API (python run.py) and UI. New Run uses Strands when the flag is true.
   Or POST /api/runs/strands with the same JSON as POST /api/runs.

The Strands Agent only coordinates. AgentController + Playwright still drive the browser.
"""
)

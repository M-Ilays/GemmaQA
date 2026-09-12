"""AgentCore Runtime entrypoint for GemmaQA Strands coordinator.

This module provides the entry point for deploying the GemmaQA Strands agent
to Amazon Bedrock AgentCore Runtime. The agent coordinates QA runs by calling
tools that interact with the GemmaQA backend API.

Usage:
    Local testing:
        python agentcore_app.py
    
    Deploy to AgentCore:
        agentcore create --agent gemmaqa-coordinator --framework strands --model-provider bedrock
        agentcore deploy
"""

import os
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Import AgentCore-specific agent builder (uses HTTP tools)
from app.strands_agent.agentcore_builder import build_agentcore_agent

# Create AgentCore app instance
app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    """
    AgentCore Runtime invokes this function for each agent request.
    
    Args:
        payload: Dict containing:
            - prompt: The user's request (e.g., "Test the Contact List app")
            - session_id: Optional session identifier for memory
            - actor_id: Optional user identifier
    
    Returns:
        Dict with agent response and metadata
    """
    # Build the Strands agent with HTTP-based tools
    agent = build_agentcore_agent()
    
    # Extract prompt from payload
    prompt = payload.get("prompt", "Coordinate an authorized QA run")
    session_id = payload.get("session_id", "default")
    
    # Invoke the agent
    result = await agent.invoke_async(prompt)
    
    # Format response
    message = str(result.message) if hasattr(result, "message") else str(result)
    
    return {
        "message": message,
        "agent": "gemmaqa_strands_coordinator",
        "session_id": session_id,
        "framework": "strands-agents",
        "runtime": "agentcore"
    }


# Health check is handled automatically by AgentCore Runtime
# No custom health_check decorator needed


if __name__ == "__main__":
    # Run the AgentCore app locally on port 8080
    port = int(os.getenv("PORT", "8080"))
    print(f"Starting GemmaQA AgentCore app on port {port}")
    app.run(host="0.0.0.0", port=port)

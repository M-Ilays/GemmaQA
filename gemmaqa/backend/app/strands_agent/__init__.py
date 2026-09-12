"""Strands Agents SDK integration for GemmaQA.

The Strands Agent coordinates a QA run. Existing AgentController + Playwright
do the browser work via tools. This package does not rewrite AgentController.
"""

from app.strands_agent.service import run_with_strands_agent, strands_status

__all__ = ["run_with_strands_agent", "strands_status"]

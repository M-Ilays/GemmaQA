"""Legacy API provider alias → OpenAICompatibleGemmaProvider."""

from __future__ import annotations

from app.gemma.openai_compatible import OpenAICompatibleGemmaProvider


class ApiGemmaProvider(OpenAICompatibleGemmaProvider):
    """Backward-compatible name for OpenAI-compatible hosted/local endpoints."""

    name = "api"

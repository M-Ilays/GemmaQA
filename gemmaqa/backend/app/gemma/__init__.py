"""Gemma provider factory — never loads models at import time."""

from __future__ import annotations

from app.config import get_settings
from app.gemma.base import ActionGenerationRequest, GemmaProvider
# Safe to import at module level: `bedrock_provider` imports no SDK of its own —
# boto3 is imported inside `_build_client`, so a mock or openai_compatible
# install never needs the Bedrock extra. Same arrangement as
# `transformers_provider` and `gemini_provider`, which defer torch/google-genai
# the same way.
from app.gemma.bedrock_provider import BedrockProvider
from app.gemma.gemini_provider import GeminiProvider
from app.gemma.health import get_active_health
from app.gemma.mock_provider import MockGemmaProvider, StubGemmaProvider
from app.gemma.openai_compatible import OpenAICompatibleGemmaProvider
from app.gemma.transformers_provider import TransformersGemmaProvider
from app.utils.logging import get_logger

logger = get_logger("gemma")

_PROVIDER_SINGLETON: GemmaProvider | None = None


def reset_gemma_provider() -> None:
    """Clear cached provider (tests / config reload)."""
    global _PROVIDER_SINGLETON
    _PROVIDER_SINGLETON = None


def get_gemma_provider(*, force_new: bool = False) -> GemmaProvider:
    """
    Instantiate (or return cached) configured Gemma provider.

    Safe for unit tests: selecting mock never loads a model.
    Real providers are constructed without loading weights; transformers
    loads lazily on first generate call only.
    """
    global _PROVIDER_SINGLETON
    if _PROVIDER_SINGLETON is not None and not force_new:
        return _PROVIDER_SINGLETON

    settings = get_settings()
    provider_name = settings.normalized_gemma_provider

    if provider_name == "mock":
        logger.info(
            "Using MockGemmaProvider (deterministic heuristics — not a live Gemma model)"
        )
        provider: GemmaProvider = MockGemmaProvider()
    elif provider_name == "transformers":
        model_ref = settings.effective_gemma_model_id or settings.gemma_local_model_path
        if not model_ref:
            raise RuntimeError(
                "GEMMA_PROVIDER=transformers requires GEMMA_MODEL_ID or "
                "GEMMA_LOCAL_MODEL_PATH. Refusing silent fallback to mock."
            )
        logger.info(
            "Using TransformersGemmaProvider model=%s (lazy load on first call)",
            model_ref,
        )
        provider = TransformersGemmaProvider()
    elif provider_name == "openai_compatible":
        if not settings.effective_gemma_api_base or not settings.effective_gemma_model_id:
            raise RuntimeError(
                "GEMMA_PROVIDER=openai_compatible requires GEMMA_API_BASE_URL "
                "(e.g. http://127.0.0.1:11434/v1) and GEMMA_MODEL_ID "
                "(e.g. gemma3:4b). Refusing silent fallback to mock."
            )
        logger.info(
            "Using OpenAICompatibleGemmaProvider model=%s base=%s",
            settings.effective_gemma_model_id,
            settings.effective_gemma_api_base,
        )
        provider = OpenAICompatibleGemmaProvider()
        if not provider.health.configured:
            raise RuntimeError(
                provider.health.config_error
                or "OpenAI-compatible Gemma provider is not configured"
            )
    elif provider_name == "bedrock":
        if not settings.aws_region or not settings.effective_bedrock_model_id:
            raise RuntimeError(
                "GEMMA_PROVIDER=bedrock requires AWS_REGION (e.g. us-east-1) and "
                "BEDROCK_MODEL_ID (e.g. eu.anthropic.claude-sonnet-4-20250514-v1:0). "
                "Refusing silent fallback to mock."
            )
        # Auth is NOT required here. An absent bearer token is legitimate when
        # the standard AWS credential chain supplies SigV4 credentials (an
        # instance role, a named profile), so refusing at this point would break
        # a valid deployment. A genuinely missing credential surfaces at call
        # time as the `missing_credentials` failure kind, which says so exactly.
        # The auth MODE is logged; the token itself never is.
        logger.info(
            "Using BedrockProvider model=%s region=%s auth=%s",
            settings.effective_bedrock_model_id,
            settings.aws_region,
            "bearer_token" if settings.bedrock_auth_configured else "aws_credential_chain",
        )
        provider = BedrockProvider()
        if not provider.health.configured:
            raise RuntimeError(
                provider.health.config_error or "Bedrock provider is not configured"
            )
    elif provider_name == "gemini":
        if not settings.effective_gemini_api_key:
            raise RuntimeError(
                "GEMMA_PROVIDER=gemini requires GEMINI_API_KEY. "
                "Obtain your API key from https://aistudio.google.com/apikey. "
                "Never commit this key to version control. "
                "Refusing silent fallback to mock."
            )
        # Model ID is optional - defaults to gemini-3.5-flash
        model_id = settings.effective_gemini_model_id or "gemini-3.5-flash"
        logger.info(
            "Using GeminiProvider model=%s multimodal=%s",
            model_id,
            settings.effective_gemma_supports_images,
        )
        provider = GeminiProvider()
        if not provider.health.configured:
            raise RuntimeError(
                provider.health.config_error or "Gemini provider is not configured"
            )
    else:
        raise RuntimeError(
            f"Unknown GEMMA_PROVIDER={settings.gemma_provider!r}. "
            "Use mock | openai_compatible | transformers | bedrock | gemini. "
            "Refusing silent fallback to mock."
        )

    _PROVIDER_SINGLETON = provider
    return provider


def __getattr__(name: str):
    if name == "ApiGemmaProvider":
        from app.gemma.api_provider import ApiGemmaProvider

        return ApiGemmaProvider
    if name == "LocalGemmaProvider":
        from app.gemma.local_provider import LocalGemmaProvider

        return LocalGemmaProvider
    raise AttributeError(name)


__all__ = [
    "GemmaProvider",
    "ActionGenerationRequest",
    "MockGemmaProvider",
    "StubGemmaProvider",
    "OpenAICompatibleGemmaProvider",
    "TransformersGemmaProvider",
    "BedrockProvider",
    "GeminiProvider",
    "get_gemma_provider",
    "reset_gemma_provider",
    "get_active_health",
]

"""Tests for the Gemini provider.

These tests validate initialization, configuration, and error handling
WITHOUT making real API calls to avoid costs and key requirements.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import get_settings
from app.gemma.gemini_provider import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_MIN_OUTPUT_TOKENS,
    GeminiProvider,
)
from app.gemma.health import ProviderHealth


@pytest.fixture
def mock_settings_with_gemini_key(monkeypatch):
    """Settings with a configured Gemini API key."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key-12345")
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    # Clear the settings cache
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def mock_settings_no_key(monkeypatch):
    """Settings without a Gemini API key."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMMA_API_KEY", raising=False)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_gemini_provider_initialization_with_key(mock_settings_with_gemini_key):
    """Test GeminiProvider initializes correctly with API key."""
    provider = GeminiProvider()
    
    assert provider.name == "gemini"
    assert provider.api_key == "test-api-key-12345"
    assert provider.model == DEFAULT_GEMINI_MODEL
    assert provider.health.configured is True
    assert not provider.health.config_error  # Empty string when configured
    assert provider.health.provider_type == "gemini"
    # Reachability is unknown until first call
    assert provider.health.reachable is None


def test_gemini_provider_initialization_without_key(mock_settings_no_key):
    """Test GeminiProvider initializes but reports not configured when key is missing."""
    provider = GeminiProvider()
    
    assert provider.name == "gemini"
    assert provider.api_key == ""
    assert provider.health.configured is False
    assert provider.health.config_error == "GEMINI_API_KEY is not configured"


def test_gemini_provider_custom_model_id(monkeypatch):
    """Test GeminiProvider uses custom model ID when provided."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-3.7-flash")
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.model == "gemini-3.7-flash"
    
    get_settings.cache_clear()


def test_gemini_provider_defaults_model_when_not_specified(monkeypatch):
    """Test GeminiProvider defaults to gemini-3.5-flash."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL_ID", raising=False)
    monkeypatch.delenv("GEMMA_MODEL_ID", raising=False)
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.model == DEFAULT_GEMINI_MODEL
    
    get_settings.cache_clear()


def test_gemini_provider_multimodal_support(monkeypatch):
    """Test GeminiProvider respects multimodal settings."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_SUPPORTS_IMAGES", "true")
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.supports_images is True
    assert provider.health.multimodal_support is True
    
    get_settings.cache_clear()


def test_gemini_provider_lazy_client_initialization(mock_settings_with_gemini_key):
    """Test that client is not initialized until first use."""
    provider = GeminiProvider()
    
    # Client should be None initially
    assert provider._client is None
    assert provider._genai_module is None


def test_gemini_provider_lazy_init_fails_without_key(mock_settings_no_key):
    """Test that lazy_init_client raises clear error when key is missing."""
    provider = GeminiProvider()
    
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not configured"):
        provider._lazy_init_client()


def test_gemini_provider_lazy_init_fails_without_sdk(mock_settings_with_gemini_key):
    """Test that lazy_init_client raises clear error when SDK not installed."""
    provider = GeminiProvider()

    import builtins

    real_import = builtins.__import__

    def _missing_genai(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "google" or str(name).startswith("google."):
            raise ImportError("mocked missing google-genai")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=_missing_genai):
        with pytest.raises(RuntimeError, match="google-genai SDK not installed"):
            provider._lazy_init_client()


@pytest.mark.asyncio
async def test_gemini_provider_generate_requires_configuration(mock_settings_no_key):
    """Test that _generate raises clear error when not configured."""
    provider = GeminiProvider()
    
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not configured"):
        await provider._generate(
            system="Test system",
            user="Test user"
        )


@pytest.mark.asyncio
async def test_gemini_provider_generate_text_only(mock_settings_with_gemini_key):
    """Test text-only generation (mocked SDK)."""
    provider = GeminiProvider()
    
    # Mock the google.genai module and client
    mock_response = MagicMock()
    mock_response.text = "This is a test response from Gemini"
    
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(return_value=mock_response)
    
    # Directly set the client and module (bypassing lazy init)
    provider._client = mock_client
    provider._genai_module = MagicMock()  # Dummy module
    
    result = await provider._generate(
        system="You are a helpful assistant",
        user="Say hello"
    )
    
    assert result == "This is a test response from Gemini"
    assert provider.health.reachable is True


@pytest.mark.asyncio
async def test_gemini_new_run_defaults_json_mime_and_thinking_token_floor(
    mock_settings_with_gemini_key,
):
    """AgentController omits json_mode / max_output_tokens; Gemini must still
    send JSON MIME and at least 4096 output tokens so thinking cannot truncate."""
    provider = GeminiProvider()
    mock_response = MagicMock()
    mock_response.text = '{"status": "ok"}'
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(return_value=mock_response)
    provider._client = mock_client
    provider._genai_module = MagicMock()

    await provider._generate(system="Return JSON", user="status")

    kwargs = mock_client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == DEFAULT_GEMINI_MODEL
    assert kwargs["config"]["max_output_tokens"] == GEMINI_MIN_OUTPUT_TOKENS
    assert kwargs["config"]["response_mime_type"] == "application/json"


@pytest.mark.asyncio
async def test_gemini_explicit_json_mode_false_and_token_budget(
    mock_settings_with_gemini_key,
):
    """ADK / probes may pass explicit kwargs; those must not be overridden."""
    provider = GeminiProvider()
    mock_response = MagicMock()
    mock_response.text = "plain"
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(return_value=mock_response)
    provider._client = mock_client
    provider._genai_module = MagicMock()

    await provider._generate(
        system="s",
        user="u",
        json_mode=False,
        max_output_tokens=256,
    )

    kwargs = mock_client.models.generate_content.call_args.kwargs
    assert kwargs["config"]["max_output_tokens"] == 256
    assert "response_mime_type" not in kwargs["config"]


@pytest.mark.asyncio
async def test_gemini_provider_generate_with_images(mock_settings_with_gemini_key, tmp_path):
    """Test multimodal generation with images (mocked SDK)."""
    # Create a temporary test image
    test_image = tmp_path / "test.png"
    test_image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 100)  # Minimal PNG
    
    provider = GeminiProvider()
    provider.supports_images = True
    
    # Mock the google.genai module and client
    mock_response = MagicMock()
    mock_response.text = "I see an image"
    
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(return_value=mock_response)
    
    # Mock the types module for Part creation
    mock_part = MagicMock()
    mock_types = MagicMock()
    mock_types.Part.from_bytes = MagicMock(return_value=mock_part)
    
    # Directly set the client and module
    provider._client = mock_client
    provider._genai_module = MagicMock()

    with patch("google.genai.types.Part.from_bytes", return_value=mock_part) as from_bytes:
        result = await provider._generate(
            system="Analyze this image",
            user="What do you see?",
            images=[str(test_image)]
        )

        assert result == "I see an image"
        from_bytes.assert_called_once()


@pytest.mark.asyncio
async def test_gemini_provider_generate_handles_api_errors(mock_settings_with_gemini_key):
    """Test that API errors are properly caught and logged."""
    provider = GeminiProvider()
    
    # Mock the google.genai module to raise an error
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(
        side_effect=Exception("API rate limit exceeded")
    )
    
    # Directly set the client
    provider._client = mock_client
    provider._genai_module = MagicMock()
    
    with pytest.raises(Exception, match="API rate limit exceeded"):
        await provider._generate(
            system="Test",
            user="Test"
        )


@pytest.mark.asyncio
async def test_gemini_provider_generate_handles_empty_response(mock_settings_with_gemini_key):
    """Test that empty responses are handled gracefully."""
    provider = GeminiProvider()
    
    # Mock empty response
    mock_response = MagicMock()
    mock_response.text = ""
    
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(return_value=mock_response)
    
    # Directly set the client
    provider._client = mock_client
    provider._genai_module = MagicMock()
    
    with pytest.raises(RuntimeError, match="empty response"):
        await provider._generate(
            system="Test",
            user="Test"
        )


@pytest.mark.asyncio
async def test_gemini_provider_health_check_with_key(mock_settings_with_gemini_key):
    """Test health check reports healthy when key is configured."""
    provider = GeminiProvider()
    
    is_healthy = await provider.health_check()
    
    assert is_healthy is True
    assert provider.health.configured is True
    assert provider.health.config_error is None


@pytest.mark.asyncio
async def test_gemini_provider_health_check_without_key(mock_settings_no_key):
    """Test health check reports unhealthy when key is missing."""
    provider = GeminiProvider()
    
    is_healthy = await provider.health_check()
    
    assert is_healthy is False
    assert provider.health.configured is False
    assert provider.health.config_error == "GEMINI_API_KEY is not configured"


def test_gemini_provider_supports_liveness_probe_is_false():
    """Test that Gemini provider correctly declares no free liveness probe."""
    # This is a class attribute check - don't need a real instance
    assert GeminiProvider.supports_liveness_probe is False


def test_gemini_provider_fallback_api_key(monkeypatch):
    """Test that Gemini can fall back to GEMMA_API_KEY if GEMINI_API_KEY not set."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMMA_API_KEY", "fallback-key-67890")
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.api_key == "fallback-key-67890"
    
    get_settings.cache_clear()


def test_gemini_provider_prefers_gemini_api_key_over_gemma(monkeypatch):
    """Test that GEMINI_API_KEY takes precedence over GEMMA_API_KEY."""
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-specific-key")
    monkeypatch.setenv("GEMMA_API_KEY", "generic-key")
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.api_key == "gemini-specific-key"
    
    get_settings.cache_clear()


def test_gemini_provider_temperature_config(monkeypatch):
    """Test that temperature configuration is respected."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_TEMPERATURE", "0.7")
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.temperature == 0.7
    
    get_settings.cache_clear()


def test_gemini_provider_max_tokens_config(monkeypatch):
    """Test that max tokens configuration is respected."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_MAX_OUTPUT_TOKENS", "1024")
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.max_tokens == 1024
    
    get_settings.cache_clear()


def test_gemini_provider_timeout_config(monkeypatch):
    """Test that timeout configuration is respected."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_TIMEOUT_SECONDS", "120.0")
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.timeout == 120.0
    
    get_settings.cache_clear()


def test_gemini_provider_top_p_config(monkeypatch):
    """Test that top_p configuration is respected."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_TOP_P", "0.95")
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.top_p == 0.95
    
    get_settings.cache_clear()


def test_gemini_provider_stop_sequences_config(monkeypatch):
    """Test that stop sequences configuration is respected."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_STOP_SEQUENCES", "STOP,END,FINISH")
    get_settings.cache_clear()
    
    provider = GeminiProvider()
    assert provider.stop_sequences == ["STOP", "END", "FINISH"]
    
    get_settings.cache_clear()

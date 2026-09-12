"""Tests for Gemini provider factory integration.

These tests verify that the gemini provider can be selected through the
factory pattern and that it doesn't affect other providers.
"""

import pytest
from unittest.mock import patch, MagicMock

from app.config import get_settings
from app.gemma import get_gemma_provider, reset_gemma_provider
from app.gemma.gemini_provider import GeminiProvider
from app.gemma.mock_provider import MockGemmaProvider
from app.gemma.openai_compatible import OpenAICompatibleGemmaProvider


@pytest.fixture(autouse=True)
def reset_provider_singleton():
    """Reset provider singleton before and after each test."""
    reset_gemma_provider()
    get_settings.cache_clear()
    yield
    reset_gemma_provider()
    get_settings.cache_clear()


def test_factory_selects_gemini_provider(monkeypatch):
    """Test that factory creates GeminiProvider when GEMMA_PROVIDER=gemini."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-12345")
    
    provider = get_gemma_provider()
    
    assert isinstance(provider, GeminiProvider)
    assert provider.name == "gemini"


def test_factory_gemini_aliases(monkeypatch):
    """Test that gemini aliases are recognized."""
    test_cases = ["gemini", "google", "google_gemini", "google_ai"]
    
    for alias in test_cases:
        reset_gemma_provider()
        get_settings.cache_clear()
        
        monkeypatch.setenv("GEMMA_PROVIDER", alias)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        
        provider = get_gemma_provider()
        assert isinstance(provider, GeminiProvider), f"Alias '{alias}' did not create GeminiProvider"


def test_factory_gemini_requires_api_key(monkeypatch):
    """Test that factory raises error when gemini selected without API key."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMMA_API_KEY", raising=False)
    
    with pytest.raises(RuntimeError, match="GEMMA_PROVIDER=gemini requires GEMINI_API_KEY"):
        get_gemma_provider()


def test_factory_gemini_allows_custom_model(monkeypatch):
    """Test that factory respects custom model ID for Gemini."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-3.7-flash")
    
    provider = get_gemma_provider()
    
    assert isinstance(provider, GeminiProvider)
    assert provider.model == "gemini-3.7-flash"


def test_factory_gemini_defaults_model_when_not_specified(monkeypatch):
    """Test that factory uses default gemini-3.5-flash when model not specified."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL_ID", raising=False)
    monkeypatch.delenv("GEMMA_MODEL_ID", raising=False)
    
    provider = get_gemma_provider()
    
    assert isinstance(provider, GeminiProvider)
    assert provider.model == "gemini-3.5-flash"


def test_factory_mock_still_works(monkeypatch):
    """Test that mock provider still works (backward compatibility)."""
    monkeypatch.setenv("GEMMA_PROVIDER", "mock")
    
    provider = get_gemma_provider()
    
    assert isinstance(provider, MockGemmaProvider)
    assert provider.name == "mock"


def test_factory_openai_compatible_still_works(monkeypatch):
    """Test that openai_compatible provider still works (backward compatibility)."""
    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("GEMMA_MODEL_ID", "llama3")
    
    provider = get_gemma_provider()
    
    assert isinstance(provider, OpenAICompatibleGemmaProvider)
    assert provider.name == "openai_compatible"


def test_factory_unknown_provider_raises_error(monkeypatch):
    """Test that factory raises clear error for unknown provider."""
    monkeypatch.setenv("GEMMA_PROVIDER", "unknown_provider_xyz")
    
    with pytest.raises(RuntimeError, match="Unknown GEMMA_PROVIDER"):
        get_gemma_provider()


def test_factory_singleton_caching(monkeypatch):
    """Test that factory returns same instance on repeated calls."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    
    provider1 = get_gemma_provider()
    provider2 = get_gemma_provider()
    
    assert provider1 is provider2


def test_factory_force_new_creates_new_instance(monkeypatch):
    """Test that force_new=True creates new instance."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    
    provider1 = get_gemma_provider()
    provider2 = get_gemma_provider(force_new=True)
    
    assert provider1 is not provider2
    assert isinstance(provider1, GeminiProvider)
    assert isinstance(provider2, GeminiProvider)


def test_factory_reset_clears_singleton(monkeypatch):
    """Test that reset_gemma_provider clears cached instance."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    
    provider1 = get_gemma_provider()
    reset_gemma_provider()
    provider2 = get_gemma_provider()
    
    assert provider1 is not provider2


def test_factory_error_message_lists_gemini(monkeypatch):
    """Test that error message for unknown provider lists gemini as option."""
    monkeypatch.setenv("GEMMA_PROVIDER", "invalid")
    
    with pytest.raises(RuntimeError) as exc_info:
        get_gemma_provider()
    
    error_message = str(exc_info.value)
    assert "gemini" in error_message.lower()


def test_config_normalized_gemma_provider_maps_gemini(monkeypatch):
    """Test that Settings.normalized_gemma_provider correctly maps gemini aliases."""
    monkeypatch.setenv("GEMMA_PROVIDER", "google")
    get_settings.cache_clear()
    
    settings = get_settings()
    assert settings.normalized_gemma_provider == "gemini"


def test_config_effective_gemini_api_key_prefers_gemini_key(monkeypatch):
    """Test that effective_gemini_api_key prefers GEMINI_API_KEY."""
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-specific")
    monkeypatch.setenv("GEMMA_API_KEY", "generic")
    get_settings.cache_clear()
    
    settings = get_settings()
    assert settings.effective_gemini_api_key == "gemini-specific"


def test_config_effective_gemini_api_key_falls_back_to_gemma(monkeypatch):
    """Test that effective_gemini_api_key falls back to GEMMA_API_KEY."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMMA_API_KEY", "generic-key")
    get_settings.cache_clear()
    
    settings = get_settings()
    assert settings.effective_gemini_api_key == "generic-key"


def test_config_effective_gemini_model_id_prefers_gemini_model(monkeypatch):
    """Test that effective_gemini_model_id prefers GEMINI_MODEL_ID."""
    monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-3.7-flash")
    monkeypatch.setenv("GEMMA_MODEL_ID", "generic-model")
    get_settings.cache_clear()
    
    settings = get_settings()
    assert settings.effective_gemini_model_id == "gemini-3.7-flash"


def test_config_effective_gemini_model_id_falls_back_to_gemma(monkeypatch):
    """Test that effective_gemini_model_id falls back to GEMMA_MODEL_ID."""
    monkeypatch.delenv("GEMINI_MODEL_ID", raising=False)
    monkeypatch.setenv("GEMMA_MODEL_ID", "generic-model")
    get_settings.cache_clear()
    
    settings = get_settings()
    assert settings.effective_gemini_model_id == "generic-model"


def test_gemini_provider_uses_shared_temperature_config(monkeypatch):
    """Test that Gemini provider uses shared GEMMA_TEMPERATURE."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_TEMPERATURE", "0.5")
    
    provider = get_gemma_provider()
    assert provider.temperature == 0.5


def test_gemini_provider_uses_shared_max_tokens_config(monkeypatch):
    """Test that Gemini provider uses shared GEMMA_MAX_OUTPUT_TOKENS."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_MAX_OUTPUT_TOKENS", "2048")
    
    provider = get_gemma_provider()
    assert provider.max_tokens == 2048


def test_gemini_provider_uses_shared_timeout_config(monkeypatch):
    """Test that Gemini provider uses shared GEMMA_TIMEOUT_SECONDS."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_TIMEOUT_SECONDS", "90.0")
    
    provider = get_gemma_provider()
    assert provider.timeout == 90.0


def test_gemini_provider_uses_shared_multimodal_config(monkeypatch):
    """Test that Gemini provider uses shared GEMMA_SUPPORTS_IMAGES."""
    monkeypatch.setenv("GEMMA_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMMA_SUPPORTS_IMAGES", "true")
    
    provider = get_gemma_provider()
    assert provider.supports_images is True

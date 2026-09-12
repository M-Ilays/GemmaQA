"""UI provider catalog and runtime switch — no secrets, no live LLM probes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import (  # noqa: E402
    CANONICAL_GEMMA_PROVIDERS,
    get_settings,
    get_runtime_provider_override,
    runtime_provider_file,
)
from app.gemma import get_gemma_provider, reset_gemma_provider  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.gemma.openai_compatible import OpenAICompatibleGemmaProvider  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "mock")
    get_settings.cache_clear()
    reset_gemma_provider()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_list_providers_includes_catalog_without_secrets(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-should-not-leak")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-secret-should-not-leak")
    monkeypatch.setenv("GEMMA_API_KEY", "gemma-secret-should-not-leak")
    get_settings.cache_clear()

    async with client:
        res = await client.get("/api/config/providers")
    assert res.status_code == 200
    body = res.json()
    ids = {item["id"] for item in body["providers"]}
    assert ids == set(CANONICAL_GEMMA_PROVIDERS)
    assert body["active"] == "mock"
    assert body["applies_to"] == "new_runs"
    dumped = json.dumps(body)
    assert "secret-should-not-leak" not in dumped
    assert "bedrock-secret-should-not-leak" not in dumped
    assert "gemma-secret-should-not-leak" not in dumped
    mock = next(item for item in body["providers"] if item["id"] == "mock")
    assert mock["configured"] is True
    assert mock["selectable"] is True


@pytest.mark.asyncio
async def test_switch_to_mock(client: AsyncClient):
    async with client:
        res = await client.post("/api/config/provider", json={"provider": "mock"})
    assert res.status_code == 200
    assert res.json()["active"] == "mock"
    assert get_runtime_provider_override() == "mock"
    assert runtime_provider_file().is_file()
    assert get_gemma_provider(force_new=True).__class__ is MockGemmaProvider


@pytest.mark.asyncio
async def test_switch_unconfigured_gemini_rejected(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "app.config.Settings.effective_gemini_api_key",
        property(lambda self: ""),
    )
    get_settings.cache_clear()
    async with client:
        res = await client.post("/api/config/provider", json={"provider": "gemini"})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "not configured" in detail.lower()
    assert "GEMINI_API_KEY" in detail
    assert get_settings().normalized_gemma_provider == "mock"


@pytest.mark.asyncio
async def test_switch_unknown_provider_rejected(client: AsyncClient):
    async with client:
        res = await client.post("/api/config/provider", json={"provider": "not-real"})
    assert res.status_code == 400
    assert "Unknown provider" in res.json()["detail"]


@pytest.mark.asyncio
async def test_switch_openai_compatible_when_configured(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GEMMA_API_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("GEMMA_MODEL_ID", "gemma3:4b")
    get_settings.cache_clear()
    async with client:
        res = await client.post(
            "/api/config/provider", json={"provider": "openai_compatible"}
        )
    assert res.status_code == 200
    body = res.json()
    assert body["active"] == "openai_compatible"
    assert get_settings().env_gemma_provider == "mock"
    provider = get_gemma_provider(force_new=True)
    assert isinstance(provider, OpenAICompatibleGemmaProvider)


@pytest.mark.asyncio
async def test_switch_accepts_alias(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-a-real-secret")
    get_settings.cache_clear()
    async with client:
        res = await client.post("/api/config/provider", json={"provider": "google"})
    assert res.status_code == 200
    assert res.json()["active"] == "gemini"


def test_env_provider_unchanged_until_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("GEMMA_MODEL_ID", "gemma3:4b")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.env_gemma_provider == "openai_compatible"
    assert settings.normalized_gemma_provider == "openai_compatible"
    assert get_runtime_provider_override() is None


def test_gemini_display_names():
    from app.runtime_info import provider_display_name, run_mode_label

    assert provider_display_name("gemini") == "Google Gemini"
    assert run_mode_label("gemini") == "Cloud AI (Google Gemini)"

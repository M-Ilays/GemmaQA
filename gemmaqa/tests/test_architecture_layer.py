"""Architecture layer tests: providers, adapters, health, no silent mock fallback."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.browser.adapters import create_browser_adapter  # noqa: E402
from app.browser.adapters.direct import DirectPlaywrightAdapter  # noqa: E402
from app.browser.adapters.mcp import FakeMcpTransport, PlaywrightMCPAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.gemma import get_gemma_provider, reset_gemma_provider  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.gemma.openai_compatible import OpenAICompatibleGemmaProvider  # noqa: E402
from app.main import app  # noqa: E402
from app.runtime_info import build_runtime_info, provider_display_name  # noqa: E402
from app.safety.policies import SafetyPolicy  # noqa: E402
from app.safety.validator import ActionValidator  # noqa: E402
from app.schemas import ActionType, BrowserAction, RiskLevel  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "mock")
    monkeypatch.setenv("BROWSER_ADAPTER", "direct_playwright")
    reset_gemma_provider()
    get_settings.cache_clear()
    yield
    reset_gemma_provider()
    get_settings.cache_clear()


def test_factory_mock():
    p = get_gemma_provider(force_new=True)
    assert isinstance(p, MockGemmaProvider)
    assert provider_display_name() == "Mock"


def test_no_silent_fallback_unknown_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "not-a-real-provider")
    get_settings.cache_clear()
    reset_gemma_provider()
    with pytest.raises(RuntimeError, match="Unknown GEMMA_PROVIDER"):
        get_gemma_provider(force_new=True)


def test_no_silent_fallback_openai_missing_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "")
    monkeypatch.setenv("GEMMA_API_URL", "")
    monkeypatch.setenv("GEMMA_MODEL_ID", "")
    monkeypatch.setenv("GEMMA_MODEL_NAME", "")
    get_settings.cache_clear()
    reset_gemma_provider()
    with pytest.raises(RuntimeError, match="GEMMA_API_BASE_URL|GEMMA_MODEL_ID"):
        get_gemma_provider(force_new=True)


def test_openai_compatible_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("GEMMA_MODEL_ID", "gemma3:4b")
    get_settings.cache_clear()
    reset_gemma_provider()
    p = get_gemma_provider(force_new=True)
    assert isinstance(p, OpenAICompatibleGemmaProvider)
    assert "Ollama" in provider_display_name()


def test_create_direct_adapter():
    adapter = create_browser_adapter("run-test")
    assert isinstance(adapter, DirectPlaywrightAdapter)
    assert adapter.name == "direct_playwright"


def test_create_mcp_adapter():
    adapter = create_browser_adapter("run-test", adapter_name="playwright_mcp")
    assert isinstance(adapter, PlaywrightMCPAdapter)


def test_unknown_browser_adapter(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BROWSER_ADAPTER", "something_else")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="Unknown BROWSER_ADAPTER"):
        create_browser_adapter("run-test")


@pytest.mark.asyncio
async def test_mcp_adapter_with_fake_transport():
    fake = FakeMcpTransport()
    adapter = PlaywrightMCPAdapter("run-mcp", transport=fake)
    await adapter.start_session()
    await adapter.navigate("https://example.com/")
    assert await adapter.get_current_url() == "https://example.com/"
    snap = await adapter.get_accessibility_snapshot()
    assert snap
    health = await adapter.health_check()
    assert health["available"] is True
    await adapter.close_session()
    assert ("browser_navigate", {"url": "https://example.com/"}) in [
        (n, a) for n, a in fake.calls
    ]


def test_validator_policy_rule_on_block():
    policy = SafetyPolicy(
        authorized_url="https://example.com",
        authorized_domain="example.com",
        safe_mode=True,
    )
    validator = ActionValidator(policy)
    action = BrowserAction(
        action=ActionType.CLICK,
        element_id="el_1",
        reason="Click delete forever",
        risk=RiskLevel.LOW,
    )
    result = validator.validate(action)
    assert result.allowed is False
    assert result.policy_rule
    assert result.risk in {"forbidden", "destructive", "read_only", "safe_write"}


def test_runtime_info_mock():
    info = build_runtime_info(decisions_validated=3, decisions_rejected=1)
    assert info["is_mock_provider"] is True
    assert info["provider"] == "Mock"
    assert info["model"] == "None"
    assert info["decisions_validated"] == 3
    assert info["decisions_rejected"] == 1
    assert "api_key" not in str(info).lower() or info.get("api_base_url") is None


@pytest.mark.asyncio
async def test_health_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        g = await client.get("/api/health/gemma")
        assert g.status_code == 200
        body = g.json()
        assert body["is_mock"] is True
        assert "api_key" not in body
        rt = await client.get("/api/config/runtime")
        assert rt.status_code == 200
        assert rt.json()["provider_type"] == "mock"
        assert "api_key" not in rt.json()

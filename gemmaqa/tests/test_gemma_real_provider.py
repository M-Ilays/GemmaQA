"""Tests for configurable Gemma providers (no real model load)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from app.gemma import get_gemma_provider, reset_gemma_provider  # noqa: E402
from app.gemma.context_sanitizer import sanitize_page_state_for_model  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.gemma.openai_compatible import OpenAICompatibleGemmaProvider  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import InteractiveElement, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402
from app.utils.sanitization import MASK  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_provider(monkeypatch: pytest.MonkeyPatch):
    reset_gemma_provider()
    get_settings.cache_clear()
    yield
    reset_gemma_provider()
    get_settings.cache_clear()


def test_factory_defaults_to_mock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "mock")
    get_settings.cache_clear()
    reset_gemma_provider()
    p = get_gemma_provider(force_new=True)
    assert isinstance(p, MockGemmaProvider)


def test_factory_openai_compatible_no_import_crash(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("GEMMA_MODEL_ID", "test-model-id")
    get_settings.cache_clear()
    reset_gemma_provider()
    p = get_gemma_provider(force_new=True)
    assert isinstance(p, OpenAICompatibleGemmaProvider)
    assert p.health.configured is True


@pytest.mark.asyncio
async def test_openai_compatible_missing_config_message(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "")
    monkeypatch.setenv("GEMMA_API_URL", "")
    monkeypatch.setenv("GEMMA_MODEL_ID", "")
    get_settings.cache_clear()
    p = OpenAICompatibleGemmaProvider()
    assert p.health.configured is False
    with pytest.raises(RuntimeError, match="GEMMA_API_BASE_URL"):
        await p._generate("sys", "user")


def test_sanitize_page_state_strips_password_and_hidden():
    page = PageState(
        page_id=new_id(),
        url="https://example.com/login?token=abc",
        title="Login",
        interactive_elements=[
            InteractiveElement(
                element_id="el_pw",
                tag="input",
                input_type="password",
                current_value="super-secret",
                accessible_name="Password",
            ),
            InteractiveElement(
                element_id="el_hid",
                tag="input",
                input_type="hidden",
                current_value="csrf-token-value",
                name="csrf",
            ),
            InteractiveElement(
                element_id="el_ok",
                tag="button",
                text="Continue",
                accessible_name="Continue",
            ),
        ],
    )
    safe = sanitize_page_state_for_model(page)
    blob = str(safe)
    assert "super-secret" not in blob
    assert "csrf-token-value" not in blob
    assert "abc" not in safe["url"] or MASK in safe["url"]
    pw = next(e for e in safe["interactive_elements"] if e["element_id"] == "el_pw")
    assert pw.get("current_value") in {MASK, None} or pw.get("sensitive") is True


@pytest.mark.asyncio
async def test_ai_health_endpoint_mock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "mock")
    get_settings.cache_clear()
    reset_gemma_provider()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/ai/health")
    assert res.status_code == 200
    data = res.json()
    assert data["provider_type"] == "mock"
    assert data["configured"] is True
    assert "api_key" not in str(data).lower() or data.get("api_key") is None
    assert "last_error_summary" in data
    assert "multimodal_support" in data


@pytest.mark.asyncio
async def test_openai_compatible_does_not_retry_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("GEMMA_MODEL_ID", "gemma3:4b")
    get_settings.cache_clear()
    p = OpenAICompatibleGemmaProvider()
    calls = {"n": 0}

    async def fail_timeout(*_a, **_k):
        calls["n"] += 1
        raise TimeoutError("Gemma endpoint timeout after 60.0s")

    p._call_once = fail_timeout  # type: ignore[method-assign]
    with pytest.raises(TimeoutError, match="timeout"):
        await p._generate("sys", "user")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_openai_compatible_retries_503(monkeypatch: pytest.MonkeyPatch):
    import httpx

    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("GEMMA_MODEL_ID", "gemma3:4b")
    get_settings.cache_clear()
    p = OpenAICompatibleGemmaProvider()
    calls = {"n": 0}

    async def fail_then_ok(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            req = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
            resp = httpx.Response(503, request=req)
            raise httpx.HTTPStatusError("503", request=req, response=resp)
        return '{"ok": true}'

    p._call_once = fail_then_ok  # type: ignore[method-assign]
    text = await p._generate("sys", "user")
    assert text == '{"ok": true}'
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_openai_compatible_retries_429(monkeypatch: pytest.MonkeyPatch):
    import httpx

    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("GEMMA_MODEL_ID", "gemma3:4b")
    get_settings.cache_clear()
    p = OpenAICompatibleGemmaProvider()
    calls = {"n": 0}

    async def fail_then_ok(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            req = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
            resp = httpx.Response(429, request=req)
            raise httpx.HTTPStatusError("429", request=req, response=resp)
        return '{"ok": true}'

    p._call_once = fail_then_ok  # type: ignore[method-assign]
    text = await p._generate("sys", "user")
    assert text == '{"ok": true}'
    assert calls["n"] == 2


def test_timeout_error_is_not_transient(monkeypatch: pytest.MonkeyPatch):
    import httpx

    monkeypatch.setenv("GEMMA_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GEMMA_API_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("GEMMA_MODEL_ID", "gemma3:4b")
    get_settings.cache_clear()
    p = OpenAICompatibleGemmaProvider()
    assert p._is_transient(TimeoutError("Gemma endpoint timeout after 60.0s")) is False
    assert p._is_transient(httpx.TimeoutException("timed out")) is False


@pytest.mark.asyncio
async def test_provider_exhaustion_returns_finish(monkeypatch: pytest.MonkeyPatch):
    from app.gemma.base import ActionGenerationRequest
    from app.schemas import ActionType

    p = MockGemmaProvider(raise_timeout=True)
    p.max_provider_failures = 2
    page = PageState(page_id=new_id(), url="https://example.com", title="Home")
    req = ActionGenerationRequest(page_state=page, remaining_action_budget=5)

    a1 = await p.generate_action(req)
    assert (a1.metadata or {}).get("ai_failure") or a1.action == ActionType.FINISH
    a2 = await p.generate_action(req)
    assert a2.action == ActionType.FINISH
    assert (a2.metadata or {}).get("provider_exhausted") or "repeatedly" in (a2.reason or "").lower()

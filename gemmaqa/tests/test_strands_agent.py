"""Strands coordinator tests — no live AWS or browser."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import Settings, get_settings  # noqa: E402
from app.schemas import CreateRunRequest, RunConfiguration  # noqa: E402
from app.strands_agent.context import active_run_request, last_launched_run_id  # noqa: E402
from app.strands_agent.operations import dumps, get_activity_log, launch_qa_run  # noqa: E402
from app.strands_agent.service import strands_status  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_effective_strands_model_id_prefers_strands_then_bedrock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRANDS_MODEL_ID", "anthropic.claude-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-other")
    settings = Settings()
    assert settings.effective_strands_model_id == "anthropic.claude-test"


def test_effective_strands_model_id_falls_back_to_bedrock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("STRANDS_MODEL_ID", raising=False)
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-other")
    settings = Settings()
    assert settings.effective_strands_model_id == "anthropic.claude-other"


def test_strands_status_never_includes_secrets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("USE_STRANDS_ORCHESTRATION", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "should-never-appear")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-other")
    get_settings.cache_clear()
    dumped = str(strands_status())
    assert "should-never-appear" not in dumped
    assert strands_status()["enabled"] is True
    assert strands_status()["configured"] is True
    assert strands_status()["ready"] is True
    assert strands_status()["aws_region"] == "us-east-1"


def test_dumps_is_json():
    assert '"ok": true' in dumps({"ok": True}) or '"ok": True' in dumps({"ok": True})
    assert dumps({"ok": True}).startswith("{")


@pytest.mark.asyncio
async def test_launch_qa_run_requires_in_scope_request():
    token = active_run_request.set(None)
    try:
        result = await launch_qa_run()
        assert result["ok"] is False
        assert "scope" in result["error"]
    finally:
        active_run_request.reset(token)


@pytest.mark.asyncio
async def test_launch_qa_run_uses_run_manager(monkeypatch: pytest.MonkeyPatch):
    request = CreateRunRequest(
        url="https://example.com",
        authorization_ack=True,
        configuration=RunConfiguration(),
    )
    fake_run = MagicMock()
    fake_run.id = "run-1"
    fake_run.status = "initializing"
    fake_run.url = request.url
    fake_run.message = "Starting"
    fake_run.pages_visited = 0
    fake_run.actions_taken = 0
    fake_run.bugs_found = 0
    fake_run.error = None

    monkeypatch.setattr(
        "app.strands_agent.operations.run_manager.create_run",
        AsyncMock(return_value="run-1"),
    )
    monkeypatch.setattr(
        "app.strands_agent.operations.run_manager.start_run",
        AsyncMock(return_value=fake_run),
    )

    token = active_run_request.set(request)
    launched = last_launched_run_id.set(None)
    try:
        result = await launch_qa_run()
        assert result["ok"] is True
        assert result["run_id"] == "run-1"
        assert last_launched_run_id.get() == "run-1"
    finally:
        active_run_request.reset(token)
        last_launched_run_id.reset(launched)


@pytest.mark.asyncio
async def test_activity_log_tool_sanitizes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.strands_agent.operations.ActivityLog.read",
        classmethod(lambda cls, run_id, **kwargs: [{"seq": 1, "password": "secret", "text": "ok"}]),
    )
    result = await get_activity_log("run-1")
    assert result["ok"] is True
    blob = str(result)
    assert "secret" not in blob


@pytest.mark.asyncio
async def test_run_with_strands_agent_requires_flag(monkeypatch: pytest.MonkeyPatch):
    from app.strands_agent.service import run_with_strands_agent

    monkeypatch.setenv("USE_STRANDS_ORCHESTRATION", "false")
    get_settings.cache_clear()
    req = CreateRunRequest(url="https://example.com", authorization_ack=True)
    with pytest.raises(RuntimeError, match="USE_STRANDS_ORCHESTRATION"):
        await run_with_strands_agent(req)


def test_strands_tools_import_without_calling_aws():
    from app.strands_agent.tools import launch_authorized_qa_run

    assert launch_authorized_qa_run.tool_name == "launch_authorized_qa_run"


@pytest.mark.asyncio
async def test_run_with_strands_agent_invokes_sdk(monkeypatch: pytest.MonkeyPatch):
    from app.strands_agent.service import run_with_strands_agent

    monkeypatch.setenv("USE_STRANDS_ORCHESTRATION", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-test")
    get_settings.cache_clear()

    async def fake_invoke(_prompt: str):
        last_launched_run_id.set("run-strands")
        return MagicMock(message="launched run-strands")

    fake_agent = MagicMock()
    fake_agent.invoke_async = fake_invoke

    monkeypatch.setattr(
        "app.strands_agent.qa_agent.build_qa_agent",
        lambda **kwargs: fake_agent,
    )

    req = CreateRunRequest(
        url="https://example.com",
        password="never-in-prompt",
        authorization_ack=True,
        configuration=RunConfiguration(testing_objective="Signup and add a contact"),
    )
    result = await run_with_strands_agent(req, require_enabled_flag=True)
    assert result["run_id"] == "run-strands"
    assert result["agent_framework"] == "strands-agents"
    assert "never-in-prompt" not in result["agent_reply"]


@pytest.mark.asyncio
async def test_strands_route_uses_coordinator(monkeypatch: pytest.MonkeyPatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async def fake_run(_payload, *, require_enabled_flag=False):
        return {
            "run_id": "run-api-strands",
            "message": "Strands Agent launched the QA run",
        }

    monkeypatch.setattr(
        "app.strands_agent.service.run_with_strands_agent",
        fake_run,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/runs/strands",
            json={
                "url": "https://example.com",
                "authorization_ack": True,
                "auto_start": True,
            },
        )
    assert res.status_code == 201
    body = res.json()
    assert body["run_id"] == "run-api-strands"
    assert "password" not in body


@pytest.mark.asyncio
async def test_health_includes_strands_without_secrets(monkeypatch: pytest.MonkeyPatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token-must-not-leak")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert "strands" in body
    assert body["strands"]["sdk"] == "strands-agents"
    assert "token-must-not-leak" not in str(body)

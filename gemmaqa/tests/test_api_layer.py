"""API and WebSocket layer tests."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.events import event_store  # noqa: E402
from app.agent.run_manager import run_manager  # noqa: E402
from app.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, QARun  # noqa: E402
from app.schemas import RunConfiguration  # noqa: E402
from app.utils.sanitization import MASK, sanitize_dict  # noqa: E402


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[AsyncClient, None]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr("app.database.AsyncSessionLocal", session_factory)
    monkeypatch.setattr("app.agent.run_manager.AsyncSessionLocal", session_factory)

    # Expose for tests that need direct DB writes
    app.state.session_factory = session_factory  # type: ignore[attr-defined]

    run_manager._tasks.clear()
    run_manager._prepared.clear()
    run_manager._start_locks.clear()
    from app.agent.controller import ACTIVE_RUNS

    ACTIVE_RUNS.clear()
    event_store._buffers.clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    await engine.dispose()
    db_path.unlink(missing_ok=True)


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "url": "https://example.com",
        "username": "admin@example.com",
        "password": "super-secret-password",
        "notes": "api test",
        "auto_start": False,
        "authorization_ack": True,
        "configuration": RunConfiguration(headless=True).model_dump(),
    }
    body.update(overrides)
    return body


async def _await_status(client: AsyncClient, run_id: str, wanted: str, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        last = (await client.get(f"/api/runs/{run_id}")).json()
        if last.get("status") == wanted:
            return last
        # Also wait for background task if tracked
        task = run_manager._tasks.get(run_id)
        if task and task.done() and last.get("status") != wanted:
            await asyncio.sleep(0.05)
            last = (await client.get(f"/api/runs/{run_id}")).json()
            return last
        await asyncio.sleep(0.05)
    return last


@pytest.mark.asyncio
async def test_create_run(client: AsyncClient) -> None:
    res = await client.post("/api/runs", json=_create_body())
    assert res.status_code == 201
    data = res.json()
    assert data["run_id"]
    assert data["status"] == "created"

    detail = await client.get(f"/api/runs/{data['run_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["url"].startswith("https://example.com")
    assert body["username"] == "admin@example.com"
    assert "password" not in body
    assert body["configuration"].get("max_actions") is None
    assert body["configuration"].get("max_pages") is None


@pytest.mark.asyncio
async def test_invalid_url(client: AsyncClient) -> None:
    res = await client.post("/api/runs", json=_create_body(url="ftp://bad.example"))
    assert res.status_code == 400
    detail = res.json()["detail"].lower()
    assert "ftp" in detail or "http" in detail or "scheme" in detail


@pytest.mark.asyncio
async def test_start_run(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]
    sf = app.state.session_factory  # type: ignore[attr-defined]

    async def fake_run(self: Any) -> None:
        await self.emit("run_started", {"url": self.request.url})
        async with sf() as session:
            await session.execute(
                update(QARun)
                .where(QARun.id == self.run_id)
                .values(status="completed", message="done", progress_pct=100.0)
            )
            await session.commit()
        await self.emit("run_completed", {"ok": True})

    with patch("app.agent.run_manager.AgentController.run", new=fake_run):
        res = await client.post(f"/api/runs/{run_id}/start")
        assert res.status_code == 200
        st = await _await_status(client, run_id, "completed")
        assert st["status"] == "completed"


@pytest.mark.asyncio
async def test_duplicate_start(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(self: Any) -> None:
        started.set()
        await release.wait()

    with patch("app.agent.run_manager.AgentController.run", new=slow_run):
        first = await client.post(f"/api/runs/{run_id}/start")
        assert first.status_code == 200
        await asyncio.wait_for(started.wait(), timeout=2)
        second = await client.post(f"/api/runs/{run_id}/start")
        assert second.status_code == 409
        release.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_cancel_run(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]
    res = await client.post(f"/api/runs/{run_id}/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_end_run_aliases_cancel(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]
    res = await client.post(f"/api/runs/{run_id}/end")
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_pause_resume_and_end_active_run(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(self: Any) -> None:
        started.set()
        await release.wait()

    with patch("app.agent.run_manager.AgentController.run", new=slow_run):
        start = await client.post(f"/api/runs/{run_id}/start")
        assert start.status_code == 200
        await asyncio.wait_for(started.wait(), timeout=2)

        not_started = await client.post("/api/runs/does-not-exist/pause")
        assert not_started.status_code == 404

        paused = await client.post(f"/api/runs/{run_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"

        again = await client.post(f"/api/runs/{run_id}/pause")
        assert again.status_code == 200
        assert again.json()["status"] == "paused"

        resumed = await client.post(f"/api/runs/{run_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] != "paused"

        ended = await client.post(f"/api/runs/{run_id}/end")
        assert ended.status_code == 200
        release.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_pause_before_start_is_rejected(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]
    res = await client.post(f"/api/runs/{run_id}/pause")
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_unknown_run(client: AsyncClient) -> None:
    assert (await client.get("/api/runs/does-not-exist")).status_code == 404
    assert (await client.post("/api/runs/does-not-exist/start")).status_code == 404


@pytest.mark.asyncio
async def test_websocket_event_sequence(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]

    e1 = await event_store.append(run_id, "action_planned", {"action": "click"})
    e2 = await event_store.append(run_id, "page_observed", {"url": "https://example.com"})
    assert e2.sequence_number == e1.sequence_number + 1

    body = (await client.get(f"/api/runs/{run_id}/events")).json()
    assert len(body) >= 2
    seqs = [e["sequence_number"] for e in body]
    assert seqs == sorted(seqs)
    assert body[-1]["event_type"] == "page_observed"

    from starlette.testclient import TestClient

    with TestClient(app) as tc:
        with tc.websocket_connect(f"/ws/runs/{run_id}") as ws:
            assert ws.receive_json()["type"] == "subscribed"
            seen = []
            for _ in range(30):
                frame = ws.receive_json()
                seen.append(frame["type"])
                if frame["type"] == "history_complete":
                    break
            assert "event" in seen
            assert "history_complete" in seen


@pytest.mark.asyncio
async def test_secret_redaction(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]
    await event_store.append(
        run_id,
        "auth_debug",
        {"password": "hunter2", "token": "abc", "ok": "visible", "authorization": "Bearer x"},
    )
    events = (await client.get(f"/api/runs/{run_id}/events")).json()
    payload = [e for e in events if e["event_type"] == "auth_debug"][-1]["payload"]
    assert payload["password"] == MASK
    assert payload["token"] == MASK
    assert payload["authorization"] == MASK
    assert payload["ok"] == "visible"
    assert sanitize_dict({"cookie": "sid=1"})["cookie"] == MASK


@pytest.mark.asyncio
async def test_completed_run(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]
    sf = app.state.session_factory  # type: ignore[attr-defined]

    async def complete_run(self: Any) -> None:
        await self.emit("run_started", {"url": self.request.url})
        async with sf() as session:
            await session.execute(
                update(QARun)
                .where(QARun.id == self.run_id)
                .values(status="completed", message="Completed", progress_pct=100.0)
            )
            await session.commit()
        await self.emit("run_completed", {"pages": 1})

    with patch("app.agent.run_manager.AgentController.run", new=complete_run):
        await client.post(f"/api/runs/{run_id}/start")
        st = await _await_status(client, run_id, "completed")
        assert st["status"] == "completed"
        assert st["progress_pct"] == 100.0


@pytest.mark.asyncio
async def test_failed_run(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]

    async def boom(self: Any) -> None:
        raise RuntimeError("simulated failure with password=secret")

    with patch("app.agent.run_manager.AgentController.run", new=boom):
        await client.post(f"/api/runs/{run_id}/start")
        task = run_manager._tasks.get(run_id)
        if task:
            await task  # crash is swallowed by RunManager; status marked failed
        st = await _await_status(client, run_id, "failed")
        assert st["status"] == "failed"
        assert st["error"]
        assert "Traceback" not in (st["error"] or "")


@pytest.mark.asyncio
async def test_delete_requires_confirm(client: AsyncClient) -> None:
    created = await client.post("/api/runs", json=_create_body())
    run_id = created.json()["run_id"]
    assert (await client.delete(f"/api/runs/{run_id}")).status_code == 400
    assert (await client.delete(f"/api/runs/{run_id}?confirm=true")).status_code == 200
    assert (await client.get(f"/api/runs/{run_id}")).status_code == 404


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    res = await client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


# ===========================================================================
# Activity log endpoint — the operator's live window into a run
#
# The WebSocket timeline lived in browser memory, capped at 250 events, and was
# lost on refresh, so an operator watching a run could not tell what it was
# doing. This endpoint is what makes the history durable and re-readable.
# ===========================================================================


async def test_activity_endpoint_returns_the_runs_recorded_steps(client: AsyncClient) -> None:
    from app.reporting.activity_log import ActivityLog

    created = (await client.post("/api/runs", json=_create_body())).json()
    run_id = created["run_id"]

    log = ActivityLog(run_id)
    log.record("page_observed", {"url": "https://example.com", "elements": 4})
    log.record("action_planned", {"action": "click", "element_id": "el_002", "reason": "Explore"})

    res = await client.get(f"/api/runs/{run_id}/activity")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 2
    assert body["last_seq"] == 2
    assert body["records"][0]["summary"] == "Observed https://example.com (4 interactive elements)"
    assert body["records"][1]["phase"] == "planning"


async def test_activity_endpoint_supports_incremental_polling(client: AsyncClient) -> None:
    """`since_seq` is what lets the live view top up instead of refetching the
    whole log every few seconds."""
    from app.reporting.activity_log import ActivityLog

    created = (await client.post("/api/runs", json=_create_body())).json()
    run_id = created["run_id"]
    log = ActivityLog(run_id)
    for i in range(4):
        log.record("state_changed", {"state": f"s{i}"})

    body = (await client.get(f"/api/runs/{run_id}/activity?since_seq=2")).json()
    assert [r["seq"] for r in body["records"]] == [3, 4]


async def test_activity_endpoint_reports_when_more_remains(client: AsyncClient) -> None:
    """A short page must not be mistaken for the end of the log."""
    from app.reporting.activity_log import ActivityLog

    created = (await client.post("/api/runs", json=_create_body())).json()
    run_id = created["run_id"]
    log = ActivityLog(run_id)
    for _ in range(6):
        log.record("state_changed", {})

    body = (await client.get(f"/api/runs/{run_id}/activity?limit=3")).json()
    assert body["more_available"] is True
    assert body["last_seq"] == 3


async def test_activity_endpoint_404s_for_an_unknown_run(client: AsyncClient) -> None:
    res = await client.get("/api/runs/not-a-run/activity")
    assert res.status_code == 404


async def test_activity_endpoint_is_empty_before_anything_is_recorded(client: AsyncClient) -> None:
    """An empty log is a valid answer for a run that has not started, not a 404."""
    created = (await client.post("/api/runs", json=_create_body())).json()
    res = await client.get(f"/api/runs/{created['run_id']}/activity")
    assert res.status_code == 200
    assert res.json() == {
        "run_id": created["run_id"],
        "records": [],
        "count": 0,
        "last_seq": 0,
        "more_available": False,
    }


async def test_activity_endpoint_never_returns_credentials(client: AsyncClient) -> None:
    from app.reporting.activity_log import ActivityLog

    created = (await client.post("/api/runs", json=_create_body())).json()
    run_id = created["run_id"]
    ActivityLog(run_id).record("action_planned", {"password": "super-secret-password"})

    assert "super-secret-password" not in (await client.get(f"/api/runs/{run_id}/activity")).text

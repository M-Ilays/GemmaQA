"""WebSocket connection manager with queues, history replay, and heartbeats."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.agent.events import RECENT_WINDOW, event_store
from app.database import AsyncSessionLocal
from app.models import EventRecord
from app.schemas import RunEvent, WSMessage
from app.utils.logging import get_logger
from app.utils.sanitization import sanitize_dict

logger = get_logger("api.ws")

router = APIRouter(tags=["websocket"])

HEARTBEAT_INTERVAL_SEC = 20


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ClientConnection:
    """Per-client outbound queue so agent emit never blocks on slow sockets."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=500)
        self.alive = True

    async def send_loop(self) -> None:
        try:
            while self.alive:
                try:
                    message = await asyncio.wait_for(self.queue.get(), timeout=HEARTBEAT_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    # Heartbeat when idle
                    await self._safe_send(
                        WSMessage(
                            type="heartbeat",
                            run_id="",
                            timestamp=_utc_now(),
                            data={"ok": True},
                        ).model_dump(mode="json")
                    )
                    continue
                if message is None:
                    break
                await self._safe_send(message)
        except Exception:
            self.alive = False

    async def _safe_send(self, message: dict[str, Any]) -> None:
        try:
            await self.websocket.send_json(message)
        except Exception:
            self.alive = False
            raise

    def enqueue(self, message: dict[str, Any]) -> None:
        if not self.alive:
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            # Drop oldest to keep stream moving
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Dropping WS message — client queue saturated")


class ConnectionManager:
    """Fan-out events to subscribers of a run_id via asyncio-safe queues."""

    def __init__(self) -> None:
        self._connections: dict[str, set[ClientConnection]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, run_id: str, websocket: WebSocket) -> ClientConnection:
        await websocket.accept()
        client = ClientConnection(websocket)
        async with self._lock:
            self._connections.setdefault(run_id, set()).add(client)
        logger.info("WS connected run=%s", run_id)
        return client

    async def disconnect(self, run_id: str, client: ClientConnection) -> None:
        client.alive = False
        try:
            client.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        async with self._lock:
            conns = self._connections.get(run_id)
            if conns and client in conns:
                conns.remove(client)
            if conns is not None and not conns:
                self._connections.pop(run_id, None)
        logger.info("WS disconnected run=%s", run_id)

    async def drop_run(self, run_id: str) -> None:
        async with self._lock:
            clients = list(self._connections.pop(run_id, set()))
        for client in clients:
            client.alive = False
            try:
                client.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def publish(self, run_id: str, event: RunEvent) -> None:
        """Non-blocking publish of a sequenced RunEvent to all subscribers."""
        message = WSMessage(
            type="event",
            run_id=run_id,
            timestamp=event.timestamp,
            event=event,
            data={
                # Legacy fields for older frontend hooks
                "event": event.event_type,
                "type": event.event_type,
                "run_id": run_id,
                "timestamp": event.timestamp.isoformat(),
                "payload": event.payload,
                "sequence_number": event.sequence_number,
                "event_id": event.event_id,
            },
        ).model_dump(mode="json")

        async with self._lock:
            targets = list(self._connections.get(run_id, set()))

        dead: list[ClientConnection] = []
        for client in targets:
            if not client.alive:
                dead.append(client)
                continue
            try:
                client.enqueue(message)
            except Exception:
                dead.append(client)
        for client in dead:
            await self.disconnect(run_id, client)

    async def broadcast(self, run_id: str, event: str, data: dict[str, Any] | None = None) -> None:
        """Legacy broadcast helper used by older call sites."""
        payload = data or {}
        if isinstance(payload.get("payload"), dict):
            inner = payload["payload"]
        else:
            inner = payload
        from app.agent.events import event_store as store

        run_event = await store.append(run_id, event, sanitize_dict(inner if isinstance(inner, dict) else {"data": inner}))
        await self.publish(run_id, run_event)


ws_manager = ConnectionManager()


async def _load_history(run_id: str) -> list[RunEvent]:
    mem = event_store.recent(run_id, RECENT_WINDOW)
    if mem:
        return mem
    # Recover from DB after process restart
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EventRecord)
            .where(EventRecord.run_id == run_id)
            .order_by(EventRecord.sequence_number.desc())
            .limit(RECENT_WINDOW)
        )
        rows = list(reversed(result.scalars().all()))
    events: list[RunEvent] = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        events.append(
            RunEvent(
                event_id=row.id,
                run_id=row.run_id,
                event_type=row.event_type,
                timestamp=row.created_at or _utc_now(),
                sequence_number=row.sequence_number,
                payload=sanitize_dict(payload) if isinstance(payload, dict) else {},
            )
        )
    return events


@router.websocket("/ws/runs/{run_id}")
async def run_progress_ws(websocket: WebSocket, run_id: str) -> None:
    """Subscribe to live + historical events for a QA run."""
    client = await ws_manager.connect(run_id, websocket)
    sender = asyncio.create_task(client.send_loop())
    try:
        # Subscribed ack
        await websocket.send_json(
            WSMessage(
                type="subscribed",
                run_id=run_id,
                timestamp=_utc_now(),
                data={"ok": True},
            ).model_dump(mode="json")
        )

        # Historical recent events first
        history = await _load_history(run_id)
        for event in history:
            await websocket.send_json(
                WSMessage(
                    type="event",
                    run_id=run_id,
                    timestamp=event.timestamp,
                    event=event,
                    data={
                        "event": event.event_type,
                        "type": event.event_type,
                        "payload": event.payload,
                        "sequence_number": event.sequence_number,
                        "event_id": event.event_id,
                        "historical": True,
                    },
                ).model_dump(mode="json")
            )
        await websocket.send_json(
            WSMessage(
                type="history_complete",
                run_id=run_id,
                timestamp=_utc_now(),
                data={"count": len(history)},
            ).model_dump(mode="json")
        )

        # Keepalive / client pings — detect disconnects
        while client.alive:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                continue
            if raw in {"ping", "pong"}:
                continue
            # Ignore other client messages
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WS handler ended for run=%s", run_id, exc_info=True)
    finally:
        await ws_manager.disconnect(run_id, client)
        sender.cancel()
        try:
            await sender
        except asyncio.CancelledError:
            pass

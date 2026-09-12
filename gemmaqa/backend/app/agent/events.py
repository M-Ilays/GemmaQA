"""In-memory + DB-backed run event store with sequence numbers and redaction."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventRecord
from app.schemas import RunEvent
from app.utils.ids import new_id
from app.utils.logging import get_logger
from app.utils.sanitization import sanitize_dict

logger = get_logger("agent.events")

MAX_EVENTS_PER_RUN = 2000
RECENT_WINDOW = 200


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class RunEventBuffer:
    sequence: int = 0
    events: list[RunEvent] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class EventStore:
    """Per-run sequenced events. Safe for concurrent append/broadcast."""

    def __init__(self) -> None:
        self._buffers: dict[str, RunEventBuffer] = defaultdict(RunEventBuffer)

    def get_buffer(self, run_id: str) -> RunEventBuffer:
        return self._buffers[run_id]

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> RunEvent:
        clean_payload = sanitize_dict(payload or {})
        buf = self._buffers[run_id]
        async with buf.lock:
            buf.sequence += 1
            event = RunEvent(
                event_id=new_id(),
                run_id=run_id,
                event_type=event_type,
                timestamp=_utc_now(),
                sequence_number=buf.sequence,
                payload=clean_payload,
            )
            buf.events.append(event)
            if len(buf.events) > MAX_EVENTS_PER_RUN:
                buf.events = buf.events[-MAX_EVENTS_PER_RUN:]

        if session is not None:
            try:
                session.add(
                    EventRecord(
                        id=event.event_id,
                        run_id=run_id,
                        event_type=event_type,
                        sequence_number=event.sequence_number,
                        payload_json=json.dumps(clean_payload),
                        created_at=event.timestamp,
                    )
                )
                await session.commit()
            except Exception:
                logger.exception("Failed to persist event %s for run %s", event_type, run_id)
                await session.rollback()

        return event

    def recent(self, run_id: str, limit: int = RECENT_WINDOW) -> list[RunEvent]:
        buf = self._buffers.get(run_id)
        if not buf:
            return []
        return list(buf.events[-limit:])

    def clear(self, run_id: str) -> None:
        self._buffers.pop(run_id, None)


event_store = EventStore()

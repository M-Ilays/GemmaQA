"""Background run lifecycle manager (single-process asyncio)."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.controller import ACTIVE_RUNS, AgentController
from app.agent.events import event_store
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import ActionRecord, BugRecord, EventRecord, EvidenceRecord, PageRecord, QARun
from app.schemas import CreateRunRequest, RunStatusEnum
from app.utils.ids import new_id
from app.utils.logging import get_logger
from app.utils.sanitization import mask_secret, sanitize_dict

logger = get_logger("agent.run_manager")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class PreparedRun:
    """Ephemeral credentials + request held until start (never persisted as plaintext)."""

    run_id: str
    request: CreateRunRequest


_TERMINAL_RUN_STATUSES = {
    RunStatusEnum.COMPLETED.value,
    RunStatusEnum.CANCELLED.value,
    RunStatusEnum.FAILED.value,
    RunStatusEnum.STOPPED.value,
}


class RunManager:
    """Create / start / pause / resume / cancel / delete runs with task tracking."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._prepared: dict[str, PreparedRun] = {}
        self._start_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, run_id: str) -> asyncio.Lock:
        if run_id not in self._start_locks:
            self._start_locks[run_id] = asyncio.Lock()
        return self._start_locks[run_id]

    async def create_run(self, request: CreateRunRequest, session: AsyncSession) -> str:
        run_id = new_id()
        run = QARun(
            id=run_id,
            url=request.url,
            username=request.username,
            password_masked=mask_secret(request.password) if request.password else None,
            status=RunStatusEnum.CREATED.value,
            config_json=request.configuration.model_dump_json(),
            notes=request.notes,
            message="Created — awaiting start",
            created_at=_utc_now(),
        )
        session.add(run)
        await session.commit()

        self._prepared[run_id] = PreparedRun(run_id=run_id, request=request)
        await self._emit(
            run_id,
            "run_created",
            {"url": request.url, "status": RunStatusEnum.CREATED.value},
            session=session,
        )
        return run_id

    async def start_run(self, run_id: str, session: AsyncSession) -> QARun:
        async with self._lock_for(run_id):
            result = await session.execute(select(QARun).where(QARun.id == run_id))
            run = result.scalar_one_or_none()
            if run is None:
                raise LookupError("Run not found")

            if run_id in self._tasks and not self._tasks[run_id].done():
                raise RuntimeError("Run already started")

            if run.status not in {
                RunStatusEnum.CREATED.value,
                RunStatusEnum.PENDING.value,
            }:
                if run_id in ACTIVE_RUNS:
                    raise RuntimeError("Run already started")
                raise RuntimeError(f"Cannot start run in status '{run.status}'")

            prepared = self._prepared.get(run_id)
            if prepared is None:
                raise RuntimeError(
                    "Run credentials are no longer available in memory; recreate the run"
                )

            async def on_event(event_type: str, data: dict[str, Any]) -> None:
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
                if not isinstance(payload, dict):
                    payload = {"value": payload}
                safe = sanitize_dict(payload)
                event = await event_store.append(run_id, event_type, safe)
                from app.api.websocket import ws_manager

                await ws_manager.publish(run_id, event)

            # Hand credentials to the controller only via in-memory request object
            start_request = prepared.request
            controller = AgentController(
                run_id=run_id,
                request=start_request,
                db_factory=AsyncSessionLocal,
                on_event=on_event,
            )
            ACTIVE_RUNS[run_id] = controller
            # Drop prepared credential bag reference (controller clears after auth)
            self._prepared.pop(run_id, None)

            run.status = RunStatusEnum.INITIALIZING.value
            run.message = "Starting"
            run.started_at = _utc_now()
            run.error = None
            await session.commit()

            await self._emit(
                run_id,
                "run_status_changed",
                {"status": run.status, "message": run.message},
                session=session,
            )

            async def _runner() -> None:
                try:
                    await controller.run()
                except Exception as exc:
                    logger.exception("Background run crashed run_id=%s", run_id)
                    async with AsyncSessionLocal() as db:
                        result = await db.execute(select(QARun).where(QARun.id == run_id))
                        row = result.scalar_one_or_none()
                        if row and row.status not in {
                            RunStatusEnum.COMPLETED.value,
                            RunStatusEnum.CANCELLED.value,
                            RunStatusEnum.FAILED.value,
                        }:
                            row.status = RunStatusEnum.FAILED.value
                            row.message = "Run failed"
                            row.error = _public_error(exc)
                            row.finished_at = _utc_now()
                            await db.commit()
                    event = await event_store.append(
                        run_id,
                        "run_failed",
                        {"error": _public_error(exc)},
                    )
                    from app.api.websocket import ws_manager

                    await ws_manager.publish(run_id, event)
                finally:
                    ACTIVE_RUNS.pop(run_id, None)
                    self._tasks.pop(run_id, None)
                    self._prepared.pop(run_id, None)
                    try:
                        start_request.password = None
                    except Exception:
                        pass

            task = asyncio.create_task(_runner(), name=f"gemmaqa-run-{run_id}")
            self._tasks[run_id] = task
            return run

    async def cancel_run(self, run_id: str, session: AsyncSession) -> QARun:
        result = await session.execute(select(QARun).where(QARun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise LookupError("Run not found")

        controller = ACTIVE_RUNS.get(run_id)
        if controller:
            controller.request_cancel()

        if run.status in {RunStatusEnum.CREATED.value, RunStatusEnum.PENDING.value}:
            run.status = RunStatusEnum.CANCELLED.value
            run.message = "Cancelled before start"
            run.finished_at = _utc_now()
            self._prepared.pop(run_id, None)
            await session.commit()
            await self._emit(
                run_id,
                "run_cancelled",
                {"reason": "cancelled_before_start"},
                session=session,
            )
            return run

        run.message = "Cancel requested"
        task = self._tasks.get(run_id)
        if controller is None and (task is None or task.done()):
            run.status = RunStatusEnum.CANCELLED.value
            run.finished_at = _utc_now()
        await session.commit()

        await self._emit(
            run_id,
            "run_cancelled",
            {"reason": "cancel_requested"},
            session=session,
        )
        return run

    async def pause_run(self, run_id: str, session: AsyncSession) -> QARun:
        result = await session.execute(select(QARun).where(QARun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise LookupError("Run not found")
        if run.status in _TERMINAL_RUN_STATUSES:
            raise RuntimeError("Cannot pause a finished run")
        if run.status in {RunStatusEnum.CREATED.value, RunStatusEnum.PENDING.value}:
            raise RuntimeError("Cannot pause a run that has not started")

        controller = ACTIVE_RUNS.get(run_id)
        if controller is None:
            raise RuntimeError("Run is not active")
        if run.status == RunStatusEnum.PAUSED.value and controller.is_pause_requested():
            return run

        controller.request_pause()
        run.status = RunStatusEnum.PAUSED.value
        run.message = "Pause requested — finishing current step"
        await session.commit()
        await self._emit(
            run_id,
            "run_paused",
            {"reason": "operator_pause"},
            session=session,
        )
        return run

    async def resume_run(self, run_id: str, session: AsyncSession) -> QARun:
        result = await session.execute(select(QARun).where(QARun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise LookupError("Run not found")
        if run.status in _TERMINAL_RUN_STATUSES:
            raise RuntimeError("Cannot continue a finished run")

        controller = ACTIVE_RUNS.get(run_id)
        if controller is None:
            raise RuntimeError("Run is not active")
        if not controller.is_pause_requested() and run.status != RunStatusEnum.PAUSED.value:
            return run

        controller.request_resume()
        run.status = (
            controller.sm.status.value
            if hasattr(controller.sm.status, "value")
            else str(controller.sm.status)
        )
        run.message = "Resumed"
        await session.commit()
        await self._emit(
            run_id,
            "run_resumed",
            {"reason": "operator_resume"},
            session=session,
        )
        return run

    async def delete_run(
        self,
        run_id: str,
        session: AsyncSession,
        *,
        confirm: bool,
    ) -> None:
        if not confirm:
            raise PermissionError("Deletion requires confirm=true")

        result = await session.execute(select(QARun).where(QARun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise LookupError("Run not found")

        task = self._tasks.get(run_id)
        if run_id in ACTIVE_RUNS or (task and not task.done()):
            raise RuntimeError("Cannot delete an active run; cancel it first")

        for model in (EventRecord, EvidenceRecord, BugRecord, ActionRecord, PageRecord):
            await session.execute(delete(model).where(model.run_id == run_id))  # type: ignore[attr-defined]
        await session.execute(delete(QARun).where(QARun.id == run_id))
        await session.commit()

        self._prepared.pop(run_id, None)
        self._tasks.pop(run_id, None)
        event_store.clear(run_id)

        from app.api.websocket import ws_manager

        await ws_manager.drop_run(run_id)

        settings = get_settings()
        evidence_dir = settings.evidence_dir / run_id
        if evidence_dir.exists():
            shutil.rmtree(evidence_dir, ignore_errors=True)
        legacy = settings.reports_dir / run_id
        if legacy.exists() and legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)

        logger.info("Deleted local run records and evidence for %s", run_id)

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return bool(task and not task.done()) or run_id in ACTIVE_RUNS

    async def _emit(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        event = await event_store.append(run_id, event_type, payload, session=session)
        from app.api.websocket import ws_manager

        await ws_manager.publish(run_id, event)


def _public_error(exc: BaseException) -> str:
    settings = get_settings()
    if settings.debug:
        return f"{type(exc).__name__}: {exc}"
    return "An internal error occurred during the run"


run_manager = RunManager()

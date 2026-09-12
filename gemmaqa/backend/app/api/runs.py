"""QA run API endpoints."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.controller import ACTIVE_RUNS
from app.agent.events import event_store
from app.agent.run_manager import run_manager
from app.config import get_settings
from app.database import get_db
from app.models import ActionRecord, BugRecord, EventRecord, PageRecord, QARun
from app.reporting import service as report_service
from app.reporting.exporters import ReportExporter
from app.schemas import (
    CreateRunRequest,
    CreateRunResponse,
    DeleteRunResponse,
    ExecutionPacingUpdate,
    PaginatedRuns,
    RunConfiguration,
    RunEvent,
    RunStatus,
    RunStatusEnum,
)
from app.safety.url_guard import validate_target_url
from app.utils.logging import get_logger
from app.reporting.activity_log import (
    DEFAULT_READ_LIMIT,
    MAX_READ_LIMIT,
    ActivityLog,
)
from app.utils.sanitization import sanitize_dict

logger = get_logger("api.runs")

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _validate_create_payload(payload: CreateRunRequest) -> CreateRunRequest:
    if not payload.authorization_ack:
        raise HTTPException(
            status_code=400,
            detail=(
                "authorization_ack must be true. "
                "Only test systems you own or are explicitly authorized to test."
            ),
        )
    settings = get_settings()
    has_creds = bool(payload.password or payload.username)
    result = validate_target_url(
        payload.url,
        allow_local_targets=settings.allow_local_targets,
        require_https_for_credentials=settings.credentials_require_https,
        has_credentials=has_creds,
    )
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.reason)
    # Persist normalized URL (no userinfo)
    assert result.normalized is not None
    payload.url = result.normalized.normalized
    if payload.configuration.login_url:
        login_check = validate_target_url(
            payload.configuration.login_url,
            allow_local_targets=settings.allow_local_targets,
            require_https_for_credentials=settings.credentials_require_https,
            has_credentials=has_creds,
        )
        if not login_check.ok:
            raise HTTPException(status_code=400, detail=f"login_url: {login_check.reason}")
        assert login_check.normalized is not None
        payload.configuration.login_url = login_check.normalized.normalized
    return payload


@router.post("", response_model=CreateRunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    payload: CreateRunRequest,
    db: AsyncSession = Depends(get_db),
) -> CreateRunResponse:
    """Create a prepared QA run. Optionally auto-start when payload.auto_start is true."""
    payload = _validate_create_payload(payload)
    if get_settings().use_strands_orchestration:
        return await _create_run_via_strands(payload)

    run_id = await run_manager.create_run(payload, db)

    if payload.auto_start:
        try:
            run = await run_manager.start_run(run_id, db)
            st = (
                RunStatusEnum(run.status)
                if run.status in RunStatusEnum._value2member_map_
                else RunStatusEnum.INITIALIZING
            )
            return CreateRunResponse(
                run_id=run_id,
                status=st,
                message="Run created and started",
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return CreateRunResponse(
        run_id=run_id,
        status=RunStatusEnum.CREATED,
        message="Run created — call POST /api/runs/{run_id}/start to begin",
    )


@router.post("/strands", response_model=CreateRunResponse, status_code=status.HTTP_201_CREATED)
async def create_run_with_strands(payload: CreateRunRequest) -> CreateRunResponse:
    """Coordinate a New Run with the AWS Strands Agents SDK.

    Existing AgentController + Playwright still execute the browser work.
    Requires Amazon Bedrock config (AWS_REGION + STRANDS_MODEL_ID or BEDROCK_MODEL_ID).
    """
    payload = _validate_create_payload(payload)
    return await _create_run_via_strands(payload, require_enabled_flag=False)


async def _create_run_via_strands(
    payload: CreateRunRequest,
    *,
    require_enabled_flag: bool = True,
) -> CreateRunResponse:
    from app.strands_agent.service import run_with_strands_agent

    try:
        result = await run_with_strands_agent(
            payload,
            require_enabled_flag=require_enabled_flag,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    run_id = result.get("run_id")
    if not run_id:
        raise HTTPException(
            status_code=502,
            detail=result.get("message") or "Strands Agent did not launch a run",
        )
    return CreateRunResponse(
        run_id=run_id,
        status=RunStatusEnum.INITIALIZING,
        message=str(result.get("message") or "Strands Agent launched the QA run"),
    )


@router.get("", response_model=PaginatedRuns)
async def list_runs(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedRuns:
    """Paginated run history (newest first)."""
    total = int((await db.execute(select(func.count()).select_from(QARun))).scalar_one() or 0)
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    page = min(page, total_pages)
    offset = (page - 1) * page_size
    result = await db.execute(
        select(QARun).order_by(QARun.created_at.desc()).offset(offset).limit(page_size)
    )
    runs = result.scalars().all()
    return PaginatedRuns(
        items=[_to_status(r) for r in runs],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{run_id}", response_model=RunStatus)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)) -> RunStatus:
    run = await _get_run_or_404(db, run_id)
    return _to_status(run)


@router.post("/{run_id}/start", response_model=RunStatus)
async def start_run(run_id: str, db: AsyncSession = Depends(get_db)) -> RunStatus:
    """Start a prepared run. Rejects duplicate starts."""
    try:
        run = await run_manager.start_run(run_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_status(run)


@router.post("/{run_id}/cancel", response_model=RunStatus)
async def cancel_run(run_id: str, db: AsyncSession = Depends(get_db)) -> RunStatus:
    """Request safe cancellation (browser closed by agent finally block)."""
    try:
        run = await run_manager.cancel_run(run_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    await db.refresh(run)
    return _to_status(run)


@router.post("/{run_id}/end", response_model=RunStatus)
async def end_run(run_id: str, db: AsyncSession = Depends(get_db)) -> RunStatus:
    """Same as cancel — finish the run now and keep the report so far."""
    return await cancel_run(run_id, db)


@router.post("/{run_id}/pause", response_model=RunStatus)
async def pause_run(run_id: str, db: AsyncSession = Depends(get_db)) -> RunStatus:
    """Pause after the current step. The time budget does not tick while paused."""
    try:
        run = await run_manager.pause_run(run_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.refresh(run)
    return _to_status(run)


@router.post("/{run_id}/resume", response_model=RunStatus)
async def resume_run(run_id: str, db: AsyncSession = Depends(get_db)) -> RunStatus:
    """Continue a paused run. The time budget resumes from where it left off."""
    try:
        run = await run_manager.resume_run(run_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.refresh(run)
    return _to_status(run)


@router.post("/{run_id}/pacing", response_model=RunStatus)
async def update_run_pacing(
    run_id: str,
    payload: ExecutionPacingUpdate,
    db: AsyncSession = Depends(get_db),
) -> RunStatus:
    """Change execution speed / action pause on a live run. No restart."""
    run = await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller is None:
        raise HTTPException(status_code=409, detail="Run is not active")
    try:
        snap = controller.set_execution_pacing(
            execution_speed=payload.execution_speed,
            action_pause=payload.action_pause,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await controller.emit("execution_pacing", snap)
    return _to_status(run)


@router.get("/{run_id}/events", response_model=list[RunEvent])
async def get_run_events(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
) -> list[RunEvent]:
    await _get_run_or_404(db, run_id)
    mem = event_store.recent(run_id, limit)
    if mem:
        return mem
    result = await db.execute(
        select(EventRecord)
        .where(EventRecord.run_id == run_id)
        .order_by(EventRecord.sequence_number.desc())
        .limit(limit)
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
                timestamp=row.created_at,
                sequence_number=row.sequence_number,
                payload=sanitize_dict(payload) if isinstance(payload, dict) else {},
            )
        )
    return events


@router.get("/{run_id}/activity")
async def get_run_activity(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    since_seq: int = Query(0, ge=0, description="Return only records after this sequence number"),
    limit: int = Query(DEFAULT_READ_LIMIT, ge=1, le=MAX_READ_LIMIT),
    phase: str | None = Query(None, description="Restrict to one phase"),
) -> dict[str, Any]:
    """The run's durable activity log — what GemmaQA did, when, and how long it took.

    Readable WHILE the run is in progress, which is the point: the WebSocket
    timeline lives in browser memory and is lost on refresh, so this is what lets
    an operator reload mid-run, or come back afterwards, and still see every step.
    `since_seq` makes polling cheap — ask only for what you have not seen.
    """
    await _get_run_or_404(db, run_id)
    records = ActivityLog.read(run_id, since_seq=since_seq, limit=limit, phase=phase)
    return {
        "run_id": run_id,
        "records": records,
        "count": len(records),
        "last_seq": records[-1]["seq"] if records else since_seq,
        # A short page does not mean the log ended — it may just be the cap.
        "more_available": len(records) >= limit,
    }


@router.get("/{run_id}/pages")
async def get_run_pages(run_id: str, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    run = await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller and controller.memory.app_store and controller.memory.app_store.model.pages:
        return [
            p.model_dump(mode="json") for p in controller.memory.app_store.model.pages
        ]
    if run.application_json:
        try:
            data = json.loads(run.application_json)
            return list(data.get("pages") or [])
        except Exception:
            pass
    if controller and controller.memory.pages:
        seen: set[str] = set()
        out = []
        for p in controller.memory.pages:
            if p.url in seen:
                continue
            seen.add(p.url)
            out.append(
                {
                    "id": p.page_id,
                    "url": p.url,
                    "title": p.title,
                    "page_type": p.classification.page_type if p.classification else "unknown",
                    "screenshot_path": p.screenshot_path,
                }
            )
        return out
    result = await db.execute(select(PageRecord).where(PageRecord.run_id == run_id))
    pages = result.scalars().all()
    return [
        {
            "id": p.id,
            "url": p.url,
            "title": p.title,
            "page_type": p.page_type,
            "screenshot_path": p.screenshot_path,
        }
        for p in pages
    ]


@router.get("/{run_id}/modules")
async def get_run_modules(run_id: str, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    run = await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller and controller.memory.app_store and controller.memory.app_store.model.modules:
        return [
            m.model_dump(mode="json") for m in controller.memory.app_store.model.modules
        ]
    if controller and controller.memory.modules:
        return [m.model_dump(mode="json") for m in controller.memory.modules]
    if run.application_json:
        try:
            data = json.loads(run.application_json)
            return list(data.get("modules") or [])
        except Exception:
            pass
    report = report_service.load_report_model(run_id, run.report_json)
    if report and report.modules:
        return [m.model_dump(mode="json") for m in report.modules]
    return []


@router.get("/{run_id}/application")
async def get_run_application(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Canonical application structure for documentation and Application Structure UI."""
    run = await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller and controller.memory.app_store:
        return controller.memory.app_store.model.model_dump(mode="json")
    if run.application_json:
        try:
            return json.loads(run.application_json)
        except Exception:
            pass
    return {
        "run_id": run_id,
        "root_url": run.url,
        "allowed_origin": "",
        "application_name": "Unknown Application",
        "modules": [],
        "pages": [],
        "navigation_edges": [],
        "forms": [],
        "scenarios": [],
        "evidence": [],
        "external_references": [],
    }


@router.get("/{run_id}/navigation")
async def get_run_navigation(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller and controller.memory.app_store:
        model = controller.memory.app_store.model
        return {
            "pages": [p.model_dump(mode="json") for p in model.pages],
            "edges": [e.model_dump(mode="json") for e in model.navigation_edges],
        }
    if run.application_json:
        try:
            data = json.loads(run.application_json)
            return {
                "pages": data.get("pages") or [],
                "edges": data.get("navigation_edges") or [],
            }
        except Exception:
            pass
    return {"pages": [], "edges": []}


@router.get("/{run_id}/forms")
async def get_run_forms(run_id: str, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    run = await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller and controller.memory.app_store:
        return [f.model_dump(mode="json") for f in controller.memory.app_store.model.forms]
    if run.application_json:
        try:
            return list(json.loads(run.application_json).get("forms") or [])
        except Exception:
            pass
    return []


@router.get("/{run_id}/coverage")
async def get_run_coverage(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller:
        return controller.memory.coverage().model_dump(mode="json")
    report = report_service.load_report_model(run_id, run.report_json)
    if report and report.coverage:
        return report.coverage.model_dump(mode="json")
    return {"run_id": run_id, "pages_discovered": 0, "pages_explored": 0}


@router.get("/{run_id}/workflows")
async def get_run_workflows(run_id: str, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    run = await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller and controller.memory.workflows:
        return [w.model_dump(mode="json") for w in controller.memory.workflows]
    report = report_service.load_report_model(run_id, run.report_json)
    if report and report.workflows:
        return [w.model_dump(mode="json") for w in report.workflows]
    return []


@router.get("/{run_id}/tests")
async def get_run_tests(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller:
        return {
            "scenarios": [s.model_dump(mode="json") for s in controller.memory.scenarios],
            "executions": [e.model_dump(mode="json") for e in controller.memory.executions],
        }
    report = report_service.load_report_model(run_id, run.report_json)
    if report:
        return {
            "scenarios": [s.model_dump(mode="json") for s in report.test_scenarios],
            "executions": [e.model_dump(mode="json") for e in report.test_executions],
        }
    return {"scenarios": [], "executions": []}


@router.get("/{run_id}/actions")
async def get_run_actions(run_id: str, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    await _get_run_or_404(db, run_id)
    result = await db.execute(
        select(ActionRecord).where(ActionRecord.run_id == run_id).order_by(ActionRecord.created_at)
    )
    actions = result.scalars().all()
    return [
        {
            "id": a.id,
            "action_type": a.action_type,
            "success": a.success,
            "message": a.message,
            "duration_ms": a.duration_ms,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in actions
    ]


@router.get("/{run_id}/bugs")
async def get_run_bugs(run_id: str, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    await _get_run_or_404(db, run_id)
    controller = ACTIVE_RUNS.get(run_id)
    if controller and controller.memory.bugs:
        return [
            sanitize_dict(
                {
                    "id": b.bug_id,
                    "title": b.title,
                    "description": b.description,
                    "severity": b.severity.value,
                    "status": b.status.value,
                    "payload": b.model_dump(mode="json"),
                }
            )
            for b in controller.memory.bugs
        ]
    result = await db.execute(select(BugRecord).where(BugRecord.run_id == run_id))
    bugs = result.scalars().all()
    out = []
    for b in bugs:
        try:
            payload = json.loads(b.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        out.append(
            sanitize_dict(
                {
                    "id": b.id,
                    "title": b.title,
                    "description": b.description,
                    "severity": b.severity,
                    "status": b.status,
                    "payload": payload,
                }
            )
        )
    return out


@router.get("/{run_id}/report")
async def get_run_report(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    data = report_service.load_report_dict(run_id, run.report_json)
    if data is None:
        report = report_service.build_from_live_memory(run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not available yet")
        return sanitize_dict(report.model_dump(mode="json"))
    return sanitize_dict(data)


@router.delete("/{run_id}", response_model=DeleteRunResponse)
async def delete_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    confirm: bool = Query(False, description="Must be true to delete local run records and evidence"),
) -> DeleteRunResponse:
    """Delete local run DB rows and evidence only. Never touches the tested app."""
    try:
        await run_manager.delete_run(run_id, db, confirm=confirm)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return DeleteRunResponse(ok=True, run_id=run_id, message="Local run records and evidence deleted")


@router.get("/{run_id}/report.json")
async def get_run_report_json(run_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    run = await _get_run_or_404(db, run_id)
    path = report_service.find_artifact(run_id, "report.json", "final_report.json")
    if path:
        return Response(path.read_text(encoding="utf-8"), media_type="application/json")
    report = report_service.load_report_model(run_id, run.report_json)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not available yet")
    return Response(report.model_dump_json(indent=2), media_type="application/json")


@router.get("/{run_id}/report.md")
async def get_run_report_md(run_id: str, db: AsyncSession = Depends(get_db)) -> PlainTextResponse:
    run = await _get_run_or_404(db, run_id)
    path = report_service.find_artifact(run_id, "report.md", "final_report.md")
    if path:
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown")
    report = report_service.load_report_model(run_id, run.report_json)
    if report is None:
        raise HTTPException(status_code=404, detail="Markdown report not available yet")
    exporter = ReportExporter(run_id)
    md = exporter.export_markdown(report).read_text(encoding="utf-8")
    return PlainTextResponse(md, media_type="text/markdown")


@router.get("/{run_id}/report.html")
async def get_run_report_html(run_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    run = await _get_run_or_404(db, run_id)
    path = report_service.find_artifact(run_id, "report.html", "final_report.html")
    if path:
        return Response(path.read_text(encoding="utf-8"), media_type="text/html")
    report = report_service.load_report_model(run_id, run.report_json)
    if report is None:
        raise HTTPException(status_code=404, detail="HTML report not available yet")
    exporter = ReportExporter(run_id)
    html = exporter.export_html(report).read_text(encoding="utf-8")
    return Response(html, media_type="text/html")


@router.get("/{run_id}/bugs.csv")
async def get_run_bugs_csv(run_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    run = await _get_run_or_404(db, run_id)
    path = report_service.find_artifact(run_id, "bugs.csv")
    if path:
        return Response(path.read_text(encoding="utf-8"), media_type="text/csv")
    report = report_service.load_report_model(run_id, run.report_json)
    if report is None:
        raise HTTPException(status_code=404, detail="Bug CSV not available yet")
    exporter = ReportExporter(run_id)
    return Response(exporter.export_bugs_csv(report).read_text(encoding="utf-8"), media_type="text/csv")


@router.get("/{run_id}/tests.csv")
async def get_run_tests_csv(run_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    run = await _get_run_or_404(db, run_id)
    path = report_service.find_artifact(run_id, "tests.csv")
    if path:
        return Response(path.read_text(encoding="utf-8"), media_type="text/csv")
    report = report_service.load_report_model(run_id, run.report_json)
    if report is None:
        raise HTTPException(status_code=404, detail="Tests CSV not available yet")
    exporter = ReportExporter(run_id)
    return Response(exporter.export_tests_csv(report).read_text(encoding="utf-8"), media_type="text/csv")


@router.get("/{run_id}/evidence")
async def list_run_evidence(run_id: str, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    await _get_run_or_404(db, run_id)
    root = get_settings().run_evidence_dir(run_id)
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        kind = "other"
        if "screenshot" in rel.lower() or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            kind = "screenshot"
        elif "trace" in rel.lower() or path.suffix.lower() == ".zip":
            kind = "trace"
        elif path.suffix.lower() in {".md", ".html", ".json", ".csv", ".mmd"}:
            kind = "report"
        items.append({"path": rel, "kind": kind, "size": path.stat().st_size, "url": f"/api/runs/{run_id}/evidence/file/{rel}"})
    return items


@router.get("/{run_id}/evidence/file/{file_path:path}")
async def get_run_evidence_file(run_id: str, file_path: str, db: AsyncSession = Depends(get_db)) -> FileResponse:
    await _get_run_or_404(db, run_id)
    root = get_settings().run_evidence_dir(run_id).resolve()
    # Reject path traversal (including encoded .. segments)
    if ".." in Path(file_path).parts:
        raise HTTPException(status_code=404, detail="Evidence file not found")
    target = (root / file_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="Evidence file not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Evidence file not found")
    return FileResponse(target)


@router.get("/{run_id}/navigation.mmd")
async def get_run_navigation_mmd(run_id: str, db: AsyncSession = Depends(get_db)) -> PlainTextResponse:
    run = await _get_run_or_404(db, run_id)
    path = report_service.find_artifact(run_id, "navigation.mmd")
    if path:
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain")
    report = report_service.load_report_model(run_id, run.report_json)
    if report is None:
        raise HTTPException(status_code=404, detail="Navigation diagram not available yet")
    diagram = report.mermaid.get("navigation") or report.navigation_structure or "flowchart TD\n  A[No pages]"
    return PlainTextResponse(diagram, media_type="text/plain")


async def _get_run_or_404(db: AsyncSession, run_id: str) -> QARun:
    result = await db.execute(select(QARun).where(QARun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _to_status(run: QARun) -> RunStatus:
    try:
        status_enum = RunStatusEnum(run.status)
    except ValueError:
        status_enum = RunStatusEnum.PENDING
    error = run.error
    settings = get_settings()
    if error and not settings.debug and ("Traceback" in error or "\n  File " in error):
        error = "An internal error occurred during the run"
    controller = ACTIVE_RUNS.get(run.id)
    pacing = controller.pacing.snapshot() if controller is not None else {}
    return RunStatus(
        run_id=run.id,
        status=status_enum,
        url=run.url,
        current_url=run.current_url,
        pages_visited=run.pages_visited or 0,
        actions_taken=run.actions_taken or 0,
        bugs_found=run.bugs_found or 0,
        progress_pct=run.progress_pct or 0.0,
        message=run.message,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=error,
        notes=run.notes,
        username=run.username,
        configuration=_configuration_from_run(run),
        execution_speed=float(pacing.get("execution_speed", 1.0)),
        action_pause=float(pacing.get("action_pause", 0.0)),
    )


def _configuration_from_run(run: QARun) -> RunConfiguration:
    try:
        data = json.loads(run.config_json or "{}")
        if isinstance(data, dict):
            return RunConfiguration.model_validate(data)
    except Exception:
        pass
    return RunConfiguration()

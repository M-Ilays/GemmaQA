"""Report retrieval API endpoints (legacy /api/reports + helpers)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import QARun
from app.reporting import service as report_service
from app.reporting.exporters import ReportExporter
from app.utils.logging import get_logger

logger = get_logger("api.reports")

router = APIRouter(prefix="/api/reports", tags=["reports"])


async def _run_or_404(db: AsyncSession, run_id: str) -> QARun:
    result = await db.execute(select(QARun).where(QARun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/{run_id}")
async def get_report(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Return the structured report JSON for a run."""
    run = await _run_or_404(db, run_id)
    data = report_service.load_report_dict(run_id, run.report_json)
    if data is None:
        raise HTTPException(status_code=404, detail="Report not available yet")
    return data


@router.get("/{run_id}/markdown")
async def get_report_markdown(run_id: str, db: AsyncSession = Depends(get_db)) -> PlainTextResponse:
    run = await _run_or_404(db, run_id)
    path = report_service.find_artifact(run_id, "report.md", "final_report.md")
    if path:
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown")
    report = report_service.load_report_model(run_id, run.report_json)
    if report is None:
        raise HTTPException(status_code=404, detail="Markdown report not found")
    exporter = ReportExporter(run_id)
    md_path = exporter.export_markdown(report)
    return PlainTextResponse(md_path.read_text(encoding="utf-8"), media_type="text/markdown")


@router.get("/{run_id}/download")
async def download_report(run_id: str, db: AsyncSession = Depends(get_db)) -> FileResponse:
    await _run_or_404(db, run_id)
    path = report_service.find_artifact(run_id, "report.json", "final_report.json")
    if path:
        return FileResponse(
            path, filename=f"gemmaqa_{run_id}.json", media_type="application/json"
        )
    raise HTTPException(status_code=404, detail="Report file not found")

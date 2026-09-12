"""Resolve and serve synchronized QA reports from live memory or evidence files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent.controller import ACTIVE_RUNS
from app.config import get_settings
from app.reporting.exporters import ReportExporter
from app.reporting.report_builder import ReportBuilder
from app.schemas import FinalReport


def report_dirs(run_id: str) -> list[Path]:
    settings = get_settings()
    return [
        settings.run_evidence_dir(run_id) / "reports",
        settings.reports_dir / run_id,
    ]


def build_from_live_memory(run_id: str) -> FinalReport | None:
    controller = ACTIVE_RUNS.get(run_id)
    if controller is None:
        return None
    return ReportBuilder().build(controller.memory)


def load_report_model(run_id: str, report_json: str | None = None) -> FinalReport | None:
    live = build_from_live_memory(run_id)
    if live is not None:
        return live

    if report_json:
        try:
            return FinalReport.model_validate(json.loads(report_json))
        except Exception:
            pass

    for base in report_dirs(run_id):
        for name in ("report.json", "final_report.json"):
            path = base / name
            if path.exists():
                try:
                    return FinalReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
    return None


def load_report_dict(run_id: str, report_json: str | None = None) -> dict[str, Any] | None:
    model = load_report_model(run_id, report_json)
    if model is not None:
        return model.model_dump(mode="json")
    return None


def ensure_exports(run_id: str, report: FinalReport) -> dict[str, str]:
    """Write/refresh export artifacts for a report (idempotent)."""
    exporter = ReportExporter(run_id)
    return exporter.export_all(report, report.sections_markdown)


def find_artifact(run_id: str, *names: str) -> Path | None:
    for base in report_dirs(run_id):
        for name in names:
            path = base / name
            if path.exists():
                return path
    return None

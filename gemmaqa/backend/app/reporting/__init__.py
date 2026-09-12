"""Reporting package."""

from __future__ import annotations

from typing import Any

__all__ = [
    "REPORT_SECTION_ORDER",
    "ReportBuilder",
    "ReportExporter",
    "build_navigation_mermaid",
    "build_user_journey_mermaid",
    "build_workflow_mermaid",
    "sanitize_mermaid_label",
]


def __getattr__(name: str) -> Any:
    if name in {"REPORT_SECTION_ORDER", "ReportBuilder"}:
        from app.reporting import report_builder as _rb

        return getattr(_rb, name)
    if name == "ReportExporter":
        from app.reporting.exporters import ReportExporter

        return ReportExporter
    if name in {
        "build_navigation_mermaid",
        "build_user_journey_mermaid",
        "build_workflow_mermaid",
        "sanitize_mermaid_label",
    }:
        from app.reporting import mermaid_builder as _mb

        return getattr(_mb, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

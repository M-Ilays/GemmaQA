"""Optimization #2: generate_final_report only after the final ReportBuilder.build()."""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.controller import AgentController  # noqa: E402
from app.agent.documenter import Documenter  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.reporting.exporters import ReportExporter  # noqa: E402
from app.reporting.report_builder import REPORT_SECTION_ORDER, ReportBuilder  # noqa: E402
from app.schemas import PageState, RunConfiguration  # noqa: E402
from app.utils.ids import new_id  # noqa: E402

POLISHED = "Polished Contact List summary using only the recorded facts."


def _memory() -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url="https://thinking-tester-contact-list.herokuapp.com/")
    memory.bootstrap_budgets(RunConfiguration())
    memory.remember_page(
        PageState(
            page_id=new_id(),
            url="https://thinking-tester-contact-list.herokuapp.com/",
            title="Contact List App",
            state_fingerprint="fp-login",
        ),
        explored=True,
    )
    memory.stop_reason = "Maximum runtime exceeded"
    return memory


def _gemma(*, result: dict | None = None, error: Exception | None = None) -> AsyncMock:
    gemma = AsyncMock()
    if error is not None:
        gemma.generate_final_report = AsyncMock(side_effect=error)
    else:
        gemma.generate_final_report = AsyncMock(
            return_value=result or {"executive_summary": POLISHED}
        )
    return gemma


def _export_after_final_build(monkeypatch, tmp_path: Path, memory: RunMemory, report) -> dict:
    class _S:
        def run_evidence_dir(self, run_id: str) -> Path:
            d = tmp_path / run_id
            d.mkdir(parents=True, exist_ok=True)
            return d

    monkeypatch.setattr("app.reporting.exporters.get_settings", lambda: _S())
    ReportExporter(memory.run_id).export_all(report, report.sections_markdown)
    return json.loads((tmp_path / memory.run_id / "reports" / "final_report.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_mid_run_sync_does_not_call_generate_final_report() -> None:
    gemma = _gemma()
    doc = Documenter(gemma=gemma)
    memory = _memory()

    await doc.sync(memory)
    await doc.sync(memory)

    gemma.generate_final_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_still_writes_deterministic_reportbuilder_sections() -> None:
    gemma = _gemma()
    doc = Documenter(gemma=gemma)
    memory = _memory()

    sections = await doc.sync(memory)
    built = ReportBuilder().build(memory)

    for title in REPORT_SECTION_ORDER:
        assert title in sections
        assert sections[title].strip()
    assert "Executive Summary" in memory.doc_sections
    assert "Exploratory QA of" in memory.doc_sections["Executive Summary"]
    assert POLISHED not in memory.doc_sections["Executive Summary"]
    assert built.executive_summary in memory.doc_sections["Executive Summary"] or (
        built.executive_summary.replace("**", "") in memory.doc_sections["Executive Summary"]
        or "Contact List" in memory.doc_sections["Executive Summary"]
    )
    assert memory.doc_sections["Page Inventory"]


@pytest.mark.asyncio
async def test_final_polish_calls_generate_final_report_at_most_once() -> None:
    gemma = _gemma()
    doc = Documenter(gemma=gemma)
    memory = _memory()
    await doc.sync(memory)
    report = ReportBuilder().build(memory)

    once = await doc.polish_final_executive(memory, report)
    twice = await doc.polish_final_executive(memory, once)

    gemma.generate_final_report.assert_awaited_once()
    assert once.executive_summary == POLISHED
    assert twice.executive_summary == POLISHED


@pytest.mark.asyncio
async def test_successful_final_polish_is_preserved_in_exported_report(monkeypatch, tmp_path) -> None:
    gemma = _gemma()
    doc = Documenter(gemma=gemma)
    memory = _memory()
    await doc.sync(memory)
    report = ReportBuilder().build(memory)
    deterministic = report.executive_summary
    assert deterministic
    assert deterministic != POLISHED

    polished = await doc.polish_final_executive(memory, report)
    exported = _export_after_final_build(monkeypatch, tmp_path, memory, polished)

    assert polished.executive_summary == POLISHED
    assert exported["executive_summary"] == POLISHED
    assert exported["executive_summary"] != deterministic
    gemma.generate_final_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_final_polish_keeps_deterministic_report(monkeypatch, tmp_path) -> None:
    gemma = _gemma(error=TimeoutError("Gemma endpoint timeout after 60.0s"))
    doc = Documenter(gemma=gemma)
    memory = _memory()
    await doc.sync(memory)
    report = ReportBuilder().build(memory)
    deterministic = report.executive_summary
    assert "Exploratory QA of" in deterministic

    kept = await doc.polish_final_executive(memory, report)
    exported = _export_after_final_build(monkeypatch, tmp_path, memory, kept)

    assert kept.executive_summary == deterministic
    assert exported["executive_summary"] == deterministic
    assert exported["target_url"]
    assert "page_inventory" in exported
    gemma.generate_final_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_gemma_provider_keeps_deterministic_docs_and_export(monkeypatch, tmp_path) -> None:
    doc = Documenter(gemma=None)
    memory = _memory()

    sections = await doc.sync(memory)
    report = ReportBuilder().build(memory)
    after = await doc.polish_final_executive(memory, report)
    exported = _export_after_final_build(monkeypatch, tmp_path, memory, after)

    assert "Executive Summary" in sections
    assert after.executive_summary == report.executive_summary
    assert exported["executive_summary"] == report.executive_summary
    assert doc._final_polish_attempted is False


@pytest.mark.asyncio
async def test_gemini_provider_skips_final_polish() -> None:
    """Run 55588f75 sat ~17s after FINISH in generate_final_report, then closed."""
    gemma = _gemma()
    gemma.name = "gemini"
    doc = Documenter(gemma=gemma)
    memory = _memory()
    await doc.sync(memory)
    report = ReportBuilder().build(memory)
    deterministic = report.executive_summary

    kept = await doc.polish_final_executive(memory, report)

    gemma.generate_final_report.assert_not_awaited()
    assert kept.executive_summary == deterministic
    assert doc._final_polish_attempted is False


def test_controller_final_path_polishes_after_build_without_rebuild() -> None:
    source = inspect.getsource(AgentController.run)
    build_at = source.index("self.report_builder.build(self.memory)")
    polish_at = source.index("polish_final_executive")
    export_at = source.index("exporter.export_all")
    assert build_at < polish_at < export_at
    after_polish = source[polish_at:export_at]
    assert "report_builder.build" not in after_polish
    assert source.count("polish_final_executive") == 1

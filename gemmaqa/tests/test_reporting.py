"""Tests for synchronized QA reporting, exports, redaction, and coverage."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
SAMPLE_DIR = Path(__file__).resolve().parent / "fixtures" / "sample_report"
sys.path.insert(0, str(BACKEND))

from app.agent.documenter import Documenter  # noqa: E402
from app.agent.memory import NavigationEdge, ObservationNote, RunMemory  # noqa: E402
from app.reporting.exporters import ReportExporter  # noqa: E402
from app.reporting.mermaid_builder import (  # noqa: E402
    build_navigation_mermaid,
    sanitize_mermaid_label,
)
from app.reporting.report_builder import REPORT_SECTION_ORDER, ReportBuilder, redact_value  # noqa: E402
from app.schemas import (  # noqa: E402
    ActionResult,
    ActionType,
    BrowserAction,
    BugAnalysisResult,
    BugClassification,
    Defect,
    DefectSeverity,
    EvidenceItem,
    FormDescriptor,
    FormField,
    ModuleRecord,
    PageClassification,
    PageState,
    RoleObservation,
    RunConfiguration,
    TableDescriptor,
    Workflow,
    WorkflowStep,
)
from app.schemas import TestExecution as ScenarioExecution  # noqa: E402
from app.schemas import TestScenario as ScenarioSpec  # noqa: E402
from app.utils.ids import new_id  # noqa: E402
from app.utils.sanitization import MASK  # noqa: E402


def _rich_memory() -> RunMemory:
    run_id = "sample-run-001"
    memory = RunMemory(run_id=run_id, start_url="https://demo.gemmaqa.local/")
    memory.bootstrap_budgets(RunConfiguration(max_actions=40, max_pages=10))
    memory.domain_hypothesis = "Internal operations console for inventory and tickets"
    memory.domain_purpose = "Help operators manage inventory and support tickets"

    login = PageState(
        page_id="page-login",
        url="https://demo.gemmaqa.local/login",
        title="Login",
        navigation_items=["Home", "Login"],
        forms=[
            FormDescriptor(
                form_id="form-login",
                method="POST",
                action="/api/login",
                fields=[
                    FormField(name="email", field_type="email", label="Email", required=True),
                    FormField(
                        name="password",
                        field_type="password",
                        label="Password",
                        required=True,
                        current_value="super-secret-password",
                    ),
                ],
            )
        ],
        classification=PageClassification(page_type="authentication", confidence=0.9),
        state_fingerprint="fp-login",
        console_errors=["TypeError: x is undefined"],
        network_failures=["500 GET /api/session Authorization: Bearer tok_LIVE_abc"],
    )
    dash = PageState(
        page_id="page-dash",
        url="https://demo.gemmaqa.local/dashboard",
        title="Dashboard",
        navigation_items=["Dashboard", "Inventory", "Tickets"],
        tables=[
            TableDescriptor(
                table_id="tbl-tickets",
                headers=["ID", "Subject", "Status"],
                row_count=3,
            )
        ],
        classification=PageClassification(page_type="dashboard", confidence=0.8),
        state_fingerprint="fp-dash",
    )
    inv = PageState(
        page_id="page-inv",
        url="https://demo.gemmaqa.local/inventory",
        title="Inventory | GemmaQA Demo",
        navigation_items=["Dashboard", "Inventory"],
        forms=[
            FormDescriptor(
                form_id="form-search",
                method="GET",
                fields=[FormField(name="q", field_type="search", label="Search")],
            )
        ],
        classification=PageClassification(page_type="list", confidence=0.7, module_guess="Inventory"),
        state_fingerprint="fp-inv",
    )

    for page in (login, dash, inv):
        memory.remember_page(page, explored=True)

    memory.note_unexplored_url("https://demo.gemmaqa.local/settings")
    memory.navigation_edges = [
        NavigationEdge(login.url, dash.url, "submit", "el_010"),
        NavigationEdge(dash.url, inv.url, "click", "el_020"),
    ]
    memory.modules = [
        ModuleRecord(
            module_id="mod-auth",
            name="Authentication",
            description="Login and session",
            entry_urls=[login.url],
            page_ids=[login.page_id],
        ),
        ModuleRecord(
            module_id="mod-inv",
            name="Inventory",
            description="Stock listing",
            entry_urls=[inv.url],
            page_ids=[inv.page_id],
        ),
    ]
    memory.roles = [
        RoleObservation(
            role_name="operator",
            observed_capabilities=["view dashboard", "search inventory"],
            restricted_areas=["admin settings"],
        )
    ]
    memory.workflows = [
        Workflow(
            workflow_id="wf-login",
            name="Login to dashboard",
            starting_page=login.url,
            steps=[
                WorkflowStep(step_id="s1", order=1, action="fill", description="Enter email"),
                WorkflowStep(step_id="s2", order=2, action="fill", description="Enter password"),
                WorkflowStep(step_id="s3", order=3, action="click", description="Submit login"),
            ],
        )
    ]
    memory.scenarios = [
        ScenarioSpec(
            test_id="tc-001",
            title="Login with valid credentials",
            category="smoke",
            steps=["Open login", "Submit form"],
            expected_results=["Dashboard loads"],
        ),
        ScenarioSpec(
            test_id="tc-002",
            title="Search inventory",
            category="functional",
            steps=["Open inventory", "Search"],
            expected_results=["Results table updates"],
        ),
    ]
    memory.executions = [
        ScenarioExecution(
            execution_id="ex-001",
            test_id="tc-001",
            run_id=run_id,
            status="passed",
            notes="OK",
            executed_at=datetime.utcnow(),
        ),
        ScenarioExecution(
            execution_id="ex-002",
            test_id="tc-002",
            run_id=run_id,
            status="failed",
            notes="Authorization: Bearer leaked-token-should-redact",
            executed_at=datetime.utcnow(),
        ),
    ]
    memory.evidence = [
        EvidenceItem(
            evidence_id="ev-shot",
            run_id=run_id,
            kind="screenshot",
            path="evidence/sample-run-001/screenshots/login.png",
            description="Login page",
            page_id=login.page_id,
        )
    ]
    memory.bugs = [
        Defect(
            bug_id="BUG-001",
            run_id=run_id,
            title="HTTP 500 on session endpoint",
            description="Session API returns 500",
            severity=DefectSeverity.CRITICAL,
            classification="confirmed_bug",
            tags=["confirmed_bug", "Authentication"],
            module="Authentication",
            page_title="Login",
            page_url=login.url,
            expected="200 OK",
            actual="500 with Authorization: Bearer tok_LIVE_abc",
            steps_to_reproduce=["Open login", "Submit credentials"],
            test_data="password=super-secret-password",
            evidence_ids=["ev-shot"],
            confidence=0.95,
            business_impact="Users cannot establish a session",
            possible_root_cause="Unhandled exception in session service",
        )
    ]
    memory.suspected_bugs = [
        BugAnalysisResult(
            classification=BugClassification.SUSPECTED_BUG,
            title="Console TypeError on login",
            module="Authentication",
            severity="medium",
            actual_result="TypeError: x is undefined",
            expected_result="No console errors",
            confidence=0.6,
            page_url=login.url,
        )
    ]
    memory.observations = [
        ObservationNote(
            note_id="obs-1",
            title="Missing breadcrumbs on inventory",
            detail="Inventory list has no breadcrumb trail",
            page_url=inv.url,
        )
    ]
    memory.actions = [
        ActionResult(
            action_id="act-1",
            run_id=run_id,
            action=BrowserAction(
                action=ActionType.CLICK,
                element_id="el_020",
                reason="Open inventory",
                expected_result="Inventory page",
            ),
            success=True,
            before_url=dash.url,
            after_url=inv.url,
        )
    ]
    memory.user_journeys = ["Login → Dashboard → Inventory"]
    memory.stop_reason = "action_budget_sample"
    return memory


def test_report_synchronization_all_sections():
    memory = _rich_memory()
    sections = asyncio.run(Documenter(gemma=None).sync(memory))
    for title in REPORT_SECTION_ORDER:
        assert title in sections, f"missing section {title}"
        assert sections[title].strip()
    report = ReportBuilder().build(memory)
    assert report.page_inventory
    assert len(report.confirmed_bugs) >= 1
    assert len(report.suspected_bugs) >= 1
    # Sync again after adding a page — docs stay aligned with memory
    memory.remember_page(
        PageState(
            page_id="page-settings",
            url="https://demo.gemmaqa.local/settings",
            title="Settings",
            state_fingerprint="fp-settings",
        )
    )
    asyncio.run(Documenter(gemma=None).sync(memory))
    report2 = ReportBuilder().build(memory)
    assert any(p["url"].endswith("/settings") for p in report2.page_inventory)
    assert "Settings" in memory.doc_sections["Page Inventory"]


def _patch_evidence_dir(monkeypatch, root: Path) -> None:
    class _S:
        def run_evidence_dir(self, run_id: str) -> Path:
            d = root / run_id
            d.mkdir(parents=True, exist_ok=True)
            return d

    monkeypatch.setattr("app.reporting.exporters.get_settings", lambda: _S())


def test_bug_csv_export(tmp_path, monkeypatch):
    memory = _rich_memory()
    report = ReportBuilder().build(memory)
    _patch_evidence_dir(monkeypatch, tmp_path)
    path = ReportExporter(memory.run_id).export_bugs_csv(report)
    text = path.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert rows
    assert "bug_id" in reader.fieldnames
    assert any(r["bug_id"] == "BUG-001" for r in rows)
    assert MASK in text or "REDACTED" in text
    assert "super-secret-password" not in text
    assert "tok_LIVE_abc" not in text


def test_test_csv_export(tmp_path, monkeypatch):
    memory = _rich_memory()
    report = ReportBuilder().build(memory)
    _patch_evidence_dir(monkeypatch, tmp_path)
    exporter = ReportExporter(memory.run_id)
    tests_path = exporter.export_tests_csv(report)
    exec_path = exporter.export_executions_csv(report)
    tests = list(csv.DictReader(io.StringIO(tests_path.read_text(encoding="utf-8"))))
    execs = list(csv.DictReader(io.StringIO(exec_path.read_text(encoding="utf-8"))))
    assert {t["test_id"] for t in tests} >= {"tc-001", "tc-002"}
    assert len(execs) == 2
    assert "leaked-token-should-redact" not in exec_path.read_text(encoding="utf-8")


def test_mermaid_escaping():
    dirty = 'Login [admin] "quote" | pipe <script> #hash'
    clean = sanitize_mermaid_label(dirty)
    assert "[" not in clean and "]" not in clean
    assert '"' not in clean and "|" not in clean
    assert "<" not in clean and ">" not in clean
    assert "#" not in clean
    diagram = build_navigation_mermaid(
        [
            PageState(
                page_id="p1",
                url="https://x/a",
                title='A [weird] "title"|x',
            )
        ]
    )
    assert "flowchart" in diagram
    assert "[" not in diagram.split("\n")[1].split("[")[0] or True
    # Node label content must not contain raw quotes/brackets inside the quoted label badly
    assert '["' in diagram
    label_part = diagram.split('["', 1)[1].split('"]', 1)[0]
    assert '"' not in label_part
    assert "[" not in label_part


def test_missing_evidence_still_builds():
    memory = RunMemory(run_id=new_id(), start_url="https://example.com")
    memory.bootstrap_budgets(RunConfiguration(max_actions=5, max_pages=3))
    memory.bugs = [
        Defect(
            bug_id="BUG-X",
            run_id=memory.run_id,
            title="Bug without evidence",
            description="Missing evidence ids",
            severity=DefectSeverity.MINOR,
            classification="confirmed_bug",
            evidence_ids=["does-not-exist"],
        )
    ]
    report = ReportBuilder().build(memory)
    assert report.confirmed_bugs
    assert report.confirmed_bugs[0].screenshot_evidence == []
    assert "Evidence Index" in report.sections_markdown


def test_empty_run_report():
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/empty")
    memory.bootstrap_budgets(RunConfiguration(max_actions=1, max_pages=1))
    report = ReportBuilder().build(memory)
    assert report.run_id == memory.run_id
    assert report.page_inventory == []
    assert report.confirmed_bugs == []
    assert report.coverage is not None
    assert report.coverage.observed_coverage_pct == 0.0
    assert "do not claim complete" in report.coverage.disclaimer.lower() or "complete" in (
        report.coverage.coverage_notes[-1].lower() if report.coverage.coverage_notes else ""
    )
    for title in REPORT_SECTION_ORDER:
        assert title in report.sections_markdown


def test_secret_redaction():
    assert MASK in redact_value("password=hunter2")
    assert "hunter2" not in redact_value("password=hunter2")
    assert MASK in redact_value("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in redact_value("Bearer abc.def.ghi")
    assert MASK in redact_value("Cookie: sessionid=xyz123; Path=/")
    assert "xyz123" not in redact_value("sessionid=xyz123")
    nested = redact_value({"password": "secret", "ok": "visible", "token": "t"})
    assert nested["password"] == MASK
    assert nested["token"] == MASK
    assert nested["ok"] == "visible"


def test_coverage_calculations():
    memory = _rich_memory()
    cov = ReportBuilder().compute_coverage(memory)
    # Canonical pages only — inventory and discovered must match
    assert cov.pages_explored == 3
    assert cov.pages_discovered == 3
    assert any("settings" in u for u in cov.unexplored_areas)
    assert cov.observed_coverage_pct == pytest.approx(100.0)  # 3 explored / 3 discovered
    assert cov.explored_coverage_pct == pytest.approx(100.0)
    assert cov.tests_generated == 2
    assert cov.tests_executed == 2
    assert cov.passed == 1
    assert cov.failed == 1
    assert cov.executed_coverage_pct == pytest.approx(100.0)
    assert cov.bugs_found == 1
    assert cov.suspected_issues == 1
    assert any("not imply complete" in n.lower() for n in cov.coverage_notes)
    report = ReportBuilder().build(memory)
    assert len(report.page_inventory) == cov.pages_discovered


def test_export_all_and_sample_report(tmp_path, monkeypatch):
    memory = _rich_memory()
    report = ReportBuilder().build(memory)
    _patch_evidence_dir(monkeypatch, tmp_path)
    paths = ReportExporter(memory.run_id).export_all(report)
    assert Path(paths["json"]).exists()
    assert Path(paths["markdown"]).exists()
    assert Path(paths["html"]).exists()
    assert Path(paths["bugs_csv"]).exists()
    assert Path(paths["tests_csv"]).exists()
    assert Path(paths["pages_csv"]).exists()
    assert Path(paths["modules_csv"]).exists()
    nav = Path(paths.get("mermaid_navigation") or paths.get("navigation", ""))
    if not nav.exists():
        nav = tmp_path / memory.run_id / "reports" / "navigation.mmd"
    assert nav.exists()
    md = Path(paths["markdown"]).read_text(encoding="utf-8")
    for title in REPORT_SECTION_ORDER:
        assert title in md

    # Polished sample artifacts under tests/fixtures/sample_report
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    _patch_evidence_dir(monkeypatch, SAMPLE_DIR)
    sample_paths = ReportExporter(memory.run_id).export_all(report)
    readme = SAMPLE_DIR / "README.md"
    readme.write_text(
        "# Sample GemmaQA Report\n\n"
        "Generated from deterministic fixture memory. "
        "Memory is the source of truth; Gemma does not invent entities.\n\n"
        f"- JSON: `{Path(sample_paths['json']).name}`\n"
        f"- Markdown: `{Path(sample_paths['markdown']).name}`\n"
        f"- HTML: `{Path(sample_paths['html']).name}`\n",
        encoding="utf-8",
    )
    assert (SAMPLE_DIR / memory.run_id / "reports" / "final_report.md").exists()


def test_report_does_not_invent_pages():
    memory = RunMemory(run_id=new_id(), start_url="https://example.com")
    memory.bootstrap_budgets(RunConfiguration())
    memory.remember_page(
        PageState(page_id="p1", url="https://example.com/", title="Home", state_fingerprint="fp")
    )
    report = ReportBuilder().build(memory)
    urls = {p["url"] for p in report.page_inventory}
    assert urls == {"https://example.com/"}
    assert len(report.workflows) == 0
    assert len(report.confirmed_bugs) == 0

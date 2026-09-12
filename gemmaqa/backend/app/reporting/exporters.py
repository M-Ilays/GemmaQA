"""Export structured QA reports to JSON, Markdown, HTML, CSV, and Mermaid files."""

from __future__ import annotations

import csv
import html
import io
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.schemas import BugReportEntry, FinalReport
from app.utils.logging import get_logger
from app.utils.sanitization import sanitize_dict
from app.reporting.report_builder import REPORT_SECTION_ORDER, redact_value

logger = get_logger("reporting.exporters")


class ReportExporter:
    """Write FinalReport artifacts under evidence/<run_id>/reports/."""

    def __init__(self, run_id: str) -> None:
        settings = get_settings()
        self.run_id = run_id
        self.out_dir = settings.run_evidence_dir(run_id) / "reports"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.diagrams_dir = self.out_dir / "diagrams"
        self.diagrams_dir.mkdir(parents=True, exist_ok=True)

    def export_json(self, report: FinalReport) -> Path:
        path = self.out_dir / "final_report.json"
        # Sanitize nested dicts before dump
        data = sanitize_dict(report.model_dump(mode="json"))
        import json

        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        # Also write canonical name used by APIs
        (self.out_dir / "report.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("Wrote %s", path)
        return path

    def export_markdown(self, report: FinalReport, sections: dict[str, str] | None = None) -> Path:
        path = self.out_dir / "final_report.md"
        sec = sections or report.sections_markdown
        parts = [
            "# GemmaQA Synchronized QA Report",
            "",
            f"**Run ID:** `{report.run_id}`  ",
            f"**Target:** `{report.target_url}`  ",
            f"**Generated:** {report.generated_at.isoformat()}  ",
            "",
        ]
        for title in REPORT_SECTION_ORDER:
            body = sec.get(title)
            if body:
                stripped = body.lstrip()
                if stripped.startswith(f"## {title}"):
                    parts.extend([body, ""])
                else:
                    # Full markdown export includes headings; UI section bodies omit them
                    parts.extend([f"## {title}", "", body, ""])
        path.write_text("\n".join(parts), encoding="utf-8")
        (self.out_dir / "report.md").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("Wrote %s", path)
        return path

    def export_html(self, report: FinalReport, sections: dict[str, str] | None = None) -> Path:
        path = self.out_dir / "final_report.html"
        sec = sections or report.sections_markdown
        blocks = []
        for title in REPORT_SECTION_ORDER:
            body = sec.get(title, f"## {title}\n\n_None._")
            # Very small markdown→HTML conversion for headings/lists/code
            blocks.append(f'<section class="section"><h2 id="{html.escape(title)}">{html.escape(title)}</h2>')
            blocks.append(self._md_lite_to_html(body))
            blocks.append("</section>")

        cov = report.coverage
        cov_banner = ""
        if cov:
            cov_banner = (
                f"<div class='metrics'>"
                f"<div><strong>Observed</strong><span>{cov.observed_coverage_pct}%</span></div>"
                f"<div><strong>Explored</strong><span>{cov.explored_coverage_pct}%</span></div>"
                f"<div><strong>Executed</strong><span>{cov.executed_coverage_pct}%</span></div>"
                f"<div><strong>Bugs</strong><span>{cov.bugs_found}</span></div>"
                f"</div>"
                f"<p class='disclaimer'>{html.escape(cov.disclaimer)}</p>"
            )

        doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>GemmaQA Report {html.escape(report.run_id[:8])}</title>
  <style>
    :root {{ --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --accent:#0f766e; --bg:#f8fafc; }}
    body {{ margin:0; font-family: "Segoe UI", system-ui, sans-serif; color:var(--ink); background:var(--bg); }}
    header {{ background:linear-gradient(135deg,#0b1b2b,#134e4a); color:#fff; padding:2rem 1.5rem; }}
    header p {{ color:#99f6e4; }}
    main {{ max-width:960px; margin:0 auto; padding:1.5rem; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:1rem; margin:1rem 0 0; }}
    .metrics div {{ background:rgba(255,255,255,.08); border-radius:12px; padding:1rem; }}
    .metrics span {{ display:block; font-size:1.6rem; font-weight:700; margin-top:.35rem; }}
    .disclaimer {{ color:#cbd5e1; font-size:.9rem; }}
    .section {{ background:#fff; border:1px solid var(--line); border-radius:14px; padding:1.25rem 1.4rem; margin:1rem 0; }}
    h2 {{ margin-top:0; color:var(--accent); font-size:1.15rem; }}
    table {{ width:100%; border-collapse:collapse; font-size:.92rem; }}
    th, td {{ border-bottom:1px solid var(--line); text-align:left; padding:.45rem .3rem; vertical-align:top; }}
    code, pre {{ background:#f1f5f9; border-radius:6px; }}
    pre {{ padding:.75rem; overflow:auto; }}
    ul {{ padding-left:1.2rem; }}
    .muted {{ color:var(--muted); }}
  </style>
</head>
<body>
  <header>
    <h1>GemmaQA Report</h1>
    <p>Run <code>{html.escape(report.run_id)}</code> · {html.escape(report.target_url)}</p>
    {cov_banner}
  </header>
  <main>
    {''.join(blocks)}
    <p class="muted">Generated {html.escape(report.generated_at.isoformat())}. Deterministic memory is the source of truth.</p>
  </main>
</body>
</html>
"""
        path.write_text(doc, encoding="utf-8")
        (self.out_dir / "report.html").write_text(doc, encoding="utf-8")
        logger.info("Wrote %s", path)
        return path

    def export_bugs_csv(self, report: FinalReport) -> Path:
        path = self.out_dir / "bugs.csv"
        rows = [*report.confirmed_bugs, *report.suspected_bugs]
        fieldnames = [
            "bug_id",
            "title",
            "module",
            "page",
            "url",
            "classification",
            "severity",
            "priority",
            "preconditions",
            "test_data",
            "steps_to_reproduce",
            "expected_result",
            "actual_result",
            "business_impact",
            "possible_root_cause_hypothesis",
            "confidence",
            "screenshot_evidence",
            "trace_evidence",
            "console_evidence",
            "network_evidence",
            "discovery_timestamp",
            "run_id",
        ]
        self._write_csv(path, fieldnames, [self._bug_row(b) for b in rows])
        return path

    def export_tests_csv(self, report: FinalReport) -> Path:
        path = self.out_dir / "tests.csv"
        fieldnames = [
            "test_id",
            "title",
            "description",
            "category",
            "priority",
            "preconditions",
            "steps",
            "expected_results",
            "related_workflow_id",
        ]
        rows = []
        for t in report.test_scenarios:
            rows.append(
                {
                    "test_id": t.test_id,
                    "title": redact_value(t.title),
                    "description": redact_value(t.description),
                    "category": t.category,
                    "priority": t.priority,
                    "preconditions": " | ".join(t.preconditions),
                    "steps": " | ".join(t.steps),
                    "expected_results": " | ".join(t.expected_results),
                    "related_workflow_id": t.related_workflow_id or "",
                }
            )
        self._write_csv(path, fieldnames, rows)
        return path

    def export_executions_csv(self, report: FinalReport) -> Path:
        path = self.out_dir / "test_executions.csv"
        fieldnames = [
            "execution_id",
            "test_id",
            "run_id",
            "status",
            "notes",
            "evidence_ids",
            "executed_at",
        ]
        rows = []
        for e in report.test_executions:
            rows.append(
                {
                    "execution_id": e.execution_id,
                    "test_id": e.test_id,
                    "run_id": e.run_id,
                    "status": e.status,
                    "notes": redact_value(e.notes),
                    "evidence_ids": " | ".join(e.evidence_ids),
                    "executed_at": e.executed_at.isoformat() if e.executed_at else "",
                }
            )
        self._write_csv(path, fieldnames, rows)
        return path

    def export_pages_csv(self, report: FinalReport) -> Path:
        path = self.out_dir / "page_inventory.csv"
        fieldnames = ["page_id", "title", "url", "page_type", "forms", "tables", "fingerprint"]
        self._write_csv(path, fieldnames, report.page_inventory)
        return path

    def export_modules_csv(self, report: FinalReport) -> Path:
        path = self.out_dir / "module_inventory.csv"
        fieldnames = ["module_id", "name", "description", "entry_urls", "page_count"]
        rows = []
        for m in report.modules:
            rows.append(
                {
                    "module_id": m.module_id,
                    "name": m.name,
                    "description": m.description,
                    "entry_urls": " | ".join(m.entry_urls),
                    "page_count": len(m.page_ids),
                }
            )
        self._write_csv(path, fieldnames, rows)
        return path

    def export_mermaid(self, report: FinalReport) -> dict[str, str]:
        paths: dict[str, str] = {}
        # Canonical navigation file for API
        nav = report.mermaid.get("navigation") or report.navigation_structure or ""
        nav_path = self.out_dir / "navigation.mmd"
        nav_path.write_text(nav, encoding="utf-8")
        paths["navigation"] = str(nav_path)

        for name, diagram in (report.mermaid or {}).items():
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
            p = self.diagrams_dir / f"{safe}.mmd"
            p.write_text(diagram, encoding="utf-8")
            paths[name] = str(p)
        logger.info("Wrote %s mermaid diagrams", len(paths))
        return paths

    def export_all(
        self,
        report: FinalReport,
        sections: dict[str, str] | None = None,
    ) -> dict[str, str]:
        sec = sections or report.sections_markdown
        paths = {
            "json": str(self.export_json(report)),
            "markdown": str(self.export_markdown(report, sec)),
            "html": str(self.export_html(report, sec)),
            "bugs_csv": str(self.export_bugs_csv(report)),
            "tests_csv": str(self.export_tests_csv(report)),
            "executions_csv": str(self.export_executions_csv(report)),
            "pages_csv": str(self.export_pages_csv(report)),
            "modules_csv": str(self.export_modules_csv(report)),
        }
        paths.update({f"mermaid_{k}": v for k, v in self.export_mermaid(report).items()})
        return paths

    def _bug_row(self, b: BugReportEntry) -> dict[str, Any]:
        return {
            "bug_id": b.bug_id,
            "title": redact_value(b.title),
            "module": b.module,
            "page": b.page,
            "url": b.url,
            "classification": b.classification,
            "severity": b.severity,
            "priority": b.priority,
            "preconditions": " | ".join(b.preconditions),
            "test_data": redact_value(b.test_data),
            "steps_to_reproduce": " | ".join(b.steps_to_reproduce),
            "expected_result": redact_value(b.expected_result),
            "actual_result": redact_value(b.actual_result),
            "business_impact": redact_value(b.business_impact),
            "possible_root_cause_hypothesis": redact_value(b.possible_root_cause_hypothesis),
            "confidence": b.confidence,
            "screenshot_evidence": " | ".join(b.screenshot_evidence),
            "trace_evidence": " | ".join(b.trace_evidence),
            "console_evidence": " | ".join(b.console_evidence),
            "network_evidence": " | ".join(b.network_evidence),
            "discovery_timestamp": b.discovery_timestamp.isoformat() if b.discovery_timestamp else "",
            "run_id": b.run_id,
        }

    def _write_csv(self, path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            clean = {}
            for k in fieldnames:
                val = redact_value(row.get(k, ""))
                if isinstance(val, (list, tuple)):
                    val = " | ".join(str(x) for x in val)
                elif isinstance(val, dict):
                    val = str(sanitize_dict(val))
                clean[k] = "" if val is None else str(val)
            writer.writerow(clean)
        path.write_text(buf.getvalue(), encoding="utf-8")
        logger.info("Wrote %s", path)

    def _md_lite_to_html(self, md: str) -> str:
        lines = md.splitlines()
        out: list[str] = []
        in_code = False
        in_table = False
        for line in lines:
            if line.startswith("```"):
                if in_code:
                    out.append("</pre>")
                    in_code = False
                else:
                    out.append("<pre>")
                    in_code = True
                continue
            if in_code:
                out.append(html.escape(line))
                continue
            if line.startswith("## "):
                continue  # section title already rendered
            if line.startswith("### "):
                out.append(f"<h3>{html.escape(line[4:])}</h3>")
                continue
            if line.startswith("| ") and " | " in line:
                if "---" in line:
                    continue
                cells = [c.strip() for c in line.strip("|").split("|")]
                tag = "th" if not in_table else "td"
                if not in_table:
                    out.append("<table><thead>")
                    out.append("<tr>" + "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in cells) + "</tr>")
                    out.append("</thead><tbody>")
                    in_table = True
                else:
                    out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
                continue
            if in_table and not line.startswith("|"):
                out.append("</tbody></table>")
                in_table = False
            if line.startswith("- "):
                out.append(f"<li>{html.escape(line[2:])}</li>")
                continue
            if line.strip() == "":
                out.append("<br/>")
            else:
                out.append(f"<p>{html.escape(line)}</p>")
        if in_table:
            out.append("</tbody></table>")
        if in_code:
            out.append("</pre>")
        # wrap orphan lis
        html_body = "\n".join(out)
        html_body = html_body.replace("<li>", "<ul><li>", 1) if "<li>" in html_body else html_body
        return html_body

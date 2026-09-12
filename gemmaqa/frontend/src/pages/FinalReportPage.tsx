import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { BugCard } from "../components/BugCard";
import { DemoBadge } from "../components/DemoBadge";
import { InferredLabel } from "../components/InferredLabel";
import { MermaidDiagram } from "../components/MermaidDiagram";
import { Stat } from "../components/Stat";
import { EmptyState, FailureState, LoadingState } from "../components/States";
import { api, env } from "../services/api";
import type { ExportKind } from "../services/api";
import type { FinalReport } from "../types";

export function FinalReportPage() {
  const { runId = "" } = useParams();
  const [report, setReport] = useState<FinalReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getReport(runId)
      .then((r) => {
        if (alive) setReport(r);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "Report unavailable");
      });
    return () => {
      alive = false;
    };
  }, [runId]);

  if (error) return <FailureState title="Report not available" detail={error} />;
  if (!report) return <LoadingState label="Loading final report…" />;

  const cov = report.coverage;
  const exports: { kind: ExportKind; label: string }[] = [
    { kind: "json", label: "JSON" },
    { kind: "md", label: "Markdown" },
    { kind: "html", label: "HTML" },
    { kind: "bugs.csv", label: "Bugs CSV" },
    { kind: "tests.csv", label: "Tests CSV" },
    { kind: "navigation.mmd", label: "Navigation Mermaid" },
  ];

  return (
    <div className="space-y-8 animate-rise">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to={`/runs/${runId}`} className="text-sm text-slate-500 hover:text-tide-400">
            ← Live dashboard
          </Link>
          <div className="mt-2 flex items-center gap-2">
            <h1 className="font-display text-3xl text-white">Final report</h1>
            {env.demoMode ? <DemoBadge /> : null}
          </div>
          <p className="mt-1 break-all text-slate-400">{report.target_url}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {exports.map((ex) => (
            <a
              key={ex.kind}
              className="btn-ghost"
              href={api.exportUrl(runId, ex.kind)}
              target="_blank"
              rel="noreferrer"
            >
              {ex.label}
            </a>
          ))}
        </div>
      </div>

      <section className="surface p-6">
        <h2 className="font-display text-xl text-white">Executive summary</h2>
        <p className="mt-3 leading-relaxed text-slate-300">{report.executive_summary}</p>
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        <div className="surface p-6">
          <h2 className="font-display text-xl text-white">Product overview</h2>
          <p className="mt-2 text-lg text-tide-300">
            {report.product_overview?.product_name || "Unknown"}
          </p>
          <p className="mt-2 text-sm text-slate-400">{report.product_overview?.summary}</p>
          <p className="mt-3 text-sm text-slate-300">
            <span className="text-slate-500">Purpose:</span> {report.application_purpose}
          </p>
          <p className="mt-2 text-sm text-slate-300">
            <span className="text-slate-500">Domain</span>
            <InferredLabel />
            <span className="mt-1 block">{report.inferred_business_domain}</span>
          </p>
        </div>
        <div className="surface p-6">
          <h2 className="font-display text-xl text-white">Coverage</h2>
          {cov ? (
            <>
              <div className="mt-4 grid grid-cols-3 gap-2">
                <Stat label="Observed" value={`${cov.observed_coverage_pct}%`} />
                <Stat label="Explored" value={`${cov.explored_coverage_pct}%`} />
                <Stat label="Executed" value={`${cov.executed_coverage_pct}%`} />
              </div>
              <p className="mt-3 text-xs text-slate-500">{cov.disclaimer}</p>
              <ul className="mt-3 space-y-1 text-sm text-slate-400">
                <li>Pages explored {cov.pages_explored} / discovered {cov.pages_discovered}</li>
                <li>
                  Tests {cov.passed} passed · {cov.failed} failed · {cov.tests_executed} executed
                </li>
                <li>
                  Bugs {cov.bugs_found} · Suspected {cov.suspected_issues}
                </li>
              </ul>
            </>
          ) : (
            <EmptyState title="No coverage record" />
          )}
        </div>
      </section>

      <section className="surface p-6">
        <h2 className="font-display text-xl text-white">
          Modules
          <InferredLabel />
        </h2>
        <ul className="mt-4 grid gap-3 sm:grid-cols-2">
          {report.modules.map((m) => (
            <li key={m.module_id} className="rounded-xl border border-white/10 p-3">
              <p className="font-medium text-white">{m.name}</p>
              <p className="mt-1 text-sm text-slate-400">{m.description}</p>
            </li>
          ))}
        </ul>
      </section>

      <section className="surface p-6">
        <h2 className="font-display text-xl text-white">Application map</h2>
        <div className="mt-4">
          <MermaidDiagram chart={report.navigation_structure || report.mermaid?.navigation || ""} />
        </div>
      </section>

      <section className="space-y-4">
        <h2 className="font-display text-xl text-white">Workflow diagrams</h2>
        {report.workflows.map((wf) => (
          <div key={wf.workflow_id} className="surface p-4">
            <h3 className="font-medium text-white">{wf.name}</h3>
            <div className="mt-3">
              <MermaidDiagram
                chart={
                  wf.mermaid ||
                  report.mermaid[`workflow_${wf.workflow_id.slice(0, 8)}`] ||
                  ""
                }
              />
            </div>
          </div>
        ))}
        {!report.workflows.length ? <EmptyState title="No workflows recorded" /> : null}
      </section>

      <section className="surface p-6">
        <h2 className="font-display text-xl text-white">Test results</h2>
        <ul className="mt-4 space-y-2">
          {report.test_executions.map((ex) => {
            const scenario = report.test_scenarios.find((t) => t.test_id === ex.test_id);
            return (
              <li key={ex.execution_id} className="flex flex-wrap justify-between gap-2 border-b border-white/5 py-2 text-sm">
                <Link
                  to={`/runs/${runId}/tests/${ex.test_id}`}
                  className="text-tide-400 hover:text-tide-300"
                >
                  {scenario?.title || ex.test_id}
                </Link>
                <span
                  className={
                    ex.status === "passed"
                      ? "text-tide-400"
                      : ex.status === "failed"
                        ? "text-rose-300"
                        : "text-slate-400"
                  }
                >
                  {ex.status}
                </span>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="font-display text-xl text-white">Bug list</h2>
        {[...report.confirmed_bugs, ...report.suspected_bugs].map((b) => (
          <BugCard key={b.bug_id} bug={b} runId={runId} />
        ))}
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        <div className="surface p-6">
          <h2 className="font-display text-xl text-white">Regression checklist</h2>
          <ul className="mt-4 space-y-2 text-sm text-slate-300">
            {report.regression_checklist.map((item) => (
              <li key={item}>☐ {item}</li>
            ))}
          </ul>
        </div>
        <div className="surface p-6">
          <h2 className="font-display text-xl text-white">Evidence</h2>
          <ul className="mt-4 space-y-2 text-sm">
            {report.evidence_index.map((e, i) => (
              <li key={i}>
                <Link
                  to={`/runs/${runId}/evidence?path=${encodeURIComponent(String(e.path || ""))}`}
                  className="text-tide-400 hover:text-tide-300"
                >
                  {String(e.kind || "file")} · {String(e.path || e.evidence_id || "item")}
                </Link>
              </li>
            ))}
            {!report.evidence_index.length ? (
              <li className="text-slate-500">No evidence index entries</li>
            ) : null}
          </ul>
        </div>
      </section>
    </div>
  );
}

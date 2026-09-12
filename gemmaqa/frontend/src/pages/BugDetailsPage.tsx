import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { DemoBadge } from "../components/DemoBadge";
import { InferredLabel } from "../components/InferredLabel";
import { ScreenshotViewer } from "../components/ScreenshotViewer";
import { SeverityBadge } from "../components/SeverityBadge";
import { FailureState, LoadingState } from "../components/States";
import { demoPlaceholderScreenshot } from "../demo/fixtures";
import { api, env } from "../services/api";
import type { BugReportEntry, FinalReport } from "../types";
import { formatTime } from "../utils/format";

export function BugDetailsPage() {
  const { runId = "", bugId = "" } = useParams();
  const [bug, setBug] = useState<BugReportEntry | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getReport(runId)
      .then((report: FinalReport) => {
        if (!alive) return;
        const all = [...(report.confirmed_bugs || []), ...(report.suspected_bugs || [])];
        const found = all.find((b) => b.bug_id === bugId);
        if (!found) throw new Error("Bug not found in report");
        setBug(found);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "Failed to load bug");
      });
    return () => {
      alive = false;
    };
  }, [runId, bugId]);

  if (error) return <FailureState title="Bug unavailable" detail={error} />;
  if (!bug) return <LoadingState label="Loading bug details…" />;

  const shot = bug.screenshot_evidence[0];
  const shotUrl = env.demoMode
    ? demoPlaceholderScreenshot
    : shot
      ? api.evidenceFileUrl(runId, shot)
      : null;

  return (
    <div className="mx-auto max-w-3xl space-y-6 animate-rise">
      <div>
        <Link to={`/runs/${runId}/report`} className="text-sm text-slate-500 hover:text-tide-400">
          ← Report
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <p className="font-mono text-xs text-slate-500">{bug.bug_id}</p>
          {env.demoMode ? <DemoBadge /> : null}
          {bug.classification !== "confirmed_bug" ? (
            <InferredLabel>Suspected / inferred</InferredLabel>
          ) : null}
        </div>
        <h1 className="mt-2 font-display text-3xl text-white">{bug.title}</h1>
        <div className="mt-3 flex flex-wrap gap-2">
          <SeverityBadge severity={bug.severity} />
          <span className="rounded-md bg-white/5 px-2 py-0.5 text-[11px] uppercase text-slate-300 ring-1 ring-white/10">
            {bug.priority}
          </span>
          <span className="rounded-md bg-white/5 px-2 py-0.5 text-[11px] uppercase text-slate-300 ring-1 ring-white/10">
            {bug.classification.replace(/_/g, " ")}
          </span>
        </div>
      </div>

      <dl className="surface grid gap-4 p-6 sm:grid-cols-2">
        <Item label="Module" value={bug.module || "—"} />
        <Item label="Page" value={bug.page || "—"} />
        <Item label="URL" value={bug.url || "—"} mono />
        <Item label="Confidence" value={String(bug.confidence)} />
        <Item label="Expected" value={bug.expected_result || "—"} wide />
        <Item label="Actual" value={bug.actual_result || "—"} wide />
        <Item label="Business impact" value={bug.business_impact || "—"} wide />
        <Item
          label="Possible root cause (hypothesis)"
          value={bug.possible_root_cause_hypothesis || "—"}
          wide
          inferred
        />
        <Item label="Discovered" value={formatTime(bug.discovery_timestamp)} />
        <Item label="Run ID" value={bug.run_id} mono />
      </dl>

      <section className="surface p-6">
        <h2 className="text-sm font-medium text-slate-200">Steps to reproduce</h2>
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm text-slate-300">
          {bug.steps_to_reproduce.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
          {!bug.steps_to_reproduce.length ? <li className="list-none text-slate-500">None recorded</li> : null}
        </ol>
      </section>

      <section className="surface p-6">
        <h2 className="mb-3 text-sm font-medium text-slate-200">Evidence preview</h2>
        <ScreenshotViewer src={shotUrl} url={bug.url} timestamp={bug.discovery_timestamp} />
      </section>
    </div>
  );
}

function Item({
  label,
  value,
  mono,
  wide,
  inferred,
}: {
  label: string;
  value: string;
  mono?: boolean;
  wide?: boolean;
  inferred?: boolean;
}) {
  return (
    <div className={wide ? "sm:col-span-2" : ""}>
      <dt className="text-xs uppercase tracking-wider text-slate-500">
        {label}
        {inferred ? <InferredLabel /> : null}
      </dt>
      <dd className={`mt-1 text-sm text-slate-200 ${mono ? "break-all font-mono text-xs" : ""}`}>
        {value}
      </dd>
    </div>
  );
}

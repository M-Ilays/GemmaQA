import { Link } from "react-router-dom";
import type { BugReportEntry } from "../types";
import { InferredLabel } from "./InferredLabel";
import { SeverityBadge } from "./SeverityBadge";

export function BugCard({
  bug,
  runId,
  compact = false,
}: {
  bug: BugReportEntry;
  runId: string;
  compact?: boolean;
}) {
  const inferred = bug.classification === "suspected_bug" || bug.confidence < 0.8;

  return (
    <Link
      to={`/runs/${runId}/bugs/${bug.bug_id}`}
      className="surface block p-4 transition hover:border-tide-500/30 hover:bg-white/[0.04]"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-mono text-xs text-slate-500">{bug.bug_id}</p>
          <h3 className="mt-1 font-medium text-white">
            {bug.title}
            {inferred ? <InferredLabel>Suspected / inferred</InferredLabel> : null}
          </h3>
        </div>
        <div className="flex flex-wrap gap-2">
          <SeverityBadge severity={bug.severity} />
          <span className="rounded-md bg-white/5 px-2 py-0.5 text-[11px] uppercase tracking-wide text-slate-300 ring-1 ring-white/10">
            {bug.priority || "—"}
          </span>
        </div>
      </div>
      <p className="mt-2 text-xs uppercase tracking-wider text-slate-500">
        {bug.classification.replace(/_/g, " ")}
        {bug.module ? ` · ${bug.module}` : ""}
      </p>
      {!compact ? (
        <dl className="mt-3 grid gap-2 text-sm text-slate-300 sm:grid-cols-2">
          <div>
            <dt className="text-xs text-slate-500">Expected</dt>
            <dd className="mt-0.5 line-clamp-2">{bug.expected_result || "—"}</dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Actual</dt>
            <dd className="mt-0.5 line-clamp-2">{bug.actual_result || "—"}</dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-xs text-slate-500">Business impact</dt>
            <dd className="mt-0.5 line-clamp-2">{bug.business_impact || "—"}</dd>
          </div>
        </dl>
      ) : null}
      {bug.screenshot_evidence?.length ? (
        <p className="mt-3 text-xs text-tide-400">
          Evidence preview · {bug.screenshot_evidence.length} screenshot
          {bug.screenshot_evidence.length === 1 ? "" : "s"}
        </p>
      ) : (
        <p className="mt-3 text-xs text-slate-600">No screenshot evidence attached</p>
      )}
    </Link>
  );
}

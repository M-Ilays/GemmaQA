import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, env } from "../services/api";
import type { RunStatus } from "../types";
import { DemoBadge } from "../components/DemoBadge";
import { EmptyState, FailureState, LoadingState } from "../components/States";
import { formatStatus, formatTime, shortId, statusColor } from "../utils/format";

export function RunHistoryPage() {
  const [runs, setRuns] = useState<RunStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .listRuns()
      .then((data) => {
        if (alive) setRuns(data);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "Failed to load runs");
      });
    return () => {
      alive = false;
    };
  }, []);

  const sorted = useMemo(
    () =>
      [...(runs || [])].sort((a, b) =>
        String(b.started_at || "").localeCompare(String(a.started_at || ""))
      ),
    [runs]
  );

  if (error) return <FailureState title="Could not load history" detail={error} />;
  if (!runs) return <LoadingState label="Loading run history…" />;
  if (!sorted.length) {
    return (
      <EmptyState
        title="No runs yet"
        detail="Start an exploratory QA run to populate history."
      />
    );
  }

  return (
    <div className="space-y-6 animate-rise">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-3xl text-white">Run history</h1>
          <p className="mt-2 text-slate-400">Past and in-progress exploratory QA sessions.</p>
        </div>
        <div className="flex items-center gap-2">
          {env.demoMode ? <DemoBadge /> : null}
          <Link to="/runs/new" className="btn-primary">
            New run
          </Link>
        </div>
      </div>

      <div className="overflow-hidden rounded-2xl border border-white/10">
        <table className="w-full text-left text-sm">
          <thead className="bg-white/[0.04] text-[11px] uppercase tracking-[0.14em] text-slate-500">
            <tr>
              <th className="px-4 py-3">Run</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Target</th>
              <th className="px-4 py-3">Pages</th>
              <th className="px-4 py-3">Bugs</th>
              <th className="px-4 py-3">Started</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {sorted.map((run) => (
              <tr key={run.run_id} className="border-t border-white/5 hover:bg-white/[0.03]">
                <td className="px-4 py-3">
                  <Link
                    to={`/runs/${run.run_id}`}
                    className="font-mono text-tide-400 hover:text-tide-300"
                  >
                    {shortId(run.run_id)}
                  </Link>
                </td>
                <td className={`px-4 py-3 capitalize ${statusColor(run.status)}`}>
                  {formatStatus(run.status)}
                </td>
                <td className="max-w-[240px] truncate px-4 py-3 text-slate-300">{run.url}</td>
                <td className="px-4 py-3 text-slate-300">{run.pages_visited}</td>
                <td className="px-4 py-3 text-slate-300">{run.bugs_found}</td>
                <td className="px-4 py-3 text-slate-500">{formatTime(run.started_at)}</td>
                <td className="px-4 py-3 text-right">
                  <Link
                    to={`/runs/new?from=${run.run_id}`}
                    className="text-xs text-tide-400 hover:text-tide-300"
                  >
                    Test again
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

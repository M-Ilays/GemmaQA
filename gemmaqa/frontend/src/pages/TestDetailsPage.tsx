import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { DemoBadge } from "../components/DemoBadge";
import { FailureState, LoadingState } from "../components/States";
import { api, env } from "../services/api";
import type { FinalReport, TestExecution, TestScenario } from "../types";
import { formatTime } from "../utils/format";

export function TestDetailsPage() {
  const { runId = "", testId = "" } = useParams();
  const [scenario, setScenario] = useState<TestScenario | null>(null);
  const [execution, setExecution] = useState<TestExecution | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getReport(runId)
      .then((report: FinalReport) => {
        if (!alive) return;
        const sc = report.test_scenarios.find((t) => t.test_id === testId);
        if (!sc) throw new Error("Test scenario not found");
        setScenario(sc);
        setExecution(report.test_executions.find((e) => e.test_id === testId) || null);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "Failed to load test");
      });
    return () => {
      alive = false;
    };
  }, [runId, testId]);

  if (error) return <FailureState title="Test unavailable" detail={error} />;
  if (!scenario) return <LoadingState label="Loading test details…" />;

  return (
    <div className="mx-auto max-w-3xl space-y-6 animate-rise">
      <div>
        <Link to={`/runs/${runId}/report`} className="text-sm text-slate-500 hover:text-tide-400">
          ← Report
        </Link>
        <div className="mt-2 flex items-center gap-2">
          <p className="font-mono text-xs text-slate-500">{scenario.test_id}</p>
          {env.demoMode ? <DemoBadge /> : null}
        </div>
        <h1 className="mt-2 font-display text-3xl text-white">{scenario.title}</h1>
        <p className="mt-2 text-sm text-slate-400">{scenario.description}</p>
        <p className="mt-2 text-xs uppercase tracking-wider text-slate-500">
          {scenario.category} · {scenario.priority}
        </p>
      </div>

      {execution ? (
        <div className="surface p-4 text-sm">
          <p>
            Execution status:{" "}
            <span
              className={
                execution.status === "passed"
                  ? "text-tide-400"
                  : execution.status === "failed"
                    ? "text-rose-300"
                    : "text-slate-300"
              }
            >
              {execution.status}
            </span>
          </p>
          <p className="mt-1 text-slate-400">{execution.notes || "No notes"}</p>
          <p className="mt-1 text-xs text-slate-600">{formatTime(execution.executed_at)}</p>
        </div>
      ) : null}

      <section className="surface p-6">
        <h2 className="text-sm font-medium text-slate-200">Preconditions</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-300">
          {scenario.preconditions.map((p) => (
            <li key={p}>{p}</li>
          ))}
          {!scenario.preconditions.length ? <li className="list-none text-slate-500">None</li> : null}
        </ul>
      </section>

      <section className="surface p-6">
        <h2 className="text-sm font-medium text-slate-200">Steps</h2>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-slate-300">
          {scenario.steps.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ol>
      </section>

      <section className="surface p-6">
        <h2 className="text-sm font-medium text-slate-200">Expected results</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-300">
          {scenario.expected_results.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ul>
      </section>
    </div>
  );
}

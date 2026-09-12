import type { NormalizedWsEvent } from "../types";
import { deriveLiveAction } from "../utils/liveAction";

export function LiveActionStatus({
  events,
  runStatus,
}: {
  events: NormalizedWsEvent[];
  runStatus: string;
}) {
  const view = deriveLiveAction(events, runStatus);
  const tone =
    view.kind === "blocked" || view.kind === "failed"
      ? "text-rose-200"
      : view.kind === "idle"
        ? "text-slate-400"
        : "text-cyan-200";

  return (
    <div className="surface p-4">
      <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Current action</p>
      <p className="mt-2 text-xs font-semibold uppercase tracking-wider text-slate-300">
        {view.banner}
      </p>
      <p className="mt-1 text-slate-600" aria-hidden>
        ↓
      </p>
      <p className={`mt-1 whitespace-pre-wrap text-sm font-medium ${tone}`}>{view.line}</p>
      {view.reason ? (
        <p className="mt-1 text-xs text-rose-300">REASON → {view.reason}</p>
      ) : null}
    </div>
  );
}

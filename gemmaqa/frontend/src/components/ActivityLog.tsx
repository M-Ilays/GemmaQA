import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, env } from "../services/api";
import type { ActivityRecord, NormalizedWsEvent } from "../types";
import { clockFromIso, isActionActivityEvent } from "../utils/liveAction";

/**
 * The run's activity log: what GemmaQA did, when, and how long it took.
 *
 * Replaces a timeline that lived only in browser memory, capped at 250 events,
 * rendered truncated raw JSON, and was lost on refresh — which is exactly why an
 * operator watching a run could not tell what it was doing. History is hydrated
 * from `GET /api/runs/{id}/activity` (durable, written by the backend), then kept
 * current from the WebSocket, so reloading mid-run loses nothing.
 */

const PHASE_STYLES: Record<string, string> = {
  startup: "bg-slate-500/15 text-slate-300",
  observation: "bg-sky-500/15 text-sky-300",
  understanding: "bg-indigo-500/15 text-indigo-300",
  planning: "bg-tide-500/15 text-tide-300",
  execution: "bg-emerald-500/15 text-emerald-300",
  verification: "bg-violet-500/15 text-violet-300",
  intelligence: "bg-cyan-500/15 text-cyan-300",
  cleanup: "bg-amber-500/15 text-amber-300",
  reporting: "bg-fuchsia-500/15 text-fuchsia-300",
  lifecycle: "bg-white/5 text-slate-400",
};

/** Slow enough to be worth an operator's attention. */
const SLOW_SPAN_MS = 2000;

function elapsed(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function duration(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

/** Plain-text form of the Activity tab — what Copy puts on the clipboard. */
export function formatActivityLogText(
  records: ActivityRecord[],
  opts: { runId: string; phaseFilter?: string | null } = { runId: "" },
): string {
  const filterNote = opts.phaseFilter ? `  filter=${opts.phaseFilter}` : "";
  const header = `GemmaQA activity log  run=${opts.runId || "unknown"}  ${records.length} steps${filterNote}`;
  if (!records.length) return `${header}\n(empty)`;
  const lines = records.map((record) => {
    const time = elapsed(record.elapsed_ms);
    const took = record.duration_ms !== undefined ? `  ${duration(record.duration_ms)}` : "";
    return `${time}  ${record.phase.padEnd(14)}  ${record.summary}${took}`;
  });
  return [header, ...lines].join("\n");
}

/** A live WebSocket frame carries the same fields the stored record has —
 * the backend composes both from one place, so neither side re-derives them. */
function fromWsEvent(ev: NormalizedWsEvent): ActivityRecord | null {
  const raw = ev.raw as unknown as Partial<ActivityRecord>;
  if (typeof raw?.seq !== "number") return null;
  return {
    seq: raw.seq,
    run_id: ev.run_id,
    at: ev.timestamp,
    elapsed_ms: raw.elapsed_ms ?? 0,
    phase: raw.phase ?? "lifecycle",
    event: ev.type,
    summary: raw.summary ?? ev.type,
    detail: ev.payload,
  };
}

export function ActivityLog({
  runId,
  events,
}: {
  runId: string;
  events: NormalizedWsEvent[];
}) {
  const [records, setRecords] = useState<ActivityRecord[]>([]);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [phaseFilter, setPhaseFilter] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const [copied, setCopied] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const lastSeq = useRef(0);
  const scroller = useRef<HTMLDivElement | null>(null);

  const merge = useCallback((incoming: ActivityRecord[]) => {
    if (!incoming.length) return;
    setRecords((prev) => {
      // Keyed by seq so a record arriving from both the API and the socket
      // appears once. Without this, hydrating while connected double-lists
      // everything.
      const bySeq = new Map(prev.map((r) => [r.seq, r]));
      for (const record of incoming) bySeq.set(record.seq, record);
      const merged = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
      lastSeq.current = Math.max(lastSeq.current, merged[merged.length - 1]?.seq ?? 0);
      return merged;
    });
  }, []);

  // Hydrate the history the socket never saw, then top up as the run advances.
  useEffect(() => {
    if (!runId || env.demoMode) return;
    let cancelled = false;
    const pull = async () => {
      try {
        const page = await api.getActivity(runId, lastSeq.current);
        if (!cancelled) {
          merge(page.records);
          setLoadError(null);
        }
      } catch (err) {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : "could not load the log");
      }
    };
    void pull();
    // Polls as a safety net, not as the primary channel: the socket delivers
    // records immediately, and this catches anything a dropped connection lost.
    const timer = window.setInterval(pull, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [runId, merge]);

  useEffect(() => {
    const live = events.map(fromWsEvent).filter((r): r is ActivityRecord => r !== null);
    merge(live);
  }, [events, merge]);

  const phases = useMemo(
    () => [...new Set(records.map((r) => r.phase))].sort(),
    [records],
  );
  const visible = useMemo(
    () => (phaseFilter ? records.filter((r) => r.phase === phaseFilter) : records),
    [records, phaseFilter],
  );

  const budget = useMemo(() => {
    const totals = new Map<string, number>();
    for (const record of records) {
      if (record.duration_ms === undefined) continue;
      totals.set(record.phase, (totals.get(record.phase) ?? 0) + record.duration_ms);
    }
    return [...totals.entries()].sort((a, b) => b[1] - a[1]);
  }, [records]);

  useEffect(() => {
    if (follow && scroller.current) {
      scroller.current.scrollTop = scroller.current.scrollHeight;
    }
  }, [visible.length, follow]);

  const copyLog = async () => {
    const text = formatActivityLogText(visible, { runId, phaseFilter });
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium text-slate-200">
          Activity log
          <span className="ml-2 text-xs font-normal text-slate-500">{records.length} steps</span>
        </h3>
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="btn-ghost px-3 py-1.5 text-xs"
            disabled={!visible.length}
            onClick={() => void copyLog()}
          >
            {copied ? "Copied" : "Copy log"}
          </button>
          <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              className="h-3 w-3 accent-tide-500"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
            />
            Follow
          </label>
        </div>
      </div>

      {budget.length ? (
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          {/* Where the time actually went — the question a list of events, however
              complete, cannot answer. */}
          {budget.map(([phase, ms]) => (
            <span
              key={phase}
              className={`rounded-full px-2 py-0.5 ${PHASE_STYLES[phase] ?? PHASE_STYLES.lifecycle}`}
            >
              {phase} {duration(ms)}
            </span>
          ))}
        </div>
      ) : null}

      {phases.length > 1 ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          <FilterChip label="All" active={phaseFilter === null} onClick={() => setPhaseFilter(null)} />
          {phases.map((phase) => (
            <FilterChip
              key={phase}
              label={phase}
              active={phaseFilter === phase}
              onClick={() => setPhaseFilter(phase === phaseFilter ? null : phase)}
            />
          ))}
        </div>
      ) : null}

      <div ref={scroller} className="mt-3 max-h-96 space-y-1 overflow-y-auto pr-1 text-sm">
        {visible.map((record) => {
          const slow = (record.duration_ms ?? 0) >= SLOW_SPAN_MS;
          return (
            <div
              key={record.seq}
              className="rounded-lg border border-white/5 bg-ink-950/40 px-2.5 py-1.5"
            >
              <button
                type="button"
                className="flex w-full items-start gap-2 text-left"
                onClick={() => setExpanded(expanded === record.seq ? null : record.seq)}
              >
                <span className="mt-0.5 font-mono text-xs tabular-nums text-slate-600">
                  {clockFromIso(record.at) || elapsed(record.elapsed_ms)}
                </span>
                <span
                  className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                    isActionActivityEvent(record.event)
                      ? "bg-emerald-500/15 text-emerald-300"
                      : PHASE_STYLES[record.phase] ?? PHASE_STYLES.lifecycle
                  }`}
                >
                  {isActionActivityEvent(record.event) ? "ACTION" : record.phase}
                </span>
                <span className="flex-1 whitespace-pre-wrap text-slate-300">{record.summary}</span>
                {record.duration_ms !== undefined ? (
                  <span
                    className={`mt-0.5 shrink-0 font-mono text-xs tabular-nums ${
                      slow ? "text-amber-300" : "text-slate-600"
                    }`}
                  >
                    {duration(record.duration_ms)}
                  </span>
                ) : null}
              </button>
              {expanded === record.seq ? (
                <pre className="mt-2 max-h-56 overflow-auto rounded bg-black/30 p-2 text-xs text-slate-400">
                  {JSON.stringify(record.detail, null, 2)}
                </pre>
              ) : null}
            </div>
          );
        })}
        {!visible.length ? (
          <p className="text-slate-500">
            {loadError ? `Could not load the activity log: ${loadError}` : "Waiting for the first step…"}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function FilterChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full px-2 py-0.5 text-xs ${
        active ? "bg-tide-500/25 text-tide-200" : "bg-white/5 text-slate-400 hover:text-slate-200"
      }`}
    >
      {label}
    </button>
  );
}

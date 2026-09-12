import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ActivityLog } from "../components/ActivityLog";
import { ExecutionPacingControls } from "../components/ExecutionPacingControls";
import { LiveActionStatus } from "../components/LiveActionStatus";
import {
  DEFAULT_ACTION_PAUSE,
  DEFAULT_EXECUTION_SPEED,
} from "../utils/executionPacing";
import { BugCard } from "../components/BugCard";
import { DemoBadge } from "../components/DemoBadge";
import { InferredLabel } from "../components/InferredLabel";
import { MermaidDiagram } from "../components/MermaidDiagram";
import { ScreenshotViewer } from "../components/ScreenshotViewer";
import { ProgressBar, Stat } from "../components/Stat";
import {
  EmptyState,
  FailureState,
  LoadingState,
  ReconnectingBanner,
} from "../components/States";
import { demoPlaceholderScreenshot } from "../demo/fixtures";
import { useRunSocket } from "../hooks/useRunSocket";
import { api, env } from "../services/api";
import type {
  ActionItem,
  BugItem,
  BugReportEntry,
  EvidenceFile,
  FinalReport,
  ModuleRecord,
  PageItem,
  RunStatus,
  TestScenario,
  Workflow,
} from "../types";
import {
  connectionLabel,
  formatStatus,
  shortId,
  statusColor,
} from "../utils/format";

type TabId =
  | "browser"
  | "activity"
  | "map"
  | "pages"
  | "workflows"
  | "tests"
  | "bugs"
  | "evidence"
  | "docs";

const TABS: { id: TabId; label: string }[] = [
  { id: "browser", label: "Live Browser" },
  { id: "activity", label: "Activity" },
  { id: "map", label: "Application Map" },
  { id: "pages", label: "Pages" },
  { id: "workflows", label: "Workflows" },
  { id: "tests", label: "Tests" },
  { id: "bugs", label: "Bugs" },
  { id: "evidence", label: "Evidence" },
  { id: "docs", label: "Documentation" },
];

// Long enough to swallow the burst one action produces, short enough that the
// view still feels live — well inside the 3s heartbeat, so an event always
// arrives sooner than the next poll would have.
const EVENT_REFRESH_DEBOUNCE_MS = 700;

/** Is this payload the same data the view is already showing?
 *
 * A poll that returns identical rows must not hand React new object identities:
 * every list would re-render, every <img> would re-mount and reload, and the
 * Mermaid diagram would redraw. Compared by serialised value because these are
 * plain JSON payloads from the API and nothing in them is a function or a Date.
 */
export function sameData(previous: unknown, next: unknown): boolean {
  if (previous === next) return true;
  if (previous == null || next == null) return false;
  try {
    return JSON.stringify(previous) === JSON.stringify(next);
  } catch {
    return false;
  }
}

function bugFromApi(b: BugItem, runId: string): BugReportEntry {
  const p = b.payload || {};
  return {
    bug_id: b.id,
    title: b.title,
    module: String(p.module || ""),
    page: String(p.page || ""),
    url: String(p.url || ""),
    classification: String(p.classification || "suspected_bug"),
    severity: b.severity,
    priority: String(p.priority || "medium"),
    preconditions: Array.isArray(p.preconditions) ? (p.preconditions as string[]) : [],
    test_data: "",
    steps_to_reproduce: Array.isArray(p.steps) ? (p.steps as string[]) : [],
    expected_result: String(p.expected || p.expected_result || ""),
    actual_result: String(p.actual || p.actual_result || b.description),
    business_impact: String(p.business_impact || ""),
    possible_root_cause_hypothesis: String(p.possible_root_cause || ""),
    confidence: Number(p.confidence ?? 0.5),
    screenshot_evidence: Array.isArray(p.screenshot_evidence)
      ? (p.screenshot_evidence as string[])
      : [],
    trace_evidence: [],
    console_evidence: [],
    network_evidence: [],
    discovery_timestamp: null,
    run_id: runId,
  };
}

export function LiveRunPage() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunStatus | null>(null);
  const [pages, setPages] = useState<PageItem[]>([]);
  const [, setActions] = useState<ActionItem[]>([]);
  const [bugs, setBugs] = useState<BugReportEntry[]>([]);
  const [report, setReport] = useState<FinalReport | null>(null);
  const [evidence, setEvidence] = useState<EvidenceFile[]>([]);
  const [mermaid, setMermaid] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>("browser");
  const [controlBusy, setControlBusy] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  const [speed, setSpeed] = useState(DEFAULT_EXECUTION_SPEED);
  const [actionPause, setActionPause] = useState(DEFAULT_ACTION_PAUSE);
  const { events, connectionState, reconnecting } = useRunSocket(runId);

  const refresh = useCallback(async () => {
    if (!runId) return;
    try {
      const [r, p, a, b] = await Promise.all([
        api.getRun(runId),
        api.getPages(runId),
        api.getActions(runId),
        api.getBugs(runId),
      ]);
      // Replace state only when the payload actually differs. A poll that hands
      // React a brand-new array holding identical data still re-renders every
      // list, re-mounts every <img>, and re-draws the Mermaid diagram — visible
      // as a flicker even at one poll per three seconds.
      setRun((prev) => (sameData(prev, r) ? prev : r));
      if (typeof r.execution_speed === "number") setSpeed(r.execution_speed);
      if (typeof r.action_pause === "number") setActionPause(r.action_pause);
      setPages((prev) => (sameData(prev, p) ? prev : p));
      setActions((prev) => (sameData(prev, a) ? prev : a));
      const mapped = b.map((x) => bugFromApi(x, runId));
      setBugs((prev) => (sameData(prev, mapped) ? prev : mapped));
      setError(null);

      const terminal = ["completed", "failed", "cancelled", "stopped"].includes(r.status);
      if (terminal || env.demoMode) {
        try {
          const [rep, ev, nav] = await Promise.all([
            api.getReport(runId),
            api.getEvidence(runId),
            api.getNavigationMermaid(runId),
          ]);
          setReport((prev) => (sameData(prev, rep) ? prev : rep));
          setEvidence((prev) => (sameData(prev, ev) ? prev : ev));
          setMermaid(nav || rep.navigation_structure || "");
          if (rep.confirmed_bugs?.length || rep.suspected_bugs?.length) {
            const fromReport = [...(rep.confirmed_bugs || []), ...(rep.suspected_bugs || [])];
            setBugs((prev) => (sameData(prev, fromReport) ? prev : fromReport));
          }
        } catch {
          // report may not exist yet mid-run
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load run");
    }
  }, [runId]);

  // The steady heartbeat. Deliberately does NOT depend on `events.length`: it
  // used to, and because the effect body refreshes immediately on every re-run,
  // each websocket event tore down the interval, fired a refresh, and rebuilt it.
  // Measured on run 5b4268e0 — 1366 events in 521s, so ~1400 refreshes and
  // ~5500 HTTP calls, at 2.6 whole-page re-renders per second for the length of
  // the run. That is the flicker operators have seen since the first version.
  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(t);
  }, [refresh]);

  // Events still make the view react faster than the heartbeat, but a burst of
  // them now collapses into ONE refresh. Every action emits roughly a dozen
  // events within a second or two, and they all describe the same step.
  useEffect(() => {
    if (!events.length) return;
    const t = window.setTimeout(() => void refresh(), EVENT_REFRESH_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [events.length, refresh]);

  const latest = events[events.length - 1];
  const productName =
    report?.product_overview?.product_name ||
    pages[0]?.title?.split("|")[0]?.trim() ||
    "Under analysis";
  const currentPageType = useMemo(() => {
    const fromEvent = latest?.type === "page_observed" ? String(latest.payload.page_type || "") : "";
    if (fromEvent) return fromEvent;
    const match = pages.find((p) => p.url === run?.current_url);
    return match?.page_type || "unknown";
  }, [latest, pages, run?.current_url]);

  const currentModule = useMemo(() => {
    const mods: ModuleRecord[] = report?.modules || [];
    const page = pages.find((p) => p.url === run?.current_url);
    if (!page) return mods[0]?.name || "—";
    const hit = mods.find((m) => m.page_ids.includes(page.id));
    return hit?.name || page.page_type || "—";
  }, [pages, report?.modules, run?.current_url]);

  const expectedResult = String(
    latest?.payload.expected_result || latest?.payload.reason || "—"
  );
  const decisionSummary = String(
    latest?.payload.reason ||
      latest?.payload.message ||
      run?.message ||
      "Waiting for agent…"
  );
  const observation = String(
    latest?.type === "page_observed"
      ? `${latest.payload.title || ""} @ ${latest.payload.url || ""}`
      : run?.message || "—"
  );

  const screenshotUrl = useMemo(() => {
    if (env.demoMode) return demoPlaceholderScreenshot;
    const shot = evidence.find((e) => e.kind === "screenshot");
    if (shot?.url?.startsWith("data:")) return shot.url;
    if (shot) return api.evidenceFileUrl(runId, shot.path);
    return null;
  }, [evidence, runId]);

  const workflows: Workflow[] = report?.workflows || [];
  const tests: TestScenario[] = report?.test_scenarios || [];
  const modules: ModuleRecord[] = report?.modules || [];
  const consoleErrors = report?.console_errors || [];
  const networkFailures = report?.network_errors || [];

  if (error) return <FailureState title="Run failed to load" detail={error} onRetry={refresh} />;
  if (!run) return <LoadingState label="Loading live run…" />;

  const done = ["completed", "failed", "cancelled", "stopped"].includes(run.status);
  const paused = run.status === "paused";

  const control = async (fn: () => Promise<unknown>) => {
    setControlBusy(true);
    setControlError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setControlError(e instanceof Error ? e.message : "Run control failed");
    } finally {
      setControlBusy(false);
    }
  };

  return (
    <div className="space-y-4 animate-rise">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to="/history" className="text-sm text-slate-500 hover:text-tide-400">
            ← History
          </Link>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <h1 className="font-display text-3xl text-white">
              Run {shortId(run.run_id)}
            </h1>
            {env.demoMode ? <DemoBadge /> : null}
          </div>
          <p className={`mt-1 text-sm uppercase tracking-wider ${statusColor(run.status)}`}>
            {formatStatus(run.status)}
            <span className="ml-3 font-mono normal-case tracking-normal text-slate-500">
              ws {connectionLabel(connectionState)}
            </span>
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {!done && !paused ? (
            <button
              type="button"
              className="btn-ghost"
              disabled={controlBusy}
              onClick={() => void control(() => api.pauseRun(runId))}
            >
              Pause
            </button>
          ) : null}
          {paused ? (
            <button
              type="button"
              className="btn-primary"
              disabled={controlBusy}
              onClick={() => void control(() => api.resumeRun(runId))}
            >
              Continue
            </button>
          ) : null}
          {!done ? (
            <button
              type="button"
              className="btn-ghost border-rose-400/40 text-rose-300"
              disabled={controlBusy}
              onClick={() => void control(() => api.cancelRun(runId))}
            >
              End run
            </button>
          ) : null}
          <Link to={`/runs/new?from=${runId}`} className="btn-ghost">
            Test again
          </Link>
          <Link to={`/runs/${runId}/report`} className="btn-primary">
            Final report
          </Link>
        </div>
      </div>

      {reconnecting ? <ReconnectingBanner /> : null}
      {controlError ? (
        <div className="rounded-xl border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">
          {controlError}
        </div>
      ) : null}
      {paused ? (
        <div className="rounded-xl border border-amber-400/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
          Run paused. Continue to resume, or End run when you are done.
        </div>
      ) : null}

      <div className="flex gap-2 overflow-x-auto pb-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={["tab whitespace-nowrap", tab === t.id ? "tab-active" : ""].join(" ")}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-[260px_minmax(0,1fr)_280px]">
        {/* Left */}
        <aside className="space-y-3">
          <div className="surface space-y-3 p-4">
            <Meta label="Run status" value={formatStatus(run.status)} />
            <Meta
              label="Product name"
              value={productName}
              inferred={!report?.product_overview}
            />
            <Meta label="Current URL" value={run.current_url || run.url} mono />
            <Meta label="Current module" value={currentModule} inferred />
            <Meta label="Current page type" value={currentPageType} inferred />
            <div>
              <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Progress</p>
              <div className="mt-2">
                <ProgressBar value={run.progress_pct} />
              </div>
              <p className="mt-1 text-sm text-slate-300">{Math.round(run.progress_pct)}%</p>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <Stat label="Actions" value={run.actions_taken} />
            <Stat label="Pages" value={run.pages_visited} />
            <Stat label="Bugs" value={run.bugs_found} />
          </div>
          <ExecutionPacingControls
            speed={speed}
            pause={actionPause}
            disabled={done || controlBusy}
            onSpeed={(value) => {
              setSpeed(value);
              void control(() => api.setRunPacing(runId, { execution_speed: value }));
            }}
            onPause={(value) => {
              setActionPause(value);
              void control(() => api.setRunPacing(runId, { action_pause: value }));
            }}
          />
        </aside>

        {/* Center */}
        <section className="space-y-4">
          {tab === "browser" || tab === "activity" ? (
            <>
              <div className="surface p-4">
                <ScreenshotViewer
                  src={screenshotUrl}
                  url={run.current_url || run.url}
                  timestamp={latest?.timestamp || run.started_at}
                />
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <LiveActionStatus events={events} runStatus={run.status} />
                <Panel title="Expected result" body={expectedResult} />
                <Panel title="Agent decision summary" body={decisionSummary} />
                <Panel title="Current observation" body={observation} />
              </div>
              <ActivityLog runId={runId || ""} events={events} />
            </>
          ) : null}

          {tab === "map" ? (
            <div className="surface p-4">
              <MermaidDiagram chart={mermaid} title="Application navigation map" />
            </div>
          ) : null}

          {tab === "pages" ? (
            <div className="surface divide-y divide-white/5">
              {pages.map((p) => (
                <div key={p.id} className="px-4 py-3">
                  <p className="font-medium text-white">{p.title || "(untitled)"}</p>
                  <p className="font-mono text-xs text-slate-500">{p.url}</p>
                  <p className="mt-1 text-xs text-slate-400">
                    Type: {p.page_type || "unknown"}
                    <InferredLabel />
                  </p>
                </div>
              ))}
              {!pages.length ? <EmptyState title="No pages discovered yet" /> : null}
            </div>
          ) : null}

          {tab === "workflows" ? (
            <div className="space-y-4">
              {workflows.map((wf) => (
                <div key={wf.workflow_id} className="surface p-4">
                  <h3 className="font-medium text-white">
                    {wf.name}
                    <InferredLabel />
                  </h3>
                  <p className="mt-1 text-sm text-slate-400">{wf.description}</p>
                  <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm text-slate-300">
                    {wf.steps.map((s) => (
                      <li key={s.step_id}>
                        {s.action} — {s.description}
                      </li>
                    ))}
                  </ol>
                  {wf.mermaid ? (
                    <div className="mt-4">
                      <MermaidDiagram chart={wf.mermaid} />
                    </div>
                  ) : null}
                </div>
              ))}
              {!workflows.length ? (
                <EmptyState title="No workflows cataloged yet" detail="They appear as exploration progresses." />
              ) : null}
            </div>
          ) : null}

          {tab === "tests" ? (
            <div className="space-y-3">
              {tests.map((t) => (
                <Link
                  key={t.test_id}
                  to={`/runs/${runId}/tests/${t.test_id}`}
                  className="surface block p-4 hover:border-tide-500/30"
                >
                  <p className="font-mono text-xs text-slate-500">{t.test_id}</p>
                  <p className="mt-1 font-medium text-white">{t.title}</p>
                  <p className="mt-1 text-xs uppercase tracking-wider text-slate-500">
                    {t.category} · {t.priority}
                  </p>
                </Link>
              ))}
              {!tests.length ? <EmptyState title="No tests generated yet" /> : null}
            </div>
          ) : null}

          {tab === "bugs" ? (
            <div className="space-y-3">
              {bugs.map((b) => (
                <BugCard key={b.bug_id} bug={b} runId={runId} />
              ))}
              {!bugs.length ? <EmptyState title="No bugs recorded yet" /> : null}
            </div>
          ) : null}

          {tab === "evidence" ? (
            <div className="space-y-3">
              {evidence.map((e) => (
                <Link
                  key={e.path}
                  to={`/runs/${runId}/evidence?path=${encodeURIComponent(e.path)}`}
                  className="surface flex items-center justify-between gap-3 p-4 hover:border-tide-500/30"
                >
                  <div>
                    <p className="font-medium text-white">{e.path}</p>
                    <p className="text-xs uppercase tracking-wider text-slate-500">{e.kind}</p>
                  </div>
                  <span className="text-xs text-slate-500">{e.size} B</span>
                </Link>
              ))}
              {!evidence.length ? (
                <EmptyState title="No evidence files yet" detail="Screenshots and traces appear during the run." />
              ) : null}
            </div>
          ) : null}

          {tab === "docs" ? (
            <div className="surface space-y-4 p-4">
              {Object.entries(report?.sections_markdown || {}).map(([title, body]) => (
                <article key={title}>
                  <h3 className="font-display text-lg text-white">{title}</h3>
                  <pre className="mt-2 whitespace-pre-wrap font-sans text-sm text-slate-400">
                    {body}
                  </pre>
                </article>
              ))}
              {!report?.sections_markdown ||
              !Object.keys(report.sections_markdown).length ? (
                <EmptyState title="Documentation syncing…" detail="Sections update as the agent explores." />
              ) : null}
            </div>
          ) : null}
        </section>

        {/* Right */}
        <aside className="space-y-3">
          <SideList
            title="Pages discovered"
            items={pages.map((p) => p.title || p.url)}
          />
          <SideList title="Modules discovered" items={modules.map((m) => m.name)} inferred />
          <SideList
            title="Workflows discovered"
            items={workflows.map((w) => w.name)}
            inferred
          />
          <SideList title="Bugs" items={bugs.map((b) => b.title)} />
          <SideList title="Console errors" items={consoleErrors} />
          <SideList title="Network failures" items={networkFailures} />
        </aside>
      </div>
    </div>
  );
}

function Meta({
  label,
  value,
  mono,
  inferred,
}: {
  label: string;
  value: string;
  mono?: boolean;
  inferred?: boolean;
}) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">
        {label}
        {inferred ? <InferredLabel /> : null}
      </p>
      <p className={`mt-1 break-all text-sm text-slate-200 ${mono ? "font-mono text-xs" : ""}`}>
        {value}
      </p>
    </div>
  );
}

function Panel({ title, body }: { title: string; body: string }) {
  return (
    <div className="surface p-4">
      <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">{title}</p>
      <p className="mt-2 text-sm text-slate-200">{body}</p>
    </div>
  );
}

function SideList({
  title,
  items,
  inferred,
}: {
  title: string;
  items: string[];
  inferred?: boolean;
}) {
  return (
    <div className="surface p-4">
      <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">
        {title}
        {inferred ? <InferredLabel /> : null}
      </p>
      <ul className="mt-2 max-h-36 space-y-1 overflow-y-auto text-sm text-slate-300">
        {items.slice(0, 12).map((item, i) => (
          <li key={`${item}-${i}`} className="truncate">
            {item}
          </li>
        ))}
        {!items.length ? <li className="text-slate-600">None yet</li> : null}
      </ul>
    </div>
  );
}

import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { DemoBadge } from "../components/DemoBadge";
import { ScreenshotViewer } from "../components/ScreenshotViewer";
import { EmptyState, FailureState, LoadingState } from "../components/States";
import { demoPlaceholderScreenshot } from "../demo/fixtures";
import { api, env } from "../services/api";
import type { EvidenceFile } from "../types";

export function EvidenceViewerPage() {
  const { runId = "" } = useParams();
  const [params] = useSearchParams();
  const selectedPath = params.get("path") || "";
  const [files, setFiles] = useState<EvidenceFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getEvidence(runId)
      .then((list) => {
        if (alive) setFiles(list);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "Failed to load evidence");
      });
    return () => {
      alive = false;
    };
  }, [runId]);

  const active = useMemo(() => {
    if (!files?.length) return null;
    return files.find((f) => f.path === selectedPath) || files[0];
  }, [files, selectedPath]);

  if (error) return <FailureState title="Evidence unavailable" detail={error} />;
  if (!files) return <LoadingState label="Loading evidence…" />;
  if (!files.length) {
    return (
      <EmptyState
        title="No evidence for this run"
        detail="Screenshots and traces appear as the agent explores."
      />
    );
  }

  const isImage = active && (active.kind === "screenshot" || /\.(png|jpe?g|webp)$/i.test(active.path));
  const src = env.demoMode
    ? demoPlaceholderScreenshot
    : active
      ? api.evidenceFileUrl(runId, active.path)
      : null;

  return (
    <div className="space-y-6 animate-rise">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link to={`/runs/${runId}`} className="text-sm text-slate-500 hover:text-tide-400">
            ← Live dashboard
          </Link>
          <div className="mt-2 flex items-center gap-2">
            <h1 className="font-display text-3xl text-white">Evidence viewer</h1>
            {env.demoMode ? <DemoBadge /> : null}
          </div>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
        <aside className="surface max-h-[70vh] overflow-y-auto p-2">
          {files.map((f) => (
            <Link
              key={f.path}
              to={`/runs/${runId}/evidence?path=${encodeURIComponent(f.path)}`}
              className={[
                "block rounded-lg px-3 py-2 text-sm",
                active?.path === f.path
                  ? "bg-white/10 text-white"
                  : "text-slate-400 hover:bg-white/5",
              ].join(" ")}
            >
              <p className="truncate font-medium">{f.path}</p>
              <p className="text-[10px] uppercase tracking-wider text-slate-500">{f.kind}</p>
            </Link>
          ))}
        </aside>

        <section className="surface p-4">
          {active ? (
            <>
              <p className="font-mono text-xs text-slate-500">{active.path}</p>
              {isImage ? (
                <div className="mt-4">
                  <ScreenshotViewer src={src} url={active.path} />
                </div>
              ) : (
                <div className="mt-4 space-y-3">
                  <p className="text-sm text-slate-400">
                    Non-image evidence. Open the file directly if the backend serves it.
                  </p>
                  <a className="btn-primary inline-flex" href={src || "#"} target="_blank" rel="noreferrer">
                    Open file
                  </a>
                </div>
              )}
            </>
          ) : null}
        </section>
      </div>
    </div>
  );
}

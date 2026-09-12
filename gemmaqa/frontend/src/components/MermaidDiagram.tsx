import { useEffect, useId, useRef, useState } from "react";
import mermaid from "mermaid";

let mermaidReady = false;

function ensureMermaid() {
  if (mermaidReady) return;
  mermaid.initialize({
    startOnLoad: false,
    theme: "dark",
    securityLevel: "strict",
    fontFamily: "IBM Plex Mono, ui-monospace, monospace",
  });
  mermaidReady = true;
}

/** Sanitize already-sanitized diagrams; still escape accidental HTML. */
export function MermaidDiagram({
  chart,
  title,
}: {
  chart: string;
  title?: string;
}) {
  const id = useId().replace(/:/g, "");
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function render() {
      if (!chart?.trim() || !ref.current) return;
      ensureMermaid();
      setError(null);
      try {
        const { svg } = await mermaid.render(`mmd-${id}-${Date.now()}`, chart);
        if (!cancelled && ref.current) {
          ref.current.innerHTML = svg;
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to render diagram");
          if (ref.current) ref.current.innerHTML = "";
        }
      }
    }
    void render();
    return () => {
      cancelled = true;
    };
  }, [chart, id]);

  if (!chart?.trim()) {
    return (
      <p className="text-sm text-slate-500">No diagram available yet.</p>
    );
  }

  return (
    <div className="space-y-2">
      {title ? <h3 className="text-sm font-medium text-slate-300">{title}</h3> : null}
      {error ? (
        <pre className="overflow-auto rounded-xl bg-ink-950/80 p-3 text-xs text-rose-300">
          {error}
          {"\n\n"}
          {chart}
        </pre>
      ) : (
        <div ref={ref} className="mermaid-host overflow-x-auto rounded-xl bg-ink-950/40 p-3" />
      )}
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { api, parseApiError } from "../services/api";
import { env } from "../config/env";
import type { ProviderCatalog } from "../types";

export const PROVIDER_CHANGED_EVENT = "gemmaqa:provider-changed";

function emitCatalog(catalog: ProviderCatalog) {
  window.dispatchEvent(new CustomEvent(PROVIDER_CHANGED_EVENT, { detail: catalog }));
}

export function ProviderSwitcher({
  variant = "header",
}: {
  variant?: "header" | "settings";
}) {
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await api.listProviders();
      setCatalog(next);
      setError(null);
    } catch (err) {
      setError(env.demoMode ? null : parseApiError(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    function onChanged(event: Event) {
      const detail = (event as CustomEvent<ProviderCatalog>).detail;
      if (detail) setCatalog(detail);
    }
    window.addEventListener(PROVIDER_CHANGED_EVENT, onChanged);
    return () => window.removeEventListener(PROVIDER_CHANGED_EVENT, onChanged);
  }, []);

  async function onChange(provider: string) {
    if (!catalog || provider === catalog.active) return;
    setSaving(true);
    setError(null);
    try {
      const next = await api.setProvider(provider);
      setCatalog(next);
      emitCatalog(next);
      if (next.warning) setError(next.warning);
    } catch (err) {
      setError(parseApiError(err));
    } finally {
      setSaving(false);
    }
  }

  if (!catalog) {
    if (variant === "header") {
      return (
        <span className="hidden text-[11px] text-slate-500 sm:inline">
          {error || "Models…"}
        </span>
      );
    }
    return <p className="text-sm text-slate-500">{error || "Loading models…"}</p>;
  }

  const active = catalog.providers.find((item) => item.id === catalog.active);

  if (variant === "header") {
    return (
      <label className="flex min-w-0 max-w-[18rem] items-center gap-2">
        <span className="hidden shrink-0 text-[10px] uppercase tracking-[0.16em] text-slate-500 lg:inline">
          Model
        </span>
        <select
          className="field max-w-[16rem] py-1.5 text-xs"
          value={catalog.active}
          disabled={saving || env.demoMode}
          title={
            env.demoMode
              ? "Demo mode uses Mock fixture data"
              : "Applies to the next New Run. Configure missing models in backend .env."
          }
          aria-label="Active LLM"
          onChange={(e) => void onChange(e.target.value)}
        >
          {catalog.providers.map((item) => (
            <option key={item.id} value={item.id} disabled={!item.selectable}>
              {item.label}
              {item.selectable ? "" : " (not configured)"}
            </option>
          ))}
        </select>
        {error ? (
          <span className="hidden max-w-[12rem] truncate text-[11px] text-amber-300 xl:inline" title={error}>
            {error}
          </span>
        ) : null}
      </label>
    );
  }

  return (
    <div className="space-y-3">
      <select
        className="field"
        value={catalog.active}
        disabled={saving || env.demoMode}
        aria-label="Active LLM"
        onChange={(e) => void onChange(e.target.value)}
      >
        {catalog.providers.map((item) => (
          <option key={item.id} value={item.id} disabled={!item.selectable}>
            {item.label}
            {item.selectable ? "" : " — not configured"}
          </option>
        ))}
      </select>
      <p className="text-sm text-slate-300">
        Active: <span className="text-white">{catalog.active_label}</span>
        {active?.model_id ? (
          <span className="font-mono text-xs text-slate-400"> · {active.model_id}</span>
        ) : null}
      </p>
      <p className="text-xs text-slate-500">
        {catalog.run_mode}. Applies to the next New Run
        {catalog.active_runs
          ? ` (${catalog.active_runs} run${catalog.active_runs === 1 ? "" : "s"} already in progress will keep the previous model)`
          : ""}
        . Default from <code className="text-slate-300">GEMMA_PROVIDER</code>: {catalog.env_default}.
      </p>
      <ul className="space-y-2 text-xs text-slate-400">
        {catalog.providers.map((item) => (
          <li
            key={item.id}
            className="rounded-xl border border-white/5 px-3 py-2"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-slate-200">{item.label}</span>
              <span className={item.configured ? "text-tide-400" : "text-amber-300"}>
                {item.configured ? "ready" : "needs .env"}
              </span>
            </div>
            <p className="mt-1">{item.description}</p>
            <p className="mt-0.5 text-slate-500">{item.note}</p>
          </li>
        ))}
      </ul>
      {env.demoMode ? (
        <p className="text-xs text-slate-500">Demo mode always uses Mock fixture data.</p>
      ) : null}
      {error ? <p className="text-sm text-amber-200">{error}</p> : null}
    </div>
  );
}

import { useEffect, useState } from "react";
import { env } from "../config/env";
import { api } from "../services/api";
import { DemoBadge } from "../components/DemoBadge";
import { ProviderSwitcher } from "../components/ProviderSwitcher";
import type { StrandsStatus } from "../types";

type Theme = "dark" | "light";

export function SettingsPage() {
  const [theme, setTheme] = useState<Theme>(() => {
    return (localStorage.getItem("gemmaqa.theme") as Theme) || "dark";
  });
  const [health, setHealth] = useState<string>("…");
  const [strands, setStrands] = useState<StrandsStatus | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("gemmaqa.theme", theme);
  }, [theme]);

  useEffect(() => {
    api
      .health()
      .then((h) => setHealth(`${h.app} ${h.version} · ${h.status}`))
      .catch(() => setHealth(env.demoMode ? "Demo mode (no backend required)" : "Unreachable"));
    api
      .strandsStatus()
      .then((status) => setStrands(status))
      .catch(() => setStrands(null));
  }, []);

  return (
    <div className="mx-auto max-w-2xl space-y-6 animate-rise">
      <div className="flex items-center gap-3">
        <h1 className="font-display text-3xl text-white">Settings</h1>
        {env.demoMode ? <DemoBadge /> : null}
      </div>

      <section className="surface space-y-4 p-6">
        <h2 className="text-sm font-medium text-slate-200">Appearance</h2>
        <div className="flex gap-2">
          {(["dark", "light"] as Theme[]).map((t) => (
            <button
              key={t}
              type="button"
              className={theme === t ? "btn-primary" : "btn-ghost"}
              onClick={() => setTheme(t)}
            >
              {t === "dark" ? "Dark" : "Light"}
            </button>
          ))}
        </div>
        <p className="text-xs text-slate-500">
          Light theme uses a muted professional palette. Prefer dark for demos.
        </p>
      </section>

      <section className="surface space-y-4 p-6">
        <h2 className="text-sm font-medium text-slate-200">Language model</h2>
        <p className="text-xs text-slate-500">
          Switch among implemented backends. New models appear here automatically once they are
          registered in the backend catalog. Secrets stay in <code className="text-slate-300">.env</code>.
        </p>
        <ProviderSwitcher variant="settings" />
      </section>

      <section className="surface space-y-3 p-6">
        <h2 className="text-sm font-medium text-slate-200">AWS Strands coordinator</h2>
        <p className="text-xs text-slate-500">
          Official <code className="text-slate-300">strands-agents</code> SDK. It launches the run;
          the Model dropdown still does page-by-page QA reasoning. Turn it on from New Run.
        </p>
        <dl className="space-y-2 text-sm">
          <Row label="SDK" value={strands?.sdk || "strands-agents"} />
          <Row label="Installed" value={strands?.installed ? "yes" : "no"} />
          <Row
            label="Configured"
            value={
              strands?.configured
                ? `${strands.model_id || "model"} · ${strands.aws_region || "region"}`
                : "needs AWS_REGION + BEDROCK_MODEL_ID"
            }
          />
        </dl>
      </section>

      <section className="surface space-y-3 p-6">
        <h2 className="text-sm font-medium text-slate-200">Environment</h2>
        <dl className="space-y-2 text-sm">
          <Row label="API base" value={env.apiBaseUrl || "(Vite proxy / same origin)"} />
          <Row
            label="WS base"
            value={env.wsBaseUrl || env.wsHost || "(same origin /ws)"}
          />
          <Row label="Demo mode" value={env.demoMode ? "ON — fixture data" : "OFF"} />
          <Row label="Backend health" value={health} />
        </dl>
        <p className="text-xs text-slate-500">
          Configure via <code className="text-slate-300">VITE_API_BASE_URL</code>,{" "}
          <code className="text-slate-300">VITE_WS_BASE_URL</code>, and{" "}
          <code className="text-slate-300">VITE_DEMO_MODE</code>.
        </p>
      </section>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-wrap justify-between gap-2 border-b border-white/5 py-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className="font-mono text-xs text-slate-300">{value}</dd>
    </div>
  );
}

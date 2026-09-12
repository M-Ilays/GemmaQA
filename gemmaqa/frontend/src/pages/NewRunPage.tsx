import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, env, parseApiError } from "../services/api";
import type { CreateRunRequest, ProviderCatalog, StrandsStatus } from "../types";
import { DemoBadge } from "../components/DemoBadge";
import { PROVIDER_CHANGED_EVENT } from "../components/ProviderSwitcher";
import { emptyRunForm, formFromPreviousRun, rememberSubmittedRun } from "../utils/runDraft";
import { shortId } from "../utils/format";

export function NewRunPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const fromRunId = searchParams.get("from") || "";
  const [form, setForm] = useState<CreateRunRequest>(emptyRunForm);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reuseNote, setReuseNote] = useState<string | null>(null);
  const [providerLabel, setProviderLabel] = useState<string | null>(null);
  const [strands, setStrands] = useState<StrandsStatus | null>(null);
  const [useStrands, setUseStrands] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .listProviders()
      .then((catalog: ProviderCatalog) => {
        if (!cancelled) setProviderLabel(catalog.active_label);
      })
      .catch(() => {
        if (!cancelled) setProviderLabel(null);
      });
    function onChanged(event: Event) {
      const detail = (event as CustomEvent<ProviderCatalog>).detail;
      if (detail) setProviderLabel(detail.active_label);
    }
    window.addEventListener(PROVIDER_CHANGED_EVENT, onChanged);
    api
      .strandsStatus()
      .then((status) => {
        if (cancelled || !status) return;
        setStrands(status);
        setUseStrands(Boolean(status.configured));
      })
      .catch(() => {
        if (!cancelled) setStrands(null);
      });
    return () => {
      cancelled = true;
      window.removeEventListener(PROVIDER_CHANGED_EVENT, onChanged);
    };
  }, []);

  useEffect(() => {
    if (!fromRunId) {
      setReuseNote(null);
      return;
    }
    let cancelled = false;
    api
      .getRun(fromRunId)
      .then((run) => {
        if (cancelled) return;
        setForm(formFromPreviousRun(run));
        setReuseNote(
          `Settings copied from run ${shortId(run.run_id)}. Change anything, then Start.`,
        );
        setError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Could not load the previous run");
      });
    return () => {
      cancelled = true;
    };
  }, [fromRunId]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!form.authorization_ack) {
      setError("You must confirm you are authorized to test this system.");
      return;
    }
    setSubmitting(true);
    setError(null);
    const passwordSnapshot = form.password;
    try {
      const {
        max_actions: _ignoredActions,
        max_pages: _ignoredPages,
        max_runtime_seconds: _ignoredRuntime,
        ...cfg
      } = form.configuration;
      const payload: CreateRunRequest = {
        url: form.url,
        username: form.username || undefined,
        password: passwordSnapshot || undefined,
        notes: form.notes || undefined,
        authorization_ack: true,
        auto_start: true,
        configuration: {
          ...cfg,
          username_selector: form.configuration.username_selector || null,
          password_selector: form.configuration.password_selector || null,
          submit_selector: form.configuration.submit_selector || null,
          login_url: form.configuration.login_url || null,
          // null, not "". RunConfiguration.testing_objective treats None as
          // "the operator did not provide one" and "" as "the operator cleared
          // it", and the model is told which — an empty string here would
          // assert an explicit choice the operator never made.
          testing_objective: form.configuration.testing_objective?.trim() || null,
        },
      };
      const res = await api.createRun(payload, { strands: useStrands && Boolean(strands?.configured) });
      rememberSubmittedRun({ ...payload, password: passwordSnapshot });
      // Clear password from component state immediately after acceptance
      // Never write credentials to localStorage or sessionStorage
      setForm((prev) => ({ ...prev, password: "", authorization_ack: false }));
      navigate(`/runs/${res.run_id}`);
    } catch (err) {
      setForm((prev) => ({ ...prev, password: "" }));
      setError(parseApiError(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 animate-rise">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="font-display text-3xl text-white">New QA run</h1>
          <p className="mt-2 text-slate-400">
            Authorize a target URL. Credentials are sent once and never stored in the browser.
            {providerLabel ? (
              <>
                {" "}
                This run will use <span className="text-slate-200">{providerLabel}</span>
                {" "}(change it from the header or Settings).
              </>
            ) : null}
          </p>
        </div>
        {env.demoMode ? <DemoBadge /> : null}
      </div>

      <aside className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100/90">
        <strong className="font-medium text-amber-50">Legal notice:</strong> Only test systems you
        own or are explicitly authorized to test. Unauthorized testing may be illegal.
      </aside>

      <aside className="rounded-xl border border-tide-500/30 bg-tide-500/10 px-4 py-3 text-sm text-tide-100/90">
        For richer modules and navigation maps, start at the app root (e.g. Contact List home) and
        enable <em>controlled writes</em> only when you authorize test sign-up/login. Safe mode
        alone will not submit forms or reach authenticated modules.
      </aside>

      {reuseNote ? (
        <aside className="rounded-xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100/90">
          {reuseNote}{" "}
          Password is reused only if you typed it in this tab for the same URL and username; it is
          never saved on disk.
        </aside>
      ) : null}

      <form onSubmit={onSubmit} className="surface space-y-5 p-6" autoComplete="off">
        <Field label="Website URL" required>
          <input
            required
            className="field"
            value={form.url}
            onChange={(e) => setForm({ ...form, url: e.target.value })}
            placeholder="https://thinking-tester-contact-list.herokuapp.com/"
          />
        </Field>

        <Field label="Testing objective">
          <textarea
            className="field min-h-[88px]"
            value={form.configuration.testing_objective || ""}
            onChange={(e) =>
              setForm({
                ...form,
                // This has to reach `configuration.testing_objective` — that is
                // the value carried into every model prompt. It used to write to
                // `notes` (a free-text annotation on the run record), so a field
                // labelled "Testing objective" set no objective at all and every
                // run reported `testing_objective: null`. Also mirrored into
                // `notes` so the run record keeps showing it.
                notes: e.target.value,
                configuration: { ...form.configuration, testing_objective: e.target.value },
              })
            }
            placeholder="e.g. Exercise the contact CRUD features"
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Username">
            <input
              className="field"
              autoComplete="username"
              value={form.username || ""}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
            />
          </Field>
          <Field label="Password">
            <input
              type="password"
              className="field"
              autoComplete="new-password"
              value={form.password || ""}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
            />
          </Field>
        </div>
        <p className="text-xs text-slate-500">
          Password is masked, never written to localStorage, and cleared from state after the run is
          accepted.
        </p>

        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Username selector">
            <input
              className="field font-mono text-xs"
              value={form.configuration.username_selector || ""}
              onChange={(e) =>
                setForm({
                  ...form,
                  configuration: { ...form.configuration, username_selector: e.target.value },
                })
              }
              placeholder="#username"
            />
          </Field>
          <Field label="Password selector">
            <input
              className="field font-mono text-xs"
              value={form.configuration.password_selector || ""}
              onChange={(e) =>
                setForm({
                  ...form,
                  configuration: { ...form.configuration, password_selector: e.target.value },
                })
              }
              placeholder="#password"
            />
          </Field>
          <Field label="Submit selector">
            <input
              className="field font-mono text-xs"
              value={form.configuration.submit_selector || ""}
              onChange={(e) =>
                setForm({
                  ...form,
                  configuration: { ...form.configuration, submit_selector: e.target.value },
                })
              }
              placeholder="button[type=submit]"
            />
          </Field>
        </div>

        <Toggle
          label="Coordinate with AWS Strands"
          hint={
            strands?.configured
              ? `Official Strands Agents SDK starts this run (Bedrock ${strands.model_id || "coordinator"}). The Model dropdown still does the page-by-page QA reasoning.`
              : "Needs AWS_REGION and BEDROCK_MODEL_ID (or STRANDS_MODEL_ID). The browser engine is unchanged."
          }
          checked={useStrands && Boolean(strands?.configured)}
          disabled={!strands?.configured || env.demoMode}
          onChange={(v) => setUseStrands(v && Boolean(strands?.configured))}
        />

        <div className="grid gap-3 sm:grid-cols-2">
          <Toggle
            label="Headless mode"
            checked={form.configuration.headless}
            onChange={(v) =>
              setForm({ ...form, configuration: { ...form.configuration, headless: v } })
            }
          />
          <Toggle
            label="Safe mode"
            hint="Extra caution while exploring. Does not by itself allow any write below."
            checked={form.configuration.safe_mode}
            onChange={(v) =>
              setForm({ ...form, configuration: { ...form.configuration, safe_mode: v } })
            }
          />
        </div>

        <fieldset className="rounded-xl border border-white/10 bg-ink-950/40 px-3 py-3">
          <legend className="label px-1">Write permissions</legend>
          <p className="mb-3 px-1 text-xs text-slate-400">
            GemmaQA only reads unless you allow it to write. To test create/read/update/delete
            behaviour it has to be able to submit a form and create a record.
          </p>
          <div className="grid gap-3">
            <Toggle
              label="Allow controlled writes"
              hint="Submit forms the application already presents."
              checked={form.configuration.allow_controlled_writes}
              onChange={(v) =>
                setForm({
                  ...form,
                  configuration: { ...form.configuration, allow_controlled_writes: v },
                })
              }
            />
            <Toggle
              label="Allow test-data creation"
              hint="Create records, clearly tagged as GemmaQA test data. Required for any CRUD testing."
              checked={Boolean(form.configuration.allow_safe_test_data_creation)}
              onChange={(v) =>
                setForm({
                  ...form,
                  configuration: { ...form.configuration, allow_safe_test_data_creation: v },
                })
              }
            />
            <Toggle
              label="Allow deletion of GemmaQA's own test records"
              hint="Lets GemmaQA remove the records it created, instead of leaving them behind. It never deletes data it did not create."
              danger
              checked={Boolean(form.configuration.allow_destructive_actions)}
              onChange={(v) =>
                setForm({
                  ...form,
                  configuration: { ...form.configuration, allow_destructive_actions: v },
                })
              }
            />
          </div>
        </fieldset>

        <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-white/10 bg-ink-950/40 px-3 py-3 text-sm text-slate-300">
          <input
            type="checkbox"
            className="mt-0.5 h-4 w-4 accent-tide-500"
            checked={form.authorization_ack}
            onChange={(e) => setForm({ ...form, authorization_ack: e.target.checked })}
            required
          />
          <span>
            I confirm I own this system or have explicit authorization to perform exploratory QA
            testing against it.
          </span>
        </label>

        {error ? <p className="text-sm text-rose-300">{error}</p> : null}

        <button
          type="submit"
          className="btn-primary w-full sm:w-auto"
          disabled={submitting || !form.authorization_ack}
        >
          {submitting ? "Starting…" : "Start"}
        </button>
      </form>
    </div>
  );
}

function Field({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="label">
        {label}
        {required ? " *" : ""}
      </span>
      {children}
    </label>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  hint,
  danger,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  hint?: string;
  danger?: boolean;
  disabled?: boolean;
}) {
  return (
    <label
      className={`flex items-start justify-between gap-3 rounded-xl border px-3 py-3 text-sm text-slate-300 ${
        disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer"
      } ${danger ? "border-amber-400/30 bg-amber-400/5" : "border-white/10 bg-ink-950/40"}`}
    >
      <span>
        {label}
        {hint ? <span className="mt-1 block text-xs text-slate-400">{hint}</span> : null}
      </span>
      <input
        type="checkbox"
        className={`mt-0.5 h-4 w-4 shrink-0 ${danger ? "accent-amber-400" : "accent-tide-500"}`}
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
    </label>
  );
}

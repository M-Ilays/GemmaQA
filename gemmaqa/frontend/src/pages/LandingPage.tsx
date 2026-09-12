import { Link } from "react-router-dom";
import { env } from "../config/env";
import { DemoBadge } from "../components/DemoBadge";

export function LandingPage() {
  return (
    <div className="space-y-16">
      <section className="relative -mx-4 overflow-hidden px-4 sm:-mx-6 sm:px-6">
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,_rgba(20,184,166,0.22),_transparent_55%)]" />
        <div className="relative mx-auto max-w-4xl py-16 text-center sm:py-24">
          <div className="mb-4 flex items-center justify-center gap-3">
            {env.demoMode ? <DemoBadge /> : null}
          </div>
          <h1 className="font-display text-5xl font-bold tracking-tight text-white sm:text-6xl md:text-7xl">
            GemmaQA
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg text-tide-300 sm:text-xl">
            From URL to test coverage, workflows, and evidence-backed bugs.
          </p>
          <p className="mx-auto mt-5 max-w-2xl text-base leading-relaxed text-slate-400">
            GemmaQA is an autonomous exploratory testing agent powered by Gemma. It opens
            authorized web applications, understands their structure, explores workflows,
            executes safe QA checks, captures evidence, and produces synchronized testing
            documentation.
          </p>
          <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
            <Link to="/runs/new" className="btn-primary px-6 py-3 text-base">
              Start a QA run
            </Link>
            <Link to="/history" className="btn-ghost px-6 py-3 text-base">
              View run history
            </Link>
          </div>
          <p className="mx-auto mt-8 max-w-xl text-xs leading-relaxed text-slate-500">
            Only test systems you own or are explicitly authorized to test. GemmaQA is for
            authorized QA use only.
          </p>
        </div>
      </section>

      <section className="grid gap-6 md:grid-cols-3">
        {[
          {
            title: "Explore safely",
            body: "Playwright-driven navigation within an authorized domain. Destructive actions stay blocked.",
          },
          {
            title: "Document continuously",
            body: "Modules, workflows, tests, and bugs stay synchronized with agent memory during the run.",
          },
          {
            title: "Evidence-backed findings",
            body: "Screenshots, console, and network signals attach to confirmed and suspected bugs.",
          },
        ].map((item) => (
          <div key={item.title} className="surface p-6">
            <h2 className="font-display text-xl text-white">{item.title}</h2>
            <p className="mt-3 text-sm leading-relaxed text-slate-400">{item.body}</p>
          </div>
        ))}
      </section>
    </div>
  );
}

import { Link, NavLink } from "react-router-dom";
import type { ReactNode } from "react";
import { env } from "../config/env";
import { DemoBadge } from "./DemoBadge";
import { ProviderSwitcher } from "./ProviderSwitcher";

const nav = [
  { to: "/", label: "Home", end: true },
  { to: "/runs/new", label: "New Run" },
  { to: "/history", label: "History" },
  { to: "/settings", label: "Settings" },
];

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="relative min-h-screen overflow-x-hidden">
      <div className="pointer-events-none absolute inset-0 grid-overlay opacity-70" />
      <header className="relative z-20 border-b border-white/10 bg-ink-950/50 backdrop-blur-xl">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <Link to="/" className="group flex items-center gap-3">
            <span className="font-display text-2xl font-bold tracking-tight text-white transition group-hover:text-tide-400">
              GemmaQA
            </span>
            <span className="hidden text-[11px] uppercase tracking-[0.22em] text-slate-500 md:inline">
              Autonomous QA
            </span>
            {env.demoMode ? <DemoBadge /> : null}
          </Link>
          <nav className="flex flex-wrap items-center gap-1">
            <ProviderSwitcher />
            {nav.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  ["tab", isActive ? "tab-active" : ""].join(" ")
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="relative z-10 mx-auto max-w-7xl px-4 py-6 sm:px-6 sm:py-8">
        {children}
      </main>
    </div>
  );
}

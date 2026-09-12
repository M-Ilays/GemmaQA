import { severityTone } from "../utils/format";

export function SeverityBadge({ severity }: { severity: string }) {
  return (
    <span
      className={`inline-flex rounded-md px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ring-1 ${severityTone(severity)}`}
    >
      {severity || "unknown"}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className="inline-flex rounded-md bg-white/5 px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide text-slate-300 ring-1 ring-white/10">
      {status.replace(/_/g, " ")}
    </span>
  );
}

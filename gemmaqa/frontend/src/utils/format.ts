export function formatStatus(status: string): string {
  return status.replace(/_/g, " ");
}

export function statusColor(status: string): string {
  switch (status) {
    case "completed":
      return "text-tide-400";
    case "failed":
      return "text-rose-400";
    case "cancelled":
    case "stopped":
      return "text-slate-400";
    case "paused":
      return "text-amber-300";
    case "exploring":
    case "testing":
    case "observing":
    case "planning":
    case "executing":
    case "navigating":
    case "opening_browser":
    case "authenticating":
    case "analyzing":
    case "documenting":
    case "initializing":
      return "text-ember-400";
    default:
      return "text-sky-300";
  }
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function formatTime(iso?: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function severityTone(severity: string): string {
  switch (severity.toLowerCase()) {
    case "blocker":
    case "critical":
      return "bg-rose-500/15 text-rose-300 ring-rose-500/30";
    case "high":
    case "major":
      return "bg-amber-500/15 text-amber-200 ring-amber-500/30";
    case "medium":
      return "bg-sky-500/15 text-sky-200 ring-sky-500/30";
    default:
      return "bg-slate-500/15 text-slate-300 ring-slate-500/30";
  }
}

export function connectionLabel(state: string): string {
  switch (state) {
    case "connected":
      return "Live";
    case "connecting":
      return "Connecting";
    case "reconnecting":
      return "Reconnecting";
    case "failed":
      return "WS failed";
    default:
      return "Offline";
  }
}

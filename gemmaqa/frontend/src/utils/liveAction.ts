import type { NormalizedWsEvent } from "../types";

/**
 * Current-action banner for the live run page.
 *
 * The backend already composes `live_status_line` / `live_action_line` on the
 * existing activity events. This module only reads those fields — it does not
 * invent a second event stream.
 */

const ACTION_EVENTS = new Set([
  "action_started",
  "action_executed",
  "action_failed",
  "action_blocked",
]);

export type LiveActionKind = "running" | "blocked" | "failed" | "idle";

export type LiveActionView = {
  banner: string;
  line: string;
  reason: string | null;
  kind: LiveActionKind;
};

export function runBanner(status: string): string {
  const s = (status || "").toLowerCase();
  if (s === "paused") return "PAUSED";
  if (["completed", "failed", "cancelled", "stopped"].includes(s)) {
    return s.toUpperCase();
  }
  if (!s || s === "created") return "READY";
  return "RUNNING";
}

function firstLine(text: string): string {
  return text.split("\n")[0] ?? text;
}

function reasonFrom(payload: Record<string, unknown>, line: string): string | null {
  const fromLine = line.split("\n").find((part) => part.startsWith("REASON → "));
  if (fromLine) return fromLine.slice("REASON → ".length);
  const raw = payload.reason ?? payload.block_reason ?? payload.error;
  return raw ? String(raw) : null;
}

export function deriveLiveAction(
  events: NormalizedWsEvent[],
  runStatus: string,
): LiveActionView {
  const banner = runBanner(runStatus);
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const ev = events[i];
    if (!ACTION_EVENTS.has(ev.type)) continue;
    const payload = ev.payload || {};
    if (ev.type === "action_blocked") {
      const line = String(
        payload.live_action_line || `BLOCKED → ${payload.action_label || payload.action || "action"}`,
      );
      return {
        banner,
        line: firstLine(line),
        reason: reasonFrom(payload, line),
        kind: "blocked",
      };
    }
    if (ev.type === "action_failed" || (ev.type === "action_executed" && payload.success === false)) {
      const line = String(
        payload.live_action_line || `FAILED → ${payload.action_label || payload.action || "action"}`,
      );
      return {
        banner,
        line: firstLine(line),
        reason: reasonFrom(payload, line),
        kind: "failed",
      };
    }
    if (ev.type === "action_started" || ev.type === "action_executed") {
      const line = String(
        payload.live_status_line || payload.live_action_line || "",
      );
      if (!line) continue;
      return { banner, line, reason: null, kind: "running" };
    }
  }
  return { banner, line: "Waiting for the first action…", reason: null, kind: "idle" };
}

export function isActionActivityEvent(event: string): boolean {
  return event.startsWith("action_");
}

export function clockFromIso(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

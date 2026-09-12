import { describe, expect, it } from "vitest";
import type { NormalizedWsEvent, WSEvent } from "../types";
import { deriveLiveAction, runBanner } from "./liveAction";

function ev(
  type: string,
  payload: Record<string, unknown>,
  timestamp = "2026-08-31T08:32:14Z",
): NormalizedWsEvent {
  const raw = { type, run_id: "r1", timestamp, payload } as WSEvent;
  return { type, run_id: "r1", timestamp, payload, raw };
}

describe("runBanner", () => {
  it("shows RUNNING while the agent is working", () => {
    expect(runBanner("exploring")).toBe("RUNNING");
    expect(runBanner("executing")).toBe("RUNNING");
    expect(runBanner("planning")).toBe("RUNNING");
  });

  it("shows a terminal or paused state as-is", () => {
    expect(runBanner("paused")).toBe("PAUSED");
    expect(runBanner("completed")).toBe("COMPLETED");
    expect(runBanner("failed")).toBe("FAILED");
  });
});

describe("deriveLiveAction", () => {
  it("reads CLICKING from an action_started payload", () => {
    const view = deriveLiveAction(
      [ev("action_started", { live_status_line: "CLICKING: Sign up", live_action_line: "CLICK → Sign up" })],
      "exploring",
    );
    expect(view.banner).toBe("RUNNING");
    expect(view.line).toBe("CLICKING: Sign up");
    expect(view.kind).toBe("running");
  });

  it("reads FILLING from a later fill start", () => {
    const view = deriveLiveAction(
      [
        ev("action_started", { live_status_line: "CLICKING: Sign up" }),
        ev("action_started", { live_status_line: "FILLING: Email" }),
      ],
      "exploring",
    );
    expect(view.line).toBe("FILLING: Email");
  });

  it("reads SUBMITTING FORM from a submit start", () => {
    const view = deriveLiveAction(
      [ev("action_started", { live_status_line: "SUBMITTING FORM" })],
      "exploring",
    );
    expect(view.line).toBe("SUBMITTING FORM");
  });

  it("represents a blocked action with its reason", () => {
    const view = deriveLiveAction(
      [
        ev("action_blocked", {
          live_action_line: "BLOCKED → Delete contact\nREASON → Safety policy",
          action_label: "Delete contact",
          reason: "Safety policy",
        }),
      ],
      "exploring",
    );
    expect(view.kind).toBe("blocked");
    expect(view.line).toBe("BLOCKED → Delete contact");
    expect(view.reason).toBe("Safety policy");
  });

  it("represents a failed action with its reason", () => {
    const view = deriveLiveAction(
      [
        ev("action_executed", {
          success: false,
          live_action_line: "FAILED → Submit\nREASON → Validation error",
          error: "Validation error",
        }),
      ],
      "exploring",
    );
    expect(view.kind).toBe("failed");
    expect(view.line).toBe("FAILED → Submit");
    expect(view.reason).toBe("Validation error");
  });
});

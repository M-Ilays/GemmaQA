import { describe, expect, it } from "vitest";
import { formatActivityLogText } from "./ActivityLog";
import type { ActivityRecord } from "../types";

function rec(partial: Partial<ActivityRecord> & Pick<ActivityRecord, "seq" | "summary">): ActivityRecord {
  return {
    run_id: "run-1",
    at: "2026-08-30T10:00:00Z",
    elapsed_ms: 0,
    phase: "lifecycle",
    event: "state_changed",
    detail: {},
    ...partial,
  };
}

describe("formatActivityLogText", () => {
  it("formats the header and one readable line per step", () => {
    const text = formatActivityLogText(
      [
        rec({
          seq: 1,
          elapsed_ms: 4000,
          phase: "planning",
          summary: "Planning next action",
          duration_ms: 61000,
        }),
        rec({
          seq: 2,
          elapsed_ms: 65000,
          phase: "execution",
          summary: "Filled first name",
        }),
      ],
      { runId: "abc-123" },
    );

    expect(text).toContain("GemmaQA activity log  run=abc-123  2 steps");
    expect(text).toContain("00:04  planning        Planning next action  61.0s");
    expect(text).toContain("01:05  execution       Filled first name");
  });

  it("notes an empty log and an active phase filter", () => {
    expect(formatActivityLogText([], { runId: "r1" })).toBe(
      "GemmaQA activity log  run=r1  0 steps\n(empty)",
    );
    expect(formatActivityLogText([rec({ seq: 1, summary: "Paused" })], { runId: "r1", phaseFilter: "lifecycle" })).toContain(
      "filter=lifecycle",
    );
  });
});

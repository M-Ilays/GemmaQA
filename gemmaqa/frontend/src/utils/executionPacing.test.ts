import { describe, expect, it } from "vitest";
import {
  ACTION_PAUSES,
  DEFAULT_ACTION_PAUSE,
  DEFAULT_EXECUTION_SPEED,
  EXECUTION_SPEEDS,
  formatPause,
  formatSpeed,
  isActionPause,
  isExecutionSpeed,
} from "./executionPacing";

describe("execution pacing presets", () => {
  it("defaults to 1x and 0s", () => {
    expect(DEFAULT_EXECUTION_SPEED).toBe(1);
    expect(DEFAULT_ACTION_PAUSE).toBe(0);
  });

  it("accepts every speed chip", () => {
    expect([...EXECUTION_SPEEDS]).toEqual([0.25, 0.5, 1, 1.5, 2, 4]);
    for (const speed of EXECUTION_SPEEDS) {
      expect(isExecutionSpeed(speed)).toBe(true);
      expect(formatSpeed(speed)).toBe(`${speed}x`);
    }
  });

  it("accepts every action-pause chip", () => {
    expect([...ACTION_PAUSES]).toEqual([0, 0.5, 1, 2, 5, 10]);
    for (const pause of ACTION_PAUSES) {
      expect(isActionPause(pause)).toBe(true);
    }
    expect(formatPause(0)).toBe("0s");
    expect(formatPause(2)).toBe("2s");
  });
});

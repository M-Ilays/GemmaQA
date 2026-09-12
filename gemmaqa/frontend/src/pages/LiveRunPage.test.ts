import { describe, expect, it } from "vitest";
import { sameData } from "./LiveRunPage";

/**
 * The live run page polls four endpoints and, before this guard, replaced state
 * with the response every time. Identical rows arriving as brand-new arrays still
 * re-render every list, re-mount every <img>, and redraw the Mermaid diagram.
 *
 * Combined with the other half of the fix — `events.length` had been in the
 * refresh effect's dependency array, so each websocket event tore down the
 * interval and refreshed immediately — run 5b4268e0 produced 1366 events in 521s:
 * ~1400 refreshes and 2.6 whole-page re-renders per second for the length of the
 * run.
 *
 * Measured after the fix, on live run 7a4e97b8: 1 DOM mutation in 12 seconds.
 */
describe("sameData", () => {
  it("recognises the same rows arriving as a new array", () => {
    const previous = [{ id: "a", n: 1 }, { id: "b", n: 2 }];
    const fromApi = [{ id: "a", n: 1 }, { id: "b", n: 2 }];

    expect(previous).not.toBe(fromApi);
    expect(sameData(previous, fromApi)).toBe(true);
  });

  it("spots an appended row", () => {
    expect(sameData([{ id: "a" }], [{ id: "a" }, { id: "b" }])).toBe(false);
  });

  it("spots a changed field", () => {
    expect(sameData({ status: "running" }, { status: "completed" })).toBe(false);
  });

  it("spots a reordering, because the view renders in order", () => {
    expect(sameData([{ id: "a" }, { id: "b" }], [{ id: "b" }, { id: "a" }])).toBe(false);
  });

  it("treats identical references as equal without serialising", () => {
    const rows = [{ id: "a" }];
    expect(sameData(rows, rows)).toBe(true);
  });

  it("does not claim two empty states are different", () => {
    expect(sameData([], [])).toBe(true);
  });

  it("never reports null and a value as the same", () => {
    // Absence is absence: the first load must always render.
    expect(sameData(null, { status: "running" })).toBe(false);
    expect(sameData({ status: "running" }, null)).toBe(false);
  });

  it("reports a difference rather than throwing on a circular payload", () => {
    // A refresh must never crash the page. Reporting "changed" is the safe answer
    // because it only costs a re-render.
    const circular: Record<string, unknown> = { id: "a" };
    circular.self = circular;

    expect(sameData(circular, { id: "a" })).toBe(false);
  });

  it("distinguishes numeric progress that only moves slightly", () => {
    expect(sameData({ progress_pct: 2.5 }, { progress_pct: 5 })).toBe(false);
    expect(sameData({ progress_pct: 2.5 }, { progress_pct: 2.5 })).toBe(true);
  });
});

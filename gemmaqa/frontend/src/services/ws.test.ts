import { describe, expect, it } from "vitest";
import { resolveApiUrl } from "../config/env";
import { normalizeWsEvent } from "./ws";
import { severityTone, shortId } from "../utils/format";

describe("normalizeWsEvent", () => {
  it("reads structured controller envelope inside data", () => {
    const ev = normalizeWsEvent({
      event: "page_observed",
      run_id: "r1",
      timestamp: "2026-01-01T00:00:00Z",
      data: {
        type: "page_observed",
        run_id: "r1",
        timestamp: "2026-01-01T00:00:00Z",
        payload: { url: "https://example.com", page_type: "login" },
      },
    });
    expect(ev.type).toBe("page_observed");
    expect(ev.payload.url).toBe("https://example.com");
    expect(ev.payload.page_type).toBe("login");
  });

  it("supports flat payload", () => {
    const ev = normalizeWsEvent({
      type: "bug_confirmed",
      run_id: "r1",
      timestamp: "t",
      payload: { title: "x" },
    });
    expect(ev.type).toBe("bug_confirmed");
    expect(ev.payload.title).toBe("x");
  });
});

describe("format helpers", () => {
  it("shortens ids", () => {
    expect(shortId("abcdefghijklmnop")).toBe("abcdefgh");
  });

  it("maps severity tones without throwing", () => {
    expect(severityTone("critical")).toContain("rose");
    expect(severityTone("medium")).toContain("sky");
  });
});

describe("resolveApiUrl", () => {
  it("returns absolute paths when base is empty", () => {
    expect(resolveApiUrl("/api/runs")).toBe("/api/runs");
  });
});

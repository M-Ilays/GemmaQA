import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { emptyRunForm } from "../utils/runDraft";

const here = dirname(fileURLToPath(import.meta.url));

describe("New Run has no action/page budgets", () => {
  it("does not expose Maximum Actions or Maximum Pages", () => {
    const src = readFileSync(join(here, "NewRunPage.tsx"), "utf8");
    expect(src).not.toMatch(/Maximum [Aa]ctions/);
    expect(src).not.toMatch(/Maximum [Pp]ages/);
    expect(src).not.toMatch(/Maximum [Rr]untime/);
    expect(src).not.toContain("Unlimited");
  });

  it("does not put budget fields on the default form", () => {
    const form = emptyRunForm();
    expect(form.configuration.max_actions).toBeUndefined();
    expect(form.configuration.max_pages).toBeUndefined();
    expect(form.configuration.max_runtime_seconds).toBeUndefined();
  });
});

describe("Live run keeps counters and operator controls", () => {
  it("still shows action/page counters, speed/pause, and Pause/Continue/End run", () => {
    const src = readFileSync(join(here, "LiveRunPage.tsx"), "utf8");
    expect(src).toContain('label="Actions"');
    expect(src).toContain('label="Pages"');
    expect(src).toContain("ExecutionPacingControls");
    expect(src).toContain("Pause");
    expect(src).toContain("Continue");
    expect(src).toContain("End run");
    expect(src).not.toMatch(/Maximum [Aa]ctions/);
    expect(src).not.toMatch(/Maximum [Pp]ages/);
    expect(src).not.toContain("15-minute");
  });
});

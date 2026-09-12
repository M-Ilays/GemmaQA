import { describe, expect, it } from "vitest";
import { demoApi, DEMO_RUN_ID, demoReport } from "../demo/fixtures";

describe("demo fixtures", () => {
  it("exposes sample run and report without inventing extra pages", async () => {
    const run = await demoApi.getRun(DEMO_RUN_ID);
    expect(run.run_id).toBe(DEMO_RUN_ID);
    expect(demoReport.page_inventory).toHaveLength(3);
    const report = await demoApi.getReport(DEMO_RUN_ID);
    expect(report.confirmed_bugs[0]?.bug_id).toBe("BUG-001");
    expect(report.coverage?.disclaimer.toLowerCase()).toContain("complete");
  });

  it("lists implemented providers without allowing a switch", async () => {
    const catalog = await demoApi.listProviders();
    expect(catalog.active).toBe("mock");
    expect(catalog.providers.map((item) => item.id)).toEqual([
      "mock",
      "openai_compatible",
      "gemini",
      "bedrock",
      "transformers",
    ]);
    await expect(demoApi.setProvider("gemini")).rejects.toThrow(/demo mode/i);
  });

  it("createRun returns demo fixture id", async () => {
    const res = await demoApi.createRun({
      url: "https://example.com",
      authorization_ack: true,
      configuration: {
        safe_mode: true,
        allow_controlled_writes: false,
        headless: true,
        allow_cross_domain: false,
      },
    });
    expect(res.run_id).toBe(DEMO_RUN_ID);
  });
});

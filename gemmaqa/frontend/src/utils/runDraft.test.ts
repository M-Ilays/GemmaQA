import { describe, expect, it, beforeEach } from "vitest";
import {
  _resetMemoryCreds,
  emptyRunForm,
  formFromPreviousRun,
  passwordIfSameTarget,
  rememberSubmittedRun,
} from "./runDraft";
import type { RunStatus } from "../types";

const run: RunStatus = {
  run_id: "r1",
  status: "completed",
  url: "https://thinking-tester-contact-list.herokuapp.com/",
  pages_visited: 2,
  actions_taken: 10,
  bugs_found: 0,
  progress_pct: 40,
  username: "pat@example.com",
  notes: "Exercise the contact CRUD features",
  configuration: {
    safe_mode: true,
    allow_controlled_writes: true,
    allow_safe_test_data_creation: true,
    allow_destructive_actions: false,
    headless: true,
    allow_cross_domain: false,
    testing_objective: "Exercise the contact CRUD features",
  },
};

describe("formFromPreviousRun", () => {
  beforeEach(() => _resetMemoryCreds());

  it("copies url, username, notes, and write choices from the previous run", () => {
    const form = formFromPreviousRun(run);
    expect(form.url).toBe(run.url);
    expect(form.username).toBe("pat@example.com");
    expect(form.notes).toBe("Exercise the contact CRUD features");
    expect(form.configuration.allow_controlled_writes).toBe(true);
    expect(form.configuration.allow_safe_test_data_creation).toBe(true);
    expect(form.configuration.max_actions).toBeUndefined();
    expect(form.configuration.max_pages).toBeUndefined();
    expect(form.password).toBe("");
    expect(form.authorization_ack).toBe(false);
  });

  it("drops leftover max_actions / max_pages / max_runtime_seconds from an older stored run", () => {
    const form = formFromPreviousRun({
      ...run,
      configuration: {
        ...run.configuration,
        max_actions: 40,
        max_pages: 10,
        max_runtime_seconds: 900,
      },
    });
    expect(form.configuration.max_actions).toBeUndefined();
    expect(form.configuration.max_pages).toBeUndefined();
    expect(form.configuration.max_runtime_seconds).toBeUndefined();
    expect(form.password).toBe("");
    expect(form.authorization_ack).toBe(false);
  });

  it("fills the password only when this tab just submitted the same target", () => {
    rememberSubmittedRun({
      ...emptyRunForm(),
      url: run.url,
      username: "pat@example.com",
      password: "secret-once",
    });
    expect(passwordIfSameTarget(run.url, "pat@example.com")).toBe("secret-once");
    expect(formFromPreviousRun(run).password).toBe("secret-once");
    expect(formFromPreviousRun({ ...run, username: "other@example.com" }).password).toBe("");
  });
});

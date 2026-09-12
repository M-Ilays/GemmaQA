import type { CreateRunRequest, RunConfiguration, RunStatus } from "../types";

/**
 * Last-used New Run values so "Test again" can reopen the same choices.
 *
 * Password is kept only in this tab's memory. It is never written to
 * localStorage or sessionStorage — same rule as NewRunPage.
 */

export const emptyRunForm = (): CreateRunRequest => ({
  url: "https://thinking-tester-contact-list.herokuapp.com/",
  username: "",
  password: "",
  notes: "",
  authorization_ack: false,
  auto_start: true,
  configuration: {
    safe_mode: true,
    allow_controlled_writes: false,
    allow_safe_test_data_creation: false,
    allow_destructive_actions: false,
    testing_objective: "",
    headless: true,
    allow_cross_domain: false,
    allow_subdomains: false,
    username_selector: "",
    password_selector: "",
    submit_selector: "",
    login_url: "",
  },
});

type MemoryCreds = { url: string; username: string; password: string };

let memoryCreds: MemoryCreds | null = null;

export function rememberSubmittedRun(req: CreateRunRequest): void {
  memoryCreds = req.password
    ? { url: req.url, username: req.username || "", password: req.password }
    : null;
}

export function passwordIfSameTarget(url: string, username: string): string {
  if (!memoryCreds) return "";
  if (memoryCreds.url !== url) return "";
  if (memoryCreds.username !== (username || "")) return "";
  return memoryCreds.password;
}

export function formFromPreviousRun(run: RunStatus): CreateRunRequest {
  const base = emptyRunForm();
  const username = run.username || "";
  return {
    ...base,
    url: run.url || base.url,
    username,
    notes: run.notes || "",
    password: passwordIfSameTarget(run.url, username),
    authorization_ack: false,
    configuration: mergeConfiguration(base.configuration, run.configuration),
  };
}

export function mergeConfiguration(
  base: RunConfiguration,
  incoming?: RunConfiguration | null,
): RunConfiguration {
  if (!incoming) return { ...base };
  const merged: RunConfiguration = {
    ...base,
    ...incoming,
    testing_objective: incoming.testing_objective ?? base.testing_objective ?? "",
    username_selector: incoming.username_selector ?? "",
    password_selector: incoming.password_selector ?? "",
    submit_selector: incoming.submit_selector ?? "",
    login_url: incoming.login_url ?? "",
  };
  // Action/page/runtime budgets are gone. Drop leftover fields from older stored runs.
  delete merged.max_actions;
  delete merged.max_pages;
  delete merged.max_runtime_seconds;
  return merged;
}

/** Test helper — do not call from UI. */
export function _resetMemoryCreds(): void {
  memoryCreds = null;
}

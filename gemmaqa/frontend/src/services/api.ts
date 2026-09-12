import { env, resolveApiUrl, resolveWsUrl } from "../config/env";
import type {
  ActionItem,
  ActivityLogPage,
  BugItem,
  CreateRunRequest,
  CreateRunResponse,
  EvidenceFile,
  FinalReport,
  PageItem,
  ProviderCatalog,
  RunStatus,
  StrandsStatus,
} from "../types";
import { demoApi } from "../demo/fixtures";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(resolveApiUrl(path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json() as Promise<T>;
  }
  return (await res.text()) as T;
}

/** FastAPI `{detail}` bodies, otherwise the raw error text. */
export function parseApiError(err: unknown): string {
  if (!(err instanceof Error)) return "Request failed";
  try {
    const parsed = JSON.parse(err.message) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
    if (Array.isArray(parsed.detail)) {
      return parsed.detail
        .map((item) =>
          typeof item === "object" && item && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join("; ");
    }
  } catch {
    return err.message;
  }
  return err.message;
}

export type ExportKind = "json" | "md" | "html" | "bugs.csv" | "tests.csv" | "navigation.mmd";

const liveApi = {
  health: () => request<{ status: string; app: string; version: string }>("/health"),
  strandsStatus: async () => {
    const body = await request<{ strands?: StrandsStatus }>("/api/health");
    return body.strands ?? null;
  },
  listProviders: () => request<ProviderCatalog>("/api/config/providers"),
  setProvider: (provider: string) =>
    request<ProviderCatalog>("/api/config/provider", {
      method: "POST",
      body: JSON.stringify({ provider }),
    }),
  listRuns: async () => {
    const data = await request<{ items: RunStatus[] } | RunStatus[]>("/api/runs");
    return Array.isArray(data) ? data : data.items;
  },
  getRun: (runId: string) => request<RunStatus>(`/api/runs/${runId}`),
  createRun: (body: CreateRunRequest, opts?: { strands?: boolean }) =>
    request<CreateRunResponse>(opts?.strands ? "/api/runs/strands" : "/api/runs", {
      method: "POST",
      body: JSON.stringify({ ...body, auto_start: true }),
    }),
  startRun: (runId: string) =>
    request<RunStatus>(`/api/runs/${runId}/start`, { method: "POST" }),
  cancelRun: (runId: string) =>
    request<RunStatus>(`/api/runs/${runId}/end`, { method: "POST" }),
  pauseRun: (runId: string) =>
    request<RunStatus>(`/api/runs/${runId}/pause`, { method: "POST" }),
  resumeRun: (runId: string) =>
    request<RunStatus>(`/api/runs/${runId}/resume`, { method: "POST" }),
  setRunPacing: (
    runId: string,
    body: { execution_speed?: number; action_pause?: number },
  ) =>
    request<RunStatus>(`/api/runs/${runId}/pacing`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getActions: (runId: string) => request<ActionItem[]>(`/api/runs/${runId}/actions`),
  // The durable activity log. `sinceSeq` fetches only what the caller has not
  // seen, so the live view can hydrate its full history on mount and then keep
  // up without refetching everything.
  getActivity: (runId: string, sinceSeq = 0, limit = 500) =>
    request<ActivityLogPage>(
      `/api/runs/${runId}/activity?since_seq=${sinceSeq}&limit=${limit}`,
    ),
  getBugs: (runId: string) => request<BugItem[]>(`/api/runs/${runId}/bugs`),
  getPages: (runId: string) => request<PageItem[]>(`/api/runs/${runId}/pages`),
  getReport: (runId: string) => request<FinalReport>(`/api/runs/${runId}/report`),
  getReportMarkdown: (runId: string) => request<string>(`/api/runs/${runId}/report.md`),
  getEvidence: (runId: string) => request<EvidenceFile[]>(`/api/runs/${runId}/evidence`),
  getNavigationMermaid: (runId: string) =>
    request<string>(`/api/runs/${runId}/navigation.mmd`),
  evidenceFileUrl: (runId: string, relativePath: string) =>
    resolveApiUrl(
      `/api/runs/${runId}/evidence/file/${relativePath.replace(/^\/+/, "")}`
    ),
  exportUrl: (runId: string, kind: ExportKind) => {
    const paths: Record<ExportKind, string> = {
      json: `/api/runs/${runId}/report.json`,
      md: `/api/runs/${runId}/report.md`,
      html: `/api/runs/${runId}/report.html`,
      "bugs.csv": `/api/runs/${runId}/bugs.csv`,
      "tests.csv": `/api/runs/${runId}/tests.csv`,
      "navigation.mmd": `/api/runs/${runId}/navigation.mmd`,
    };
    return resolveApiUrl(paths[kind]);
  },
};

export const api = env.demoMode ? demoApi : liveApi;
export { resolveWsUrl as wsUrl, env };

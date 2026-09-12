/** Centralized env access — never hardcode backend URLs in components. */

function stripTrailingSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

export const env = {
  apiBaseUrl: stripTrailingSlash(
    import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_BASE || ""
  ),
  wsBaseUrl: stripTrailingSlash(
    import.meta.env.VITE_WS_BASE_URL || ""
  ),
  /** Legacy host-only override */
  wsHost: import.meta.env.VITE_WS_HOST || "",
  demoMode: String(import.meta.env.VITE_DEMO_MODE || "").toLowerCase() === "true",
};

export function resolveApiUrl(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  const base = env.apiBaseUrl;
  if (!base) return path.startsWith("/") ? path : `/${path}`;
  return `${base}${path.startsWith("/") ? path : `/${path}`}`;
}

export function resolveWsUrl(runId: string): string {
  if (env.wsBaseUrl) {
    const base = env.wsBaseUrl.replace(/\/$/, "");
    return `${base}/ws/runs/${runId}`;
  }
  if (env.wsHost) {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${env.wsHost}/ws/runs/${runId}`;
  }
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/runs/${runId}`;
}

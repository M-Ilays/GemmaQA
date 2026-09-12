import type { NormalizedWsEvent, WSEvent, WsEventType } from "../types";

/** Normalize backend WS envelopes (legacy + structured). */
export function normalizeWsEvent(raw: WSEvent): NormalizedWsEvent {
  const nested =
    raw.data && typeof raw.data === "object"
      ? (raw.data as Record<string, unknown>)
      : undefined;

  const type = String(
    raw.event ||
      raw.type ||
      nested?.type ||
      nested?.event ||
      "unknown"
  ) as WsEventType;

  let payload: Record<string, unknown> = {};
  if (nested?.payload && typeof nested.payload === "object") {
    payload = nested.payload as Record<string, unknown>;
  } else if (raw.payload && typeof raw.payload === "object") {
    payload = raw.payload;
  } else if (nested) {
    // Strip envelope keys if present
    const { type: _t, event: _e, run_id: _r, timestamp: _ts, payload: _p, ...rest } =
      nested;
    payload = rest;
  }

  return {
    type,
    run_id: raw.run_id || String(nested?.run_id || ""),
    timestamp: raw.timestamp || String(nested?.timestamp || new Date().toISOString()),
    payload,
    raw,
  };
}

export type ConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected" | "failed";

const MAX_RETRIES = 5;
const BASE_DELAY_MS = 800;

export interface RunSocketOptions {
  runId: string;
  onEvent: (event: NormalizedWsEvent) => void;
  onState?: (state: ConnectionState) => void;
  maxRetries?: number;
}

/**
 * Native WebSocket client with limited reconnect retries.
 * Does not log sensitive payloads.
 */
export class RunSocket {
  private ws: WebSocket | null = null;
  private retries = 0;
  private closedByUser = false;
  private pingTimer: number | null = null;
  private reconnectTimer: number | null = null;
  private readonly maxRetries: number;

  constructor(private readonly options: RunSocketOptions) {
    this.maxRetries = options.maxRetries ?? MAX_RETRIES;
  }

  connect(wsUrl: string) {
    this.closedByUser = false;
    this.options.onState?.("connecting");
    const ws = new WebSocket(wsUrl);
    this.ws = ws;

    ws.onopen = () => {
      this.retries = 0;
      this.options.onState?.("connected");
      this.pingTimer = window.setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send("ping");
      }, 25000);
    };

    ws.onmessage = (msg) => {
      try {
        const parsed = JSON.parse(String(msg.data)) as WSEvent;
        this.options.onEvent(normalizeWsEvent(parsed));
      } catch {
        // ignore malformed frames
      }
    };

    ws.onerror = () => {
      // onclose handles reconnect
    };

    ws.onclose = () => {
      this.clearPing();
      if (this.closedByUser) {
        this.options.onState?.("disconnected");
        return;
      }
      if (this.retries >= this.maxRetries) {
        this.options.onState?.("failed");
        return;
      }
      this.retries += 1;
      this.options.onState?.("reconnecting");
      const delay = BASE_DELAY_MS * Math.min(8, this.retries);
      this.reconnectTimer = window.setTimeout(() => this.connect(wsUrl), delay);
    };
  }

  close() {
    this.closedByUser = true;
    this.clearPing();
    if (this.reconnectTimer) window.clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
    this.options.onState?.("disconnected");
  }

  private clearPing() {
    if (this.pingTimer) {
      window.clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }
}

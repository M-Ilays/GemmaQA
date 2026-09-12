import { useEffect, useRef, useState } from "react";
import { env } from "../config/env";
import { demoTimeline } from "../demo/fixtures";
import { wsUrl } from "../services/api";
import { RunSocket, normalizeWsEvent, type ConnectionState } from "../services/ws";
import type { NormalizedWsEvent } from "../types";

export function useRunSocket(runId: string | undefined) {
  const [events, setEvents] = useState<NormalizedWsEvent[]>([]);
  const [connectionState, setConnectionState] = useState<ConnectionState>("disconnected");
  const socketRef = useRef<RunSocket | null>(null);

  useEffect(() => {
    if (!runId) return;

    if (env.demoMode) {
      setConnectionState("connected");
      setEvents(demoTimeline.map((e) => normalizeWsEvent(e)));
      return;
    }

    const socket = new RunSocket({
      runId,
      onState: setConnectionState,
      onEvent: (event) => {
        setEvents((prev) => [...prev.slice(-249), event]);
      },
      maxRetries: 5,
    });
    socketRef.current = socket;
    socket.connect(wsUrl(runId));

    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [runId]);

  return {
    events,
    connectionState,
    connected: connectionState === "connected",
    reconnecting: connectionState === "reconnecting",
  };
}

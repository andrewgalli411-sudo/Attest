"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "./api";

export type RunEvent = { seq: number; type: string; payload: Record<string, unknown> };
export type StreamState = "connecting" | "live" | "done" | "error";

const TYPES = [
  "stage.started", "stage.finished", "finding.caught", "finding.triaged", "finding.fix_authored",
  "finding.needs_human", "finding.proved", "finding.regressed", "finding.pr_opened", "score.updated",
  "run.summary", "log", "error",
];

/** Live run events over SSE. The browser resends Last-Event-ID on reconnect, so nothing is missed. */
export function useRunEvents(runId: string, onEvent?: (e: RunEvent) => void) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  const cb = useRef(onEvent);
  useEffect(() => { cb.current = onEvent; });

  useEffect(() => {
    const es = new EventSource(api.eventsUrl(runId));
    const handle = (type: string) => (m: MessageEvent) => {
      const ev = { seq: Number(m.lastEventId), type, payload: JSON.parse(m.data) } as RunEvent;
      setEvents((prev) => (prev.some((p) => p.seq === ev.seq) ? prev : [...prev, ev]));
      cb.current?.(ev);
    };
    for (const t of TYPES) es.addEventListener(t, handle(t) as EventListener);
    es.onopen = () => setState("live");
    es.addEventListener("end", () => { setState("done"); es.close(); });
    es.onerror = () => setState((s) => (s === "done" ? s : "error"));
    return () => es.close();
  }, [runId]);

  return { events, state };
}

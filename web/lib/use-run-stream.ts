"use client";

import { useEffect, useRef, useState } from "react";
import type { Api, JournalEvent } from "@/lib/api";

export type StreamStatus = "live" | "ended" | "lost";

/** Seconds before a lost stream is opened again, from the last event it delivered. */
const RECONNECT_MS = 10_000;

/** The run's journaled events: read from the start, then streamed until the run ends. A
 * lost stream is opened again from the last event seen, so nothing is missed or repeated. */
export function useRunStream(
  api: Api,
  runId: string,
  onEvent?: (event: JournalEvent) => void,
): { events: JournalEvent[]; status: StreamStatus } {
  const [events, setEvents] = useState<JournalEvent[]>([]);
  const [status, setStatus] = useState<StreamStatus>("live");
  const [attempt, setAttempt] = useState(0);
  const last = useRef(0);
  const notify = useRef(onEvent);
  notify.current = onEvent;

  useEffect(() => {
    last.current = 0;
    setEvents([]);
    setAttempt(0);
  }, [api, runId]);

  useEffect(() => {
    const abort = new AbortController();
    let retry: number | undefined;
    setStatus("live");
    api
      .stream(
        runId,
        last.current,
        (event) => {
          if (event.seq <= last.current) return;
          last.current = event.seq;
          setEvents((seen) => [...seen, event]);
          notify.current?.(event);
        },
        abort.signal,
      )
      .then(() => setStatus("ended"))
      .catch(() => {
        if (abort.signal.aborted) return;
        setStatus("lost");
        retry = window.setTimeout(() => setAttempt((n) => n + 1), RECONNECT_MS);
      });
    return () => {
      abort.abort();
      window.clearTimeout(retry);
    };
  }, [api, runId, attempt]);

  return { events, status };
}

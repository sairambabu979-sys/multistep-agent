"use client";

import { useEffect, useRef, useState } from "react";
import { useSession } from "@/components/auth";
import { when } from "@/components/run-parts";
import type { JournalEvent } from "@/lib/api";

const SHOWN = ["by", "reason", "feedback", "note", "tree", "head_tree", "reference", "error"];

function details(event: JournalEvent["event"]): string {
  const parts: string[] = [];
  for (const key of SHOWN) {
    const value = event[key];
    if (typeof value === "string" && value) {
      parts.push(`${key}: ${key.endsWith("tree") ? value.slice(0, 12) : value}`);
    }
  }
  const triage = event.triage as { kind?: string } | undefined;
  if (triage?.kind) parts.push(`triage: ${triage.kind}`);
  return parts.join(" · ");
}

/** The run's journaled events, live: read once, then streamed until the run ends. */
export function Timeline({ runId, onEvent }: { runId: string; onEvent?: () => void }) {
  const { api } = useSession();
  const [events, setEvents] = useState<JournalEvent[]>([]);
  const [status, setStatus] = useState<"live" | "ended" | "lost">("live");
  const notify = useRef(onEvent);
  notify.current = onEvent;

  useEffect(() => {
    const abort = new AbortController();
    let last = 0;
    setEvents([]);
    setStatus("live");
    api
      .stream(
        runId,
        0,
        (event) => {
          if (event.seq <= last) return;
          last = event.seq;
          setEvents((seen) => [...seen, event]);
          notify.current?.();
        },
        abort.signal,
      )
      .then(() => setStatus("ended"))
      .catch(() => {
        if (!abort.signal.aborted) setStatus("lost");
      });
    return () => abort.abort();
  }, [api, runId]);

  const latest = events.at(-1);
  return (
    <div>
      <p className="mb-2 text-sm text-muted">
        {status === "live" ? "Live: new events appear as they are journaled." : null}
        {status === "ended" ? "The run is over: this is its whole journal." : null}
        {status === "lost" ? "The stream was lost; reload to reconnect." : null}
      </p>
      <p aria-live="polite" className="sr-only">
        {latest ? `Event ${latest.seq}: ${latest.kind}` : ""}
      </p>
      <ol className="space-y-1">
        {events.map((event) => (
          <li key={event.seq} className="grid grid-cols-[3rem_12rem_1fr] gap-2 rounded-md px-2 py-1 text-sm odd:bg-surface">
            <span className="font-mono text-muted">{event.seq}</span>
            <span className="font-mono">{event.kind}</span>
            <span className="text-muted">
              {when(event.event.at)}
              {details(event.event) ? ` · ${details(event.event)}` : ""}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

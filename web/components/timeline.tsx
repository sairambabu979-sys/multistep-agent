"use client";

import { when } from "@/components/run-parts";
import type { JournalEvent } from "@/lib/api";
import type { StreamStatus } from "@/lib/use-run-stream";

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

/** The run's journaled events, as the page's stream delivers them (``useRunStream``). */
export function Timeline({ events, status }: { events: JournalEvent[]; status: StreamStatus }) {
  const latest = events.at(-1);
  return (
    <div>
      <p className="mb-2 text-sm text-muted">
        {status === "live" ? "Live: new events appear as they are journaled." : null}
        {status === "ended" ? "The run is over: this is its whole journal." : null}
        {status === "lost" ? "The stream was lost: reconnecting, and checking every 15 seconds meanwhile." : null}
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

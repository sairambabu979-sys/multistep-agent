"use client";

import { useSession } from "@/components/auth";
import { Problem, usd } from "@/components/run-parts";
import { Card } from "@/components/ui/card";
import { Cell, Row, Table } from "@/components/ui/table";
import { label } from "@/lib/phases";
import { useResource } from "@/lib/use-resource";

function minutes(seconds: number | null): string {
  if (seconds === null) return "-";
  return seconds < 120 ? `${seconds.toFixed(0)} s` : `${(seconds / 60).toFixed(1)} min`;
}

function Bars({ title, counts }: { title: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts);
  const most = Math.max(1, ...entries.map(([, n]) => n));
  return (
    <Card title={title}>
      {entries.length === 0 ? (
        <p className="text-sm text-muted">No runs yet.</p>
      ) : (
        <Table caption={title} head={["", "Runs", ""]}>
          {entries.map(([key, n]) => (
            <Row key={key}>
              <Cell>{label(key)}</Cell>
              <Cell>{n}</Cell>
              <Cell>
                <div className="h-2 w-40 rounded-full bg-surface" aria-hidden="true">
                  <div className="h-full rounded-full bg-accent" style={{ width: `${(n / most) * 100}%` }} />
                </div>
              </Cell>
            </Row>
          ))}
        </Table>
      )}
    </Card>
  );
}

/** Figures computed by the API from every run's journaled state. */
export default function Metrics() {
  const { api } = useSession();
  const metrics = useResource(() => api.metrics(), "metrics", 5000);
  const m = metrics.data;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Metrics</h1>
      <Problem message={metrics.error} />
      {!m ? (
        <p role="status">Loading…</p>
      ) : (
        <>
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
            {[
              ["Runs", String(m.runs)],
              ["Live", String(m.live)],
              ["Waiting on a person", String(m.waiting_on_people)],
              ["Completed", String(m.completed)],
              ["Spent, all runs", usd(m.spent_usd_micros)],
              ["Time waiting on people", minutes(m.paused_seconds)],
              ["Revisions per completed run", m.mean_revisions_completed?.toFixed(2) ?? "-"],
              ["Working time per completed run", minutes(m.mean_working_seconds_completed)],
            ].map(([term, value]) => (
              <div key={term} className="rounded-lg border border-border bg-raised p-4">
                <dt className="text-sm text-muted">{term}</dt>
                <dd className="text-2xl font-semibold">{value}</dd>
              </div>
            ))}
          </dl>
          <div className="grid gap-4 md:grid-cols-2">
            <Bars title="Runs by phase" counts={m.by_phase} />
            <Bars title="Finished runs by outcome" counts={m.by_outcome} />
          </div>
        </>
      )}
    </div>
  );
}

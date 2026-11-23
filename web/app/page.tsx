"use client";

import Link from "next/link";
import { useState } from "react";
import { useSession } from "@/components/auth";
import { PhaseBadge, Problem, when } from "@/components/run-parts";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Cell, Row, Table } from "@/components/ui/table";
import { can, type RunRow } from "@/lib/api";
import { ASKING, TERMINAL } from "@/lib/phases";
import { useResource } from "@/lib/use-resource";

function Counter({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg border border-border bg-raised p-4">
      <p className="text-sm text-muted">{label}</p>
      <p className={`text-3xl font-semibold ${tone ?? ""}`}>{value}</p>
    </div>
  );
}

function StartRun({ onStarted }: { onStarted: () => void }) {
  const { api } = useSession();
  const [task, setTask] = useState("");
  const [seed, setSeed] = useState("0");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Card title="Start a simulated run">
      <p className="mb-3 text-sm text-muted">
        A simulated run goes through every stage with stub answers and changes nothing. Live runs
        start with <code className="font-mono">msa run</code>, which needs an API key.
      </p>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          api
            .start({ task, simulate: { seed: Number(seed) || 0 } })
            .then(() => {
              setTask("");
              onStarted();
            })
            .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : String(caught)))
            .finally(() => setBusy(false));
        }}
      >
        <div className="min-w-64 flex-1">
          <label htmlFor="task" className="mb-1 block text-sm font-medium">
            Task
          </label>
          <input
            id="task"
            required
            className="w-full rounded-md border border-border bg-bg px-3 py-2"
            value={task}
            onChange={(e) => setTask(e.target.value)}
          />
        </div>
        <div className="w-24">
          <label htmlFor="seed" className="mb-1 block text-sm font-medium">
            Seed
          </label>
          <input
            id="seed"
            inputMode="numeric"
            className="w-full rounded-md border border-border bg-bg px-3 py-2"
            value={seed}
            onChange={(e) => setSeed(e.target.value)}
          />
        </div>
        <Button type="submit" variant="primary" disabled={busy}>
          Start
        </Button>
      </form>
      <div className="mt-3">
        <Problem message={error} />
      </div>
    </Card>
  );
}

function RunsTable({ runs, caption }: { runs: RunRow[]; caption: string }) {
  if (runs.length === 0) return <p className="text-sm text-muted">None.</p>;
  return (
    <Table caption={caption} head={["Run", "Phase", "Outcome", "Events", "Started", "Kind"]}>
      {runs.map((run) => (
        <Row key={run.run_id}>
          <Cell mono>
            <Link className="text-accent underline-offset-2 hover:underline" href={`/runs/${run.run_id}`}>
              {run.run_id}
            </Link>
          </Cell>
          <Cell>
            <PhaseBadge phase={run.phase} />
            {run.driving && <span className="ml-2 text-xs text-muted">running</span>}
          </Cell>
          <Cell>{run.outcome ? `${run.outcome.reason}${run.outcome.detail ? `: ${run.outcome.detail}` : ""}` : "-"}</Cell>
          <Cell>{run.seq}</Cell>
          <Cell>{when(run.created_at)}</Cell>
          <Cell>{run.kind ?? "-"}</Cell>
        </Row>
      ))}
    </Table>
  );
}

export default function MissionControl() {
  const { api, me } = useSession();
  const runs = useResource(() => api.runs(), "runs", 3000);
  const all = runs.data ?? [];
  const live = all.filter((r) => !TERMINAL.has(r.phase));
  const waiting = live.filter((r) => ASKING.has(r.phase));
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Mission Control</h1>
      <Problem message={runs.error} />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Counter label="Live runs" value={live.length} />
        <Counter label="Waiting on a person" value={waiting.length} tone="text-wait" />
        <Counter label="Completed" value={all.filter((r) => r.phase === "completed").length} tone="text-good" />
        <Counter
          label="Failed or cancelled"
          value={all.filter((r) => r.phase === "failed" || r.phase === "cancelled").length}
          tone="text-bad"
        />
      </div>
      {can(me.role, "operator") && <StartRun onStarted={runs.reload} />}
      <Card title="Waiting on a person" actions={<Link href="/approvals" className="text-sm text-accent">Approval Center</Link>}>
        <RunsTable runs={waiting} caption="Runs waiting on a person" />
      </Card>
      <Card title="Run history">
        {runs.loading && !runs.data ? <p role="status">Loading runs…</p> : <RunsTable runs={all} caption="All runs, newest first" />}
      </Card>
    </div>
  );
}

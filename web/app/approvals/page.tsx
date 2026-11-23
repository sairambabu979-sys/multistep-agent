"use client";

import Link from "next/link";
import { useCallback } from "react";
import { ApprovalPanel } from "@/components/approval-panel";
import { useSession } from "@/components/auth";
import { PhaseBadge, Problem } from "@/components/run-parts";
import { Card } from "@/components/ui/card";
import { ASKING, TERMINAL, label } from "@/lib/phases";
import { useResource } from "@/lib/use-resource";

/** Every run that is waiting on a person, with its question and the answers you may give. */
export default function ApprovalCenter() {
  const { api } = useSession();
  const load = useCallback(async () => {
    const runs = await api.runs();
    const asking = runs.filter((r) => !TERMINAL.has(r.phase) && ASKING.has(r.phase));
    return Promise.all(asking.map((r) => api.run(r.run_id)));
  }, [api]);
  const waiting = useResource(load, "approvals", 3000);
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Approval Center</h1>
      <p className="text-sm text-muted">
        Plans and releases to approve, disputes to rule on, blocked stages to unblock, paused runs to
        resume. A run's clock is stopped while it waits for you.
      </p>
      <Problem message={waiting.error} />
      {waiting.loading && !waiting.data && <p role="status">Loading…</p>}
      {waiting.data?.length === 0 && <p className="text-muted">Nothing is waiting on a person.</p>}
      {waiting.data?.map((run) => (
        <Card
          key={run.run_id}
          title={
            <span className="flex items-center gap-3">
              <Link className="font-mono text-accent hover:underline" href={`/runs/${run.run_id}`}>
                {run.run_id}
              </Link>
              <PhaseBadge phase={run.phase} />
              <span className="text-sm font-normal text-muted">
                waiting for {run.pending.map((p) => label(p.replace("await_", ""))).join(", ")}
              </span>
            </span>
          }
        >
          <ApprovalPanel run={run} onDone={waiting.reload} />
        </Card>
      ))}
    </div>
  );
}

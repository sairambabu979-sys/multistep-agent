"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback } from "react";
import { ApprovalPanel } from "@/components/approval-panel";
import { useSession } from "@/components/auth";
import { DiffView } from "@/components/diff-view";
import { ReviewCenter } from "@/components/review-center";
import { PhaseBadge, Problem, PublicationFacts, RunProgress, usd } from "@/components/run-parts";
import { TestDashboard } from "@/components/test-dashboard";
import { Timeline } from "@/components/timeline";
import { WorkflowGraph } from "@/components/workflow-graph";
import { Card } from "@/components/ui/card";
import { Tabs } from "@/components/ui/tabs";
import type { RecordRow } from "@/lib/api";
import { TERMINAL } from "@/lib/phases";
import { useResource } from "@/lib/use-resource";

const GATE_RECORDS = ["verifications", "tests", "integrations", "security", "releases"] as const;

function TextFile({ runId, name, empty }: { runId: string; name: string; empty: string }) {
  const { api } = useSession();
  const file = useResource(() => api.file(runId, name).catch(() => null), `${runId}/${name}`, 5000);
  if (file.data === undefined) return <p role="status">Loading…</p>;
  if (file.data === null) return <p className="text-sm text-muted">{empty}</p>;
  return name.endsWith(".diff") ? (
    <DiffView patch={file.data} />
  ) : (
    <pre className="whitespace-pre-wrap rounded-md border border-border bg-surface p-4 text-sm">{file.data}</pre>
  );
}

export default function RunPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const { api } = useSession();
  const run = useResource(() => api.run(id), id, 2000);
  const done = run.data ? TERMINAL.has(run.data.phase) : false;
  const timeline = useResource(() => api.timeline(id), `${id}/timeline`, done ? 0 : 2000);
  const workflow = useResource(() => api.workflow(), "workflow");
  const loadRecords = useCallback(async () => {
    const names = ["findings", "negotiation", "arbitration", ...GATE_RECORDS] as const;
    const rows = await Promise.all(names.map((n) => api.records(id, n)));
    return Object.fromEntries(names.map((n, i) => [n, rows[i] ?? []])) as Record<string, RecordRow[]>;
  }, [api, id]);
  const records = useResource(loadRecords, `${id}/records`, done ? 0 : 4000);

  if (run.error && !run.data) {
    return (
      <div className="space-y-3">
        <Link href="/" className="text-sm text-accent">
          ← Mission Control
        </Link>
        <Problem message={run.error} />
      </div>
    );
  }
  if (!run.data) return <p role="status">Loading the run…</p>;
  const detail = run.data;
  const state = detail.state;
  const gates = (["static", "unit", "integration", "security"] as const).map((gate) => ({
    gate,
    holds: state.evidence[gate] !== null && state.evidence[gate] === state.head_tree,
  }));

  return (
    <div className="space-y-6">
      <Link href="/" className="text-sm text-accent">
        ← Mission Control
      </Link>
      <header className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-mono text-2xl font-semibold">{detail.run_id}</h1>
          <PhaseBadge phase={detail.phase} />
          {detail.driving && <span className="text-sm text-muted">running now</span>}
        </div>
        <RunProgress phase={detail.phase} resumePhase={state.resume_phase} revisions={state.revisions} />
        {detail.outcome && (
          <p className="text-sm">
            <span className="font-semibold">{detail.outcome.reason}</span>
            {detail.outcome.detail ? `: ${detail.outcome.detail}` : ""}
          </p>
        )}
        <Problem message={detail.driver_error ? `The driver stopped: ${detail.driver_error}` : null} />
      </header>
      <Tabs
        label="Run views"
        tabs={[
          {
            id: "overview",
            title: "Overview",
            body: (
              <div className="grid gap-4 md:grid-cols-2">
                <Card title={detail.waiting_on_person ? "Waiting on you" : "Actions"}>
                  <ApprovalPanel run={detail} onDone={run.reload} />
                </Card>
                <Card title="Facts">
                  <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
                    <dt className="text-muted">Spent</dt>
                    <dd>
                      {usd(state.ledger.spent.usd_micros)} of {usd(state.ledger.limit_usd_micros)}
                    </dd>
                    <dt className="text-muted">Revisions</dt>
                    <dd>
                      {state.revisions} of {state.limits.max_revisions}
                    </dd>
                    <dt className="text-muted">Base tree</dt>
                    <dd className="font-mono">{state.base_tree?.slice(0, 12) ?? "-"}</dd>
                    <dt className="text-muted">Head tree</dt>
                    <dd className="font-mono">{state.head_tree?.slice(0, 12) ?? "-"}</dd>
                    <dt className="text-muted">Gates on head</dt>
                    <dd>
                      {gates.map((g) => (
                        <span key={g.gate} className={`mr-2 ${g.holds ? "text-good" : "text-muted"}`}>
                          {g.holds ? "✓" : "·"} {g.gate}
                        </span>
                      ))}
                    </dd>
                    <dt className="text-muted">Approved by</dt>
                    <dd>{state.approved_by ?? "-"}</dd>
                  </dl>
                </Card>
                {state.publication && (
                  <Card title="Release">
                    <PublicationFacts publication={state.publication} />
                  </Card>
                )}
              </div>
            ),
          },
          { id: "timeline", title: "Timeline", body: <Timeline runId={id} onEvent={run.reload} /> },
          {
            id: "workflow",
            title: "Workflow",
            body:
              workflow.data && timeline.data ? (
                <WorkflowGraph workflow={workflow.data} timeline={timeline.data} current={detail.phase} />
              ) : (
                <p role="status">Loading the workflow…</p>
              ),
          },
          {
            id: "changes",
            title: "Changes",
            body: <TextFile runId={id} name="patch.diff" empty="No patch yet: it is written when the release is verified." />,
          },
          {
            id: "review",
            title: "Review",
            body: records.data ? (
              <ReviewCenter
                findings={records.data.findings ?? []}
                rounds={records.data.negotiation ?? []}
                rulings={records.data.arbitration ?? []}
              />
            ) : (
              <p role="status">Loading the review…</p>
            ),
          },
          {
            id: "tests",
            title: "Tests and gates",
            body: records.data ? <TestDashboard records={records.data} /> : <p role="status">Loading the gates…</p>,
          },
          {
            id: "pr",
            title: "Pull request",
            body: <TextFile runId={id} name="pr.md" empty="No pull request yet: it is written in final verification." />,
          },
        ]}
      />
    </div>
  );
}

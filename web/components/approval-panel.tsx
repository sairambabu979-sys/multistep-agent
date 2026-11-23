"use client";

import { useState } from "react";
import { useSession } from "@/components/auth";
import { Problem } from "@/components/run-parts";
import { Button } from "@/components/ui/button";
import { can, type RunDetail } from "@/lib/api";
import { TERMINAL } from "@/lib/phases";

const VERDICTS = ["upheld", "dismissed", "deferred"] as const;

/** What a person can do for this run now: only the actions the run is waiting for, and only
 * those the signed-in role may take. The API enforces the same rules; this hides the rest. */
export function ApprovalPanel({ run, onDone }: { run: RunDetail; onDone: () => void }) {
  const { api, me } = useSession();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verdicts, setVerdicts] = useState<Record<string, string>>({});
  const pending = new Set(run.pending);
  const gate = pending.has("await_plan_approval") ? "plan" : pending.has("await_release_approval") ? "release" : null;
  const open = run.state.findings.filter((f) => f.status === "open").map((f) => f.id);

  async function act(action: string, body: unknown = {}) {
    setBusy(true);
    setError(null);
    try {
      await api.act(run.run_id, action, body);
      setText("");
      onDone();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (TERMINAL.has(run.phase)) {
    return <p className="text-sm text-muted">This run is over: there is nothing left to decide.</p>;
  }
  const field = (id: string, title: string) => (
    <div>
      <label htmlFor={id} className="mb-1 block text-sm font-medium">
        {title}
      </label>
      <textarea
        id={id}
        rows={2}
        className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
    </div>
  );

  return (
    <div className="space-y-4">
      {run.question && (
        <details open className="rounded-md border border-wait bg-wait-bg p-3">
          <summary className="cursor-pointer font-semibold text-wait">The question for you</summary>
          <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap text-sm text-text">{run.question}</pre>
        </details>
      )}
      <Problem message={error} />
      {gate && (
        can(me.role, "approver") ? (
          <div className="space-y-2">
            {field("feedback", gate === "plan" ? "Feedback for the Planner (to send it back)" : "What to change (to send it back)")}
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" disabled={busy} onClick={() => void act("approve")}>
                {gate === "plan" ? "Approve the plan" : "Approve and publish"}
              </Button>
              <Button disabled={busy || !text.trim()} onClick={() => void act("reject", { feedback: text })}>
                Send back
              </Button>
              <Button variant="danger" disabled={busy} onClick={() => void act("abort", { reason: text || "stopped" })}>
                Stop the run
              </Button>
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted">An approver must answer this {gate} gate.</p>
        )
      )}
      {pending.has("await_human_ruling") && (
        can(me.role, "approver") ? (
          <fieldset className="space-y-2">
            <legend className="mb-1 text-sm font-medium">Rule on each disputed finding</legend>
            {open.map((id) => (
              <label key={id} className="flex items-center gap-3 text-sm">
                <span className="w-20 font-mono">{id}</span>
                <select
                  className="rounded-md border border-border bg-bg px-2 py-1"
                  value={verdicts[id] ?? ""}
                  onChange={(e) => setVerdicts({ ...verdicts, [id]: e.target.value })}
                >
                  <option value="">choose…</option>
                  {VERDICTS.map((v) => (
                    <option key={v} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <Button
              variant="primary"
              disabled={busy || open.some((id) => !verdicts[id])}
              onClick={() => void act("rule", { verdicts })}
            >
              Rule
            </Button>
          </fieldset>
        ) : (
          <p className="text-sm text-muted">An approver must rule on these findings.</p>
        )
      )}
      {pending.has("await_unblock") && can(me.role, "operator") && (
        <div className="space-y-2">
          {field("note", "What you fixed")}
          <Button variant="primary" disabled={busy} onClick={() => void act("unblock", { note: text })}>
            Unblock and rerun the stage
          </Button>
        </div>
      )}
      {pending.has("await_resume") && can(me.role, "operator") && (
        <Button variant="primary" disabled={busy} onClick={() => void act("resume")}>
          Resume
        </Button>
      )}
      {!run.waiting_on_person && can(me.role, "operator") && (
        <div className="flex flex-wrap gap-2">
          <Button disabled={busy} onClick={() => void act("pause", { reason: "paused from the workbench" })}>
            Pause
          </Button>
          <Button variant="danger" disabled={busy} onClick={() => void act("cancel", { reason: "cancelled from the workbench" })}>
            Cancel
          </Button>
        </div>
      )}
      {!can(me.role, "operator") && <p className="text-sm text-muted">Viewers can follow a run but not change it.</p>}
    </div>
  );
}

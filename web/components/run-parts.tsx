"use client";

// The pieces every run view shares: its phase, its progress, and errors.
import { Badge } from "@/components/ui/badge";
import type { Publication } from "@/lib/api";
import { label, progress, tone } from "@/lib/phases";

export function PhaseBadge({ phase }: { phase: string }) {
  return <Badge tone={tone(phase)}>{label(phase)}</Badge>;
}

export function RunProgress({
  phase,
  resumePhase,
  revisions,
}: {
  phase: string;
  resumePhase: string | null;
  revisions: number;
}) {
  const p = progress(phase, resumePhase, revisions);
  const text =
    p.stage === 0
      ? (p.note ?? label(phase))
      : `Stage ${p.stage} of ${p.of}: ${label(p.doing)}${p.note ? ` (${p.note})` : ""}`;
  return (
    <div>
      <div
        role="progressbar"
        aria-label="Run progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={p.percent}
        aria-valuetext={text}
        className="h-2 w-full overflow-hidden rounded-full bg-surface"
      >
        <div
          className={`h-full ${tone(phase) === "bad" ? "bg-bad" : tone(phase) === "waiting" ? "bg-wait" : "bg-accent"}`}
          style={{ width: `${p.percent}%` }}
        />
      </div>
      <p className="mt-1 text-xs text-muted">{text}</p>
    </div>
  );
}

export function Problem({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="rounded-md border border-bad bg-bad-bg px-3 py-2 text-sm text-bad">
      {message}
    </p>
  );
}

export function usd(micros: number): string {
  return `$${(micros / 1_000_000).toFixed(4)}`;
}

export function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}


// A link only for an http(s) URL: anything else is shown as text, never followed.
function safeUrl(url: string | null): string | null {
  return url && /^https?:\/\//i.test(url) ? url : null;
}

// Where an approved release went: its local branch, and the remote branch and pull
// request when the approval asked for them (ADR-018).
export function PublicationFacts({ publication }: { publication: Publication }) {
  const link = safeUrl(publication.pr_url);
  return (
    <div className="space-y-2">
      <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
        <dt className="text-muted">Published</dt>
        <dd>{publication.mode}</dd>
        <dt className="text-muted">Local branch</dt>
        <dd className="font-mono">
          {publication.branch} at {publication.commit.slice(0, 12)}
        </dd>
        <dt className="text-muted">Pushed to</dt>
        <dd className="font-mono">
          {publication.remote_branch ? `${publication.remote}/${publication.remote_branch}` : "-"}
        </dd>
        <dt className="text-muted">Pull request</dt>
        <dd>
          {link ? (
            <a href={link} target="_blank" rel="noopener noreferrer" className="text-accent underline">
              {link}
            </a>
          ) : (
            (publication.pr_url ?? "-")
          )}
        </dd>
      </dl>
      <Problem
        message={publication.error ? `The ${publication.mode} step did not complete: ${publication.error}` : null}
      />
    </div>
  );
}

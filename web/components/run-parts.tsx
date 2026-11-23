"use client";

// The pieces every run view shares: its phase, its progress, and errors.
import { Badge } from "@/components/ui/badge";
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

import type { ReactNode } from "react";
import type { Tone } from "@/lib/phases";

const TONES: Record<Tone, string> = {
  neutral: "bg-surface text-muted border-border",
  working: "bg-work-bg text-work border-work",
  waiting: "bg-wait-bg text-wait border-wait",
  good: "bg-good-bg text-good border-good",
  bad: "bg-bad-bg text-bad border-bad",
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

// The workflow's stages, in the order a run goes through them, and an honest progress
// figure: which stage the run is doing (or resumes), never an animation or a guess.

export const PIPELINE = [
  "created",
  "validating",
  "context",
  "planning",
  "plan_approval",
  "implementing",
  "static_analysis",
  "unit_testing",
  "integration_testing",
  "security_review",
  "code_review",
  "negotiation",
  "final_verification",
  "human_approval",
  "completed",
] as const;

export const SIDE = ["rework", "blocked", "paused", "failed", "cancelled"] as const;
export const TERMINAL = new Set(["completed", "failed", "cancelled"]);
export const ASKING = new Set(["plan_approval", "blocked", "human_approval", "paused"]);

export function label(phase: string): string {
  const words = phase.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export type Tone = "neutral" | "working" | "waiting" | "good" | "bad";

export function tone(phase: string): Tone {
  if (phase === "completed") return "good";
  if (phase === "failed" || phase === "cancelled") return "bad";
  if (ASKING.has(phase)) return "waiting";
  return "working";
}

export interface Progress {
  stage: number; // 1-based, within the main pipeline
  of: number;
  percent: number;
  doing: string; // the phase whose work this is
  note: string | null; // why the run is off the main line, if it is
}

/** Where a run is, given its phase and, when paused or blocked, the phase it resumes. */
export function progress(phase: string, resumePhase: string | null, revisions = 0): Progress {
  const of = PIPELINE.length;
  let doing = phase;
  let note: string | null = null;
  if ((phase === "paused" || phase === "blocked") && resumePhase) {
    doing = resumePhase;
    note = phase === "paused" ? "paused by an operator" : "blocked: a person must fix the environment";
  } else if (phase === "blocked") {
    doing = "negotiation";
    note = "waiting for a ruling";
  } else if (phase === "rework") {
    doing = "static_analysis";
    note = `rework: revision ${revisions}, every gate runs again`;
  } else if (phase === "failed" || phase === "cancelled") {
    return { stage: 0, of, percent: 0, doing: phase, note: `stopped: ${phase}` };
  }
  const index = PIPELINE.indexOf(doing as (typeof PIPELINE)[number]);
  const stage = index < 0 ? 0 : index + 1;
  return { stage, of, percent: Math.round(((stage - 1) / (of - 1)) * 100), doing, note };
}

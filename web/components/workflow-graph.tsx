"use client";

import type { Workflow } from "@/lib/api";
import { label } from "@/lib/phases";

// Fixed positions: the main line top to bottom, the branches beside the stage they leave.
const SPOTS: Record<string, [number, number]> = {
  created: [0, 0],
  validating: [0, 1],
  context: [0, 2],
  planning: [0, 3],
  plan_approval: [1, 3],
  implementing: [0, 4],
  static_analysis: [0, 5],
  rework: [1, 5],
  unit_testing: [0, 6],
  integration_testing: [0, 7],
  blocked: [1, 7],
  security_review: [0, 8],
  code_review: [0, 9],
  negotiation: [1, 9],
  final_verification: [0, 10],
  human_approval: [0, 11],
  completed: [0, 12],
  paused: [2, 0],
  failed: [2, 12],
  cancelled: [1, 12],
};
const W = 168;
const H = 30;
const GAP_X = 208;
const GAP_Y = 44;

function centre(phase: string): [number, number] {
  const [col, row] = SPOTS[phase] ?? [2, 6];
  return [16 + col * GAP_X + W / 2, 12 + row * GAP_Y + H / 2];
}

/** ``"  5  PLANNING -> IMPLEMENTING  PlanProduced  [...]"`` -> ``["planning", "implementing"]`` */
export function transitions(timeline: string[]): [string, string][] {
  const found: [string, string][] = [];
  for (const line of timeline) {
    const match = /^\s*\d+\s+(\w+) -> (\w+)\s/.exec(line);
    if (match && match[1] !== "START") found.push([match[1]!.toLowerCase(), match[2]!.toLowerCase()]);
  }
  return found;
}

export function WorkflowGraph({
  workflow,
  timeline,
  current,
}: {
  workflow: Workflow;
  timeline: string[];
  current: string;
}) {
  const taken = transitions(timeline);
  const walked = new Set(taken.map(([a, b]) => `${a}>${b}`));
  const visited = new Set(["created", ...taken.flatMap(([a, b]) => [a, b])]);
  const edges = new Map<string, boolean>();
  for (const rule of workflow.rules) {
    for (const target of rule.targets) {
      if (target !== rule.source) edges.set(`${rule.source}>${target}`, walked.has(`${rule.source}>${target}`));
    }
  }
  for (const [a, b] of taken) if (a !== b) edges.set(`${a}>${b}`, true); // escapes, e.g. a pause
  const width = 16 + 3 * GAP_X;
  const height = 12 + 13 * GAP_Y;
  const path = taken.map(([, b]) => label(b));
  return (
    <figure>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full max-w-3xl"
        role="img"
        aria-labelledby="workflow-title"
      >
        <title id="workflow-title">
          {`The workflow's ${workflow.phases.length} states; this run is in ${label(current)}.`}
        </title>
        <defs>
          <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--muted)" />
          </marker>
          <marker id="arrow-on" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--accent)" />
          </marker>
        </defs>
        {[...edges.entries()].map(([key, on]) => {
          const [a, b] = key.split(">") as [string, string];
          const [x1, y1] = centre(a);
          const [x2, y2] = centre(b);
          const bend = x1 === x2 && Math.abs(y2 - y1) > GAP_Y ? -60 : 0;
          return (
            <path
              key={key}
              d={`M${x1},${y1 + (y2 > y1 ? H / 2 : -H / 2)} Q${(x1 + x2) / 2 + bend},${(y1 + y2) / 2} ${x2},${y2 + (y2 > y1 ? -H / 2 : H / 2)}`}
              fill="none"
              stroke={on ? "var(--accent)" : "var(--border)"}
              strokeWidth={on ? 2 : 1}
              markerEnd={on ? "url(#arrow-on)" : "url(#arrow)"}
              opacity={on ? 1 : 0.6}
            />
          );
        })}
        {workflow.phases.map((phase) => {
          const [col, row] = SPOTS[phase] ?? [2, 6];
          const x = 16 + col * GAP_X;
          const y = 12 + row * GAP_Y;
          const here = phase === current;
          const seen = visited.has(phase);
          return (
            <g key={phase}>
              <rect
                x={x}
                y={y}
                width={W}
                height={H}
                rx={6}
                fill={here ? "var(--accent)" : seen ? "var(--work-bg)" : "var(--surface)"}
                stroke={here ? "var(--accent)" : seen ? "var(--work)" : "var(--border)"}
                strokeWidth={here ? 2 : 1}
              />
              <text
                x={x + W / 2}
                y={y + H / 2 + 4}
                textAnchor="middle"
                fontSize={12}
                fontWeight={here ? 700 : 400}
                fill={here ? "var(--accent-text)" : "var(--text)"}
              >
                {label(phase)}
              </text>
            </g>
          );
        })}
      </svg>
      <figcaption className="mt-2 text-sm text-muted">
        <span className="sr-only">Path taken: </span>
        {path.length ? `Path: Created → ${path.join(" → ")}` : "The run has just been created."}
      </figcaption>
    </figure>
  );
}

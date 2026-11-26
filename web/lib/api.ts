// The msa HTTP API, typed. Every call carries the bearer token; nothing else is sent.

export type Role = "viewer" | "operator" | "approver" | "admin";
export const ROLES: readonly Role[] = ["viewer", "operator", "approver", "admin"];

export function can(role: Role | undefined, needed: Role): boolean {
  return role !== undefined && ROLES.indexOf(role) >= ROLES.indexOf(needed);
}

export interface Me {
  name: string;
  role: Role;
}

export interface Outcome {
  reason: string;
  detail: string | null;
}

export interface RunRow {
  run_id: string;
  phase: string;
  outcome: Outcome | null;
  seq: number;
  created_at: string;
  kind: string | null;
  driving: boolean;
}

export interface Finding {
  id: string;
  severity: string;
  status: string;
  raised_on: string;
}

// Where an approved release went (ADR-018): its local branch, and the remote branch and
// pull request when the approval asked for them. A push or pull request that failed has
// its error here; the release is committed either way.
export interface Publication {
  mode: "local" | "push" | "pr";
  branch: string;
  commit: string;
  remote: string | null;
  remote_branch: string | null;
  pr_url: string | null;
  error: string | null;
}

export interface RunState {
  run_id: string;
  seq: number;
  phase: string;
  started_at: string;
  updated_at: string;
  deadline: string;
  paused: number | string;
  waiting_since: string | null;
  revisions: number;
  replans: number;
  negotiation_round: number;
  base_tree: string | null;
  head_tree: string | null;
  findings: Finding[];
  resume_phase: string | null;
  release: string | null;
  approved_by: string | null;
  publication: Publication | null;
  limits: { budget_usd_micros: number; max_revisions: number; plan_gate: string };
  ledger: { limit_usd_micros: number; spent: { usd_micros: number } };
  evidence: Record<"static" | "unit" | "integration" | "security", string | null>;
}

export interface RunDetail {
  run_id: string;
  phase: string;
  outcome: Outcome | null;
  seq: number;
  pending: string[];
  waiting_on_person: boolean;
  driving: boolean;
  driver_error: string | null;
  question: string | null;
  state: RunState;
}

export interface JournalEvent {
  seq: number;
  kind: string;
  event: Record<string, unknown> & { at: string };
}

export interface Rule {
  source: string;
  event: string;
  targets: string[];
}

export interface Workflow {
  phases: string[];
  terminal: string[];
  rules: Rule[];
  escapes: { event: string; target: string; why: string }[];
}

export interface Preference {
  id: string;
  rule: string;
  enforcement: string;
  origin: string;
  scope: { language: string | null; paths: string[] };
}

export interface Skill {
  id: string;
  version: string;
  description: string;
  roles: string[];
  allowed_tools: string[] | null;
  constraints: string[];
  lineage: string[];
}

export interface Policy {
  locked: { id: string; enforcement: string; reason: string }[];
  forbidden_tools: Record<string, string[]>;
  source: string;
}

export interface Metrics {
  runs: number;
  live: number;
  waiting_on_people: number;
  by_phase: Record<string, number>;
  by_outcome: Record<string, number>;
  spent_usd_micros: number;
  paused_seconds: number;
  completed: number;
  mean_revisions_completed: number | null;
  mean_working_seconds_completed: number | null;
}

export type AuditEntry = Record<string, string | number | null>;
export type RecordRow = Record<string, unknown>;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export const DEFAULT_BASE = process.env.NEXT_PUBLIC_AEGIS_API ?? "http://127.0.0.1:8765";

export class Api {
  constructor(
    readonly base: string,
    readonly token: string,
  ) {}

  private headers(json: boolean): HeadersInit {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.token}` };
    if (json) headers["Content-Type"] = "application/json";
    return headers;
  }

  private async call<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.base}/api${path}`, init);
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
        else if (body.detail !== undefined) detail = JSON.stringify(body.detail);
      } catch {
        // not JSON: keep the status text
      }
      throw new ApiError(response.status, detail);
    }
    const type = response.headers.get("content-type") ?? "";
    return (type.includes("application/json") ? response.json() : response.text()) as Promise<T>;
  }

  get<T>(path: string): Promise<T> {
    return this.call<T>(path, { headers: this.headers(false), cache: "no-store" });
  }

  post<T>(path: string, body: unknown = {}): Promise<T> {
    return this.call<T>(path, {
      method: "POST",
      headers: this.headers(true),
      body: JSON.stringify(body),
    });
  }

  me = () => this.get<Me>("/me");
  runs = () => this.get<RunRow[]>("/runs");
  run = (id: string) => this.get<RunDetail>(`/runs/${encodeURIComponent(id)}`);
  events = (id: string, after = 0) =>
    this.get<JournalEvent[]>(`/runs/${encodeURIComponent(id)}/events?after=${after}`);
  file = (id: string, name: string) =>
    this.get<string>(`/runs/${encodeURIComponent(id)}/files/${encodeURIComponent(name)}`);
  records = (id: string, name: string) =>
    this.get<RecordRow[]>(`/runs/${encodeURIComponent(id)}/records/${encodeURIComponent(name)}`);
  timeline = (id: string) => this.get<string[]>(`/runs/${encodeURIComponent(id)}/timeline`);
  workflow = () => this.get<Workflow>("/workflow");
  preferences = () => this.get<Preference[]>("/preferences");
  skills = () => this.get<Skill[]>("/skills");
  policy = () => this.get<Policy>("/policy");
  metrics = () => this.get<Metrics>("/metrics");
  audit = () => this.get<AuditEntry[]>("/audit");
  act = (id: string, action: string, body: unknown = {}) =>
    this.post<{ run: RunDetail }>(`/runs/${encodeURIComponent(id)}/${action}`, body);
  start = (body: { task: string; run_id?: string; simulate?: { seed: number } }) =>
    this.post<{ run_id: string }>("/runs", body);

  /** The run's events as they are journaled. EventSource cannot send a bearer token, so
   * the stream is read with fetch and parsed here. Resolves when the run ends. */
  async stream(
    id: string,
    after: number,
    onEvent: (event: JournalEvent) => void,
    signal: AbortSignal,
  ): Promise<void> {
    const response = await fetch(
      `${this.base}/api/runs/${encodeURIComponent(id)}/stream?after=${after}`,
      { headers: this.headers(false), signal },
    );
    if (!response.ok || response.body === null) {
      throw new ApiError(response.status, response.statusText);
    }
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    const parser = new SseParser();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      for (const frame of parser.push(value)) {
        if (frame.event === "end") return;
        if (frame.data) onEvent(JSON.parse(frame.data) as JournalEvent);
      }
    }
  }
}

export interface SseFrame {
  id?: string;
  event?: string;
  data: string;
}

/** Server-sent events, per the HTML spec's line rules, fed chunk by chunk. */
export class SseParser {
  private buffer = "";

  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    let end: number;
    while ((end = this.buffer.indexOf("\n\n")) >= 0) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      const frame: SseFrame = { data: "" };
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line === "" || line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "data") data.push(value);
        else if (field === "event") frame.event = value;
        else if (field === "id") frame.id = value;
      }
      frame.data = data.join("\n");
      if (frame.event !== undefined || frame.data !== "") frames.push(frame);
    }
    return frames;
  }
}

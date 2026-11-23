# ADR-015: AEGIS: eight roles, a twenty-state workflow, and the stack around a pure core

- **Status:** Accepted (2026-11-22)
- **Refines:** PLAN §1–§3 and §13; supersedes the phase names of PLAN §2.1 (ADR-003 stands)

## Context

AEGIS is the product goal for this repository: a stateful multi-agent platform that turns a
request into a reviewed, tested, secured, documented pull request, with a person in control
of every high-impact decision. It names eight roles, a durable workflow with twenty states,
a stack (FastAPI, Postgres, Temporal, OpenTelemetry, a Next.js workbench), and a definition
of done: one multi-file task, end to end, with seeded bugs caught, a crash survived, and an
auditable PR for a person to approve.

M0–M8 already built most of the engine: a pure, model-checked state machine; a crash-safe
journal; the Planner, Coder, blind Tester, Reviewer and Arbiter; negotiation with
executable arbitration; layered preferences; confined tools; human gates. This record
decides how AEGIS maps onto that, what is added, and what is deliberately not.

The goal itself sets the rule for every choice below: *add complexity only when it
measurably improves quality or reliability.*

## Decision

### 1. The eight roles

| AEGIS role | Where it lives | Model? |
|---|---|---|
| Orchestrator | The pure core (`msa.core`) and the interpreter. No model decides a transition: the transition table does, and every invariant is checked on every step. | No |
| Architect/Planner | The Planner role, with the repository context loaded in CONTEXT. | Yes |
| Preferences/Skills | The preferences skill plus the skills registry (ADR-016): versioned, typed, policy-bounded. | No |
| Implementer | The Coder role, in its own git worktree, with capabilities enforced by the tool layer. | Yes |
| Independent Reviewer | The Reviewer role: read-only, never sees the Coder's reasoning, findings scoped to changed lines (ADR-014). | Yes |
| Code Negotiator | The negotiation protocol and the arbitration ladder: repro test, preference, style rule, then the Arbiter role or a person (ADR-004, ADR-014). Deterministic wherever a rule can decide. | Only the last rung |
| Test Engineer | The Tester role (blind acceptance and repro tests, triage) plus the harness's unit and integration stages, which run tests rather than trust claims. | Partly |
| Release Verifier | FINAL_VERIFICATION: deterministic gates (evidence on the head tree, no open blocking finding, scope, secrets), then the PR bundle. A person approves; no agent can. | No |

The Code Negotiator is not a separate model persona. A separate "judge" model would be
one more voice to persuade. Here it is a procedure: evidence first, rules second, a model
or a person only for what neither can decide.

### 2. The workflow

| State | Entered when | Waits on | Leaves to |
|---|---|---|---|
| CREATED | a run is submitted | `admit_run`: policy and quota admission | VALIDATING, FAILED |
| VALIDATING | admitted | `validate_request`: repository, task, preference conflicts | CONTEXT, FAILED |
| CONTEXT | valid | `load_context`: base tree, repository map, worktrees | PLANNING |
| PLANNING | context loaded | the Planner | PLAN_APPROVAL, IMPLEMENTING |
| PLAN_APPROVAL | `plan_gate = human` | a person | IMPLEMENTING, PLANNING (≤ 2 replans), CANCELLED |
| IMPLEMENTING | plan approved | the Coder and the blind Tester, in parallel, then integration of both trees | STATIC_ANALYSIS |
| STATIC_ANALYSIS | a tree to check | format, lint, types, preference checks | UNIT_TESTING, REWORK |
| UNIT_TESTING | static checks passed | the repository's tests, acceptance tests (fail-before/pass-after), flakes, triage | INTEGRATION_TESTING, REWORK, BLOCKED |
| INTEGRATION_TESTING | unit tests passed | the integration suite, in an isolated environment | SECURITY_REVIEW, REWORK, BLOCKED |
| SECURITY_REVIEW | integration passed | secret scan, insecure-code rules, dependency audit | CODE_REVIEW, REWORK |
| CODE_REVIEW | secure | the Reviewer | NEGOTIATION, FINAL_VERIFICATION |
| NEGOTIATION | blocking findings | rounds, then the arbitration ladder | REWORK, FINAL_VERIFICATION, BLOCKED |
| REWORK | any failed gate or agreed change | the owner (Coder or Tester) | STATIC_ANALYSIS, FAILED (revision cap) |
| FINAL_VERIFICATION | review settled with no change | the release verifier | HUMAN_APPROVAL, REWORK |
| HUMAN_APPROVAL | release verified | a person, then publishing | COMPLETED, REWORK, CANCELLED |
| COMPLETED | published | — | — |
| PAUSED | an operator pauses | an operator resumes | the state it paused in |
| BLOCKED | a ruling or an environment fix is needed | a person | the stage it blocked in, or onward |
| FAILED, CANCELLED | a hard limit, a refusal, an unrecoverable error; or a person stops | — | — |

- **Every gate re-runs after any change.** REWORK always returns to STATIC_ANALYSIS, and
  evidence is bound to tree hashes, so a stage passed for an old tree counts for nothing.
- **Mandatory gates cannot be negotiated.** Static analysis, unit tests, integration tests
  and the security review are deterministic stages with no negotiation edge. Only code
  review findings are negotiated, and a security finding from SECURITY_REVIEW never
  reaches negotiation.
- **No agent approves.** Approval events carry the approver, and the core refuses any
  `agent:` principal. The API refuses approvals from anyone without the approver role.
- **The clock stops for people.** PLAN_APPROVAL, BLOCKED, HUMAN_APPROVAL and PAUSED stop
  the wall clock (ADR-014).
- **BLOCKED replaces dead ends.** An environment failure (a missing tool or service) used
  to fail the run. Now it blocks: a person fixes the environment and unblocks, and the
  stage runs again without paying again for planning and implementation.

### 3. The stack

- **Kept: the pure core and the event-sourced journal.** It is model-checked over every
  reachable state, mutation-tested to zero survivors, and crash-tested with SIGKILL
  (ADR-003, ADR-007).
- **Temporal: an execution backend for the same core, not a replacement.** Temporal's
  determinism requirement for workflow code is exactly what the core already meets: no
  I/O, no clock, no randomness. A Temporal workflow can fold the same `transition` over
  activity results. It would add horizontal scaling and operational tooling, so it is
  worth building once more than one worker is needed. Until it runs against a real
  Temporal server, it is not claimed.
- **Postgres: a second `Journal` implementation behind the same protocol and the same
  contract tests.** SQLite stays the zero-dependency default.
- **FastAPI + Pydantic: the service** that the workbench and other clients use. It
  exposes runs, the event stream (SSE), findings, tests, approvals, preferences and skills.
- **OpenTelemetry: spans for every effect, invocation and tool call**, each tagged with
  the run, effect and policy context.
- **Next.js + React + TypeScript + Tailwind: the workbench**, with shadcn-style components
  built on Radix primitives and vendored into the repository.
- **Sandbox:** commands run in a fresh network namespace (`unshare -rn`) where available,
  so tests have no network at all, on top of ADR-009's confinement. Untrusted repositories
  still need a container (M10).

### 4. Permissions

| Principal | Can |
|---|---|
| `viewer` | read runs, events, findings, tests, preferences |
| `operator` | submit, pause, resume, cancel, unblock runs; edit preferences |
| `approver` | everything an operator can, plus approve plans, rule on disputes, approve releases |
| `admin` | manage tokens, skills, security policies |
| `agent:<role>` | act inside its run through its tools, under its capability set; never approve, never publish, never change policies |

Tokens are hashed at rest; the server never logs them. Every API action is an audit event,
with the principal on it.

## Consequences

- PLAN §2's phase names are replaced. Old journals do not replay; none exist outside tests
  and simulations.
- Three new deterministic stages run on every tree. They are cheap compared with any model
  call.
- The workbench is a second product surface, built on the API, and has its own tests.
- The live demonstration, the Temporal backend and the Postgres backend each depend on
  something this environment does not provide by itself: an API key, and permission to
  run downloaded server binaries. Each is reported as such until it runs.

## Rejected

- **A separate negotiator model.** It adds a persuadable voice without adding evidence.
- **Replacing the core with Temporal.** It would trade a checked, pure model for workflow
  code checked only by tests, for scaling no current deployment needs.
- **Letting security findings be negotiated.** A critical defect is a gate, not a
  discussion.

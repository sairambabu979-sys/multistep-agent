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

Tokens are hashed at rest; the server never logs them. Every change the API makes and every
request it refuses is an audit entry, with the principal on it, written before the response.
Reads are not logged: a polling dashboard would bury the changes. The CLI and the API answer
a run through the same code (`msa.runtime.control`), so a gate cannot be answered one way
from the terminal and another from the browser.

### 5. The stages without a model

- **CREATED** admits a run only within `[admission]`: a budget and wall-clock ceiling, and
  at most `max_live_runs` live runs per repository (parked ones count).
- **VALIDATING** refuses a task that holds a secret (it would be journaled and sent to the
  model); `msa run` refuses it before anything is journaled at all.
- **CONTEXT** records the base commit beside the tree, so publishing has a parent.
- **INTEGRATION_TESTING** runs `[integration].paths` without network where the kernel
  allows (`unshare -rn`; `isolate_network = always` blocks where it cannot). A failure that
  also fails on the base tree is not the change's: it blocks for a person, as does a
  flaky one. Only failures on the head alone go to the Coder. No suite: the stage passes
  with nothing run, and the report and the PR say exactly that.
- **SECURITY_REVIEW** judges only what the change adds: secrets on any added line, and
  insecure calls found in the syntax tree of added Python lines. A dependency manifest the
  change touched is always listed; `forbid_new_dependencies` and `audit_command` can make
  it block. A blocking finding goes to the Tester only when all of them are in its files.
- **FINAL_VERIFICATION** rebuilds the patch and checks it again (non-empty, every path one
  the Coder or the Tester may write, no secret), and requires each gate's own record to
  name the exact tree, so the run files must agree with the journal. Then it writes
  `pr.md` and the bundle.
- **Publishing** commits the tree onto the base commit as `<branch_prefix><run>`, with
  `Approved-by:` naming the person. It never pushes, never moves an existing branch that
  holds anything else, and reuses its own commit when it runs again after a crash.

## Verification

- **The model checker** explores every reachable state at the production default limits,
  with an operator's pause offered in every state: 225,300 states over four
  configurations (78 s). No state is stuck; COMPLETED is reachable from every live
  state; every declared edge is taken and no undeclared one; and pausing then resuming
  returns to exactly the state that was paused, in every state that accepts a pause. It
  found two bugs while this was built: a run cancelled while paused kept its resume state,
  and BLOCKED declared resume edges into stages that can never block.
- **The abstraction behind it** keys a state by which gates hold for the head (heads never
  repeat, and evidence only counts when it names the head) and a terminal state by its
  outcome alone. Both are sound for liveness. Without them the same exploration visits
  about 890,000 states.
- **Mutation testing of the core** catches all 1,475 mutants: 1,472 killed, 3 by timeout.
  Its first run on this core found twelve survivors, all real gaps: unchecked refusal
  messages, a dead parameter, and no corrupted-state test for a run blocked on a ruling.
- **A bug found by a test of the command line**, not by the model checker (which never
  lets time pass a deadline): a run waiting on a person failed with `deadline_exceeded`
  when an operator paused it after the nominal deadline. The clock now stops for every
  event while a person is asked. A regression test fails without the fix.
- **The API** (`tests/unit/service/test_api.py`), on simulated runs that park at every
  question, as live runs do. It shows:
  - each role can do exactly what the table says, and every refusal is audited;
  - release approval, sending a release back, unblocking, ruling, and pause and resume
    all go through the core, with the token's person on the event, and the run is driven
    on;
  - the core's refusals come back as 409;
  - the stream replays every event, in order, then ends;
  - path tricks and files outside the allowlist are refused;
  - a task holding a secret is refused, and is neither stored nor logged.
- **The workbench** (`web/tests`):
  - Unit tests (Vitest) cover the progress figure, the diff and stream parsers, and the
    approval panel's role gating.
  - End to end (Playwright), the production build runs against a real `msa serve` on
    seeded runs. From the browser it approves a release, rules on a dispute, and follows
    each run to its end; a viewer gets no approval buttons; the tabs work from the
    keyboard; the theme choice survives a reload.
  - axe finds no serious or critical WCAG 2.1 AA violation on any page, in either theme.
- **Traces** (`tests/unit/observability/test_tracing.py`): a span per effect, invocation
  and tool call, nested as the work is (effect > invocation > tool), carrying no prompt,
  argument or file content.
- **The definition-of-done run** (`tests/integration/test_demo.py`, snapshot in
  [`docs/demo`](../demo/README.md)). One multi-file task on the inventory fixture goes
  through every stage, with the model scripted and everything else real. The scripted
  Coder makes four deliberate mistakes, and each is caught by the stage built for it:
  - a hard repository preference (`print()`) fails STATIC_ANALYSIS;
  - a blind acceptance test triaged `impl_bug` fails UNIT_TESTING;
  - `eval()` fails SECURITY_REVIEW;
  - a non-atomic `receive()` is caught by the Reviewer and accepted in NEGOTIATION.

  Each is fixed by a revision, four in all. A real `SIGKILL` lands mid-revision. On resume,
  no journaled effect runs again, and the revision in flight replays its paid turns from
  the response store, so its edit is not applied twice. The run ends on a branch a person
  approved, and the PR lists each gate's evidence and what was caught along the way. Two
  probes make the test fail: resuming without the response store, and removing the
  repository's preference layer.
- **The stages run on real worktrees** in `tests/unit/runtime/test_stages.py`, and whole
  runs in `tests/integration`: approved, published to a branch whose tree, parent and
  `Approved-by` trailer are checked. Hand mutants of the base-tree comparison, the
  evidence check and the owner routing were each shown to fail those tests.

## Consequences

- PLAN §2's phase names are replaced. Old journals do not replay; none exist outside tests
  and simulations.
- Three new deterministic stages run on every tree. They are cheap compared with any model
  call.
- The workbench is a second product surface, built on the API, and has its own tests.
- A live-model run, the Temporal backend and the Postgres backend each depend on something
  this environment does not provide by itself: an API key, and permission to run
  downloaded server binaries. Each is reported as such until it runs. The
  definition-of-done run uses a scripted model and says so wherever it is shown.

## Rejected

- **A separate negotiator model.** It adds a persuadable voice without adding evidence.
- **Replacing the core with Temporal.** It would trade a checked, pure model for workflow
  code checked only by tests, for scaling no current deployment needs.
- **Letting security findings be negotiated.** A critical defect is a gate, not a
  discussion.

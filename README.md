# multistep-agent (AEGIS)

A multi-agent coding platform built around a pure, event-sourced state machine. It turns a
request into a reviewed, tested, security-checked pull request that a person approves:

admission → validation → context → planner → coder ∥ blind tester → static analysis →
unit tests → integration tests (no network) → security review → reviewer ⇄ author
negotiation with executable arbitration → release verification → a person approves →
a local branch, pushed and opened as a pull request if you ask, all shaped by a layered
preferences skill.

**Status.** The core workflow, every stage's executor, the human gates (plan, ruling,
release, unblock, pause), the skills registry and security policy, the CLI, and the HTTP
API with roles, a live event stream, an audit log and traces, and the web workbench are
built and tested end to end, with only the model scripted. The journal runs on SQLite or
PostgreSQL, and runs can be driven in-process or by Temporal
([ADR-017](docs/adr/017-postgres-and-temporal.md)). After approval, a release can be pushed
and opened as a pull request ([ADR-018](docs/adr/018-push-and-pull-requests.md)).
Repository commands can run in a container for a repository you do not trust
([ADR-019](docs/adr/019-container-sandbox.md)). The security review audits the pinned
Python dependencies a change adds ([ADR-020](docs/adr/020-dependency-audit.md)). Milestone
status is in [docs/PLAN.md §13](docs/PLAN.md#13-milestones).

**With the real model** it has run ten times (recorded below), from the manual `live run`
workflow (`.github/workflows/live-run.yml`), each run stopping at the release approval:

- **live-1** cost $0.53 of a $5 cap. Unit tests caught a bug in the Tester's own acceptance
  test, and the Tester fixed it. Its integration suite had network: the runner refused the
  network namespace, and `isolate_network = "auto"` fell back.
- **live-2** ran with isolation required and cost $0.40. It was SIGKILLed at seq 6 with the
  Coder mid-invocation, then resumed. No agent invocation already answered ran again,
  and the Coder's resumed invocation replayed 4 of its 7 turns instead of paying for them
  again. Its integration suite ran without network.
- **live-3** ran the round-trip task (`from_csv(to_csv(store))` for any store) for $0.64,
  crashed at seq 6 and resumed in the same way. The fixture's `to_csv` does not quote, so a
  SKU holding a comma breaks it. The Coder saw that and moved `to_csv` to the csv module,
  keeping plain output byte for byte, so the Reviewer found nothing left to raise.

For both crashes, "nothing answered ran again" covers the agent invocations only, 2 of the
5 effects answered before the kill. The stages that call no model left no record a check
could read. Every effect start is now logged (`effects.jsonl`), and the check reads that
log.

- **live-4** ran the thread-safe task for $1.03 and was the first crash checked against every
  effect start. It was SIGKILLed at seq 6 with the Tester mid-invocation. After the resume,
  none of the 5 effects answered before the kill started again, model call or not, and the
  Tester's in-flight effect started again. The Coder put a private lock around every read
  and write and took `items()`'s snapshot under it. The Reviewer raised nothing.

Up to live-4, no live run had raised a review finding: four runs, the last two on tasks
chosen to give review something real to find. So negotiation had not yet run with the real
model, and the scripted demo was still its only evidence (live-9 to live-11 below changed
that).

The workflow's `seeded` mode is for that gap. It plants one known bug in the Coder's first
implementation (`scripts/live_run_seeded.py`): `receive()` applies a delivery note line by
line, so a bad line part-way through leaves the store partly restocked. It is the bug the
scripted demo's Reviewer catches. Every later model call is real: the Tester, the gates, the
Reviewer, the Coder's own fixes or disputes, and the Arbiter. The run's job summary says
first that the implementation was planted. Offline, the seed passes ruff, mypy, the tests
and the security rules, so only review is left to find it
(`tests/integration/test_live_run_seeded.py`).

- **live-6**, the first seeded run, cost $0.25. The real Reviewer raised findings for the
  first time, two of them, both accurate and both about the code not following the real
  plan. The minor one: the plan's listed tests were missing. The nit: each line was split on
  every comma instead of once. Neither is blocking, so by design neither was negotiated.
  **Nothing caught the planted bug.** The task never asks for `receive` to be all or
  nothing, and the Reviewer judged the code against the plan, so the seed planted a design
  choice the task allows, not a defect. That run also showed the PR overclaiming: it said
  both advisory findings were "settled in negotiation". It now says what happened: advisory
  findings are not negotiated, and a blocking finding with no recorded verdict says so.
- **live-7** reran the seed on `restock-atomic`, which states the rule: a bad delivery note
  raises and leaves the store exactly as it was. It cost $0.55. **The planted bug was
  caught and fixed by the real model.** The real Tester's acceptance tests failed on the
  planted tree. The real triage called it an implementation bug and sent it back to the
  Coder (UNIT_TESTING to REWORK). The real Coder's revision checks every line before it
  applies any. Every gate then passed on the delivered tree. The Reviewer raised one nit
  (the tests went into a new file), reported as advisory and not negotiated.

- **live-8** seeded `restock-scale`, whose task forbids scanning or sorting the whole store
  for a line. It planted a `receive()` that is right in every other way but looks each SKU
  up through `items()`, which sorts the whole store once per line. Offline, small tests
  cannot see that. Live, the real Tester's tests caught it anyway: UNIT_TESTING to REWORK,
  as an implementation bug. The real Coder's revision checks SKUs with a dict lookup and
  never calls `items()`. $0.58.

So planted defects have now been caught live by the real roles, twice, through tests and
rework. Negotiation still has only the scripted demo as evidence. Across eight live runs, no
reviewer finding was severe enough to be negotiated, since only blocker and major ones are.
The pipeline explains why. Tests run before review, and a capable Tester turns every rule a
task states into a test, so a planted defect only reaches review if no test can express it.
Those are the findings reviewers rate minor or nit.

So `seeded` now plants one of two things. `code` is the planted bug above. `finding`
plants the Reviewer's first review instead, on `restock-atomic`: one blocking correctness
finding, written without reading the code, that `receive()` is not all or nothing. It
points at the lines of `src/inventory/receiving.py` that the review request lists as
changed, so it is admitted wherever the real Coder put them. Everything else is real: the
Planner, the Coder's implementation and its answer to the finding, the Reviewer's reply, and,
if they still disagree, the Tester's repro test and the Arbiter. The real Tester's
acceptance tests check that rule before review, so the claim is probably false, and what the
real roles do with a probably-false blocking finding is the result. Offline, a stand-in
rebuttal and withdrawal take it to a `withdrawn` verdict
(`tests/integration/test_live_run_seeded.py`).

- **live-9** ran `seeded: finding` on `restock-atomic` and cost $0.57. **This was the first
  negotiation with the real model.** By its own summary, the real Coder's first
  implementation checks every line in one pass and restocks in a second. Every gate passed
  on it, the Tester's acceptance tests included. The planted finding `c0-1` was admitted as a
  blocker: CODE_REVIEW to NEGOTIATION. In the one round, the real Coder contested it and
  the real Reviewer withdrew it, with no code change (0 revisions). NEGOTIATION then went to
  FINAL_VERIFICATION and on to HUMAN_APPROVAL. A `withdrawn` verdict only comes from a
  rebuttal or counterproposal the reviewer assents to (`settle_round`). So the Coder did not
  accept a false blocker to get through, and the finding closed only when the Reviewer
  agreed. The two agreed in one round, so the repro test and the Arbiter did not run. Those
  rungs are still evidenced only offline.

`seeded: dispute` is for those two rungs. It plants the Reviewer's whole side, which never
yields. Its first review raises two blocking findings, written without reading the code: the
same correctness claim, and a design claim that the task itself contradicts (that `receive`
belongs on `Store`, not in the module the task names). Every reply maintains both, so
nothing closes in negotiation and the ladder must decide. The real Tester's repro test
settles the correctness claim. The real Arbiter rules on the design one, because the run's
settings escalate to the Arbiter rather than a person. Offline, with stand-ins for the
Coder, the Tester and the Arbiter, a repro test that really runs on the head tree dismisses
the first claim, and a ruling dismisses the second.

- **live-10** ran `seeded: dispute` on `restock-atomic` and cost $0.64. **Both remaining
  escalation steps ran with the real model.** Every gate passed on the real Coder's first
  implementation (0 revisions). Both planted findings were admitted, `c0-1` as a blocker
  and `c0-2` as major. One negotiation round closed neither, because the planted Reviewer
  never yields, so the run escalated to arbitration (NEGOTIATION to NEGOTIATION,
  `request_arbitration`). The real Tester wrote a repro test for `c0-1`'s claim, and it
  passed when run on the head tree in the sandbox, so the claim was **dismissed by the
  repro test**. That is objective evidence the planted correctness claim was false, which
  live-9 could only infer. The real Arbiter **dismissed** `c0-2`, the design claim the task
  contradicts. The run went on through FINAL_VERIFICATION to HUMAN_APPROVAL.

So every rung of negotiation has now run with the real model:
- the author's move and the reviewer's reply (live-9);
- the repro test (live-10);
- the Arbiter (live-10).

What stays planted is a Reviewer's side: the findings and, in live-10, its refusals.

In live-10 both planted claims were false, and both were dismissed. `seeded: code-dispute`
is for the other case, a true finding. It plants both on `restock`. The Coder's first
implementation is the line-by-line `receive()` of `code`, which that task's tests do not
catch (live-6), so it reaches review. The Reviewer's side is that of `dispute`, so this
time its correctness claim is true. Offline, the repro test fails on the head tree, the
claim is upheld, the Coder's revision makes `receive` all or nothing, and a second review
passes it (`tests/integration/test_live_run_seeded.py`).

- **live-11** ran `seeded: code-dispute` on `restock` and cost $0.60. **A true finding was
  upheld and fixed with the real model.** The planted line-by-line `receive()` passed every
  gate, since `restock`'s tests do not require all or nothing. Review then admitted the
  planted findings `c0-1` (blocker) and `c0-2` (major). One round closed neither, and the
  run escalated. The real Tester's repro test for `c0-1` **failed on the head tree, so the
  claim was upheld**, and the real Arbiter **dismissed** `c0-2`. NEGOTIATION went to REWORK.
  The real Coder's revision passed every gate again, and the repro test now ships with it
  as a regression test (`tests/repro/test_live_11_c0_1.py`). The real Reviewer's second
  review raised one advisory nit, that `receive` restates `restock`'s checks. The run
  reached HUMAN_APPROVAL with 1 revision. Its release was then approved by a person
  (`Approved-by: github:sairambabu979-sys`) and published.

  It also showed the PR overclaiming again. Its summary was the plan's, written before the
  upheld finding: "calls `restock` for each" line, the opposite of the delivered code. A
  revised run's PR now says its summary is the plan's approach and lists what each revision
  changed.

The workflow's `release` job is a live run's human gate. It waits in the `live-release`
environment until that environment's required reviewer approves it in GitHub. It then
approves the run in that person's name (`github:<login>`, read from GitHub's record of the
approval), and the run publishes and completes. If nobody approved, it approves nothing and
fails.

live-3 is the first live run released this way. Its release job refused twice while
`live-release` had no reviewer. Then the repository owner approved it in GitHub, and the run
was approved as `github:sairambabu979-sys` and published to `aegis/live-3` (commit
`b33e5524`, `Approved-by: github:sairambabu979-sys`). It is COMPLETED.

The full design is in [`docs/PLAN.md`](docs/PLAN.md), decisions are in
[`docs/adr/`](docs/adr/), and the generated state diagram is in [`docs/fsm.md`](docs/fsm.md).

## The workflow

Twenty states, every transition validated by code (`src/msa/core/transitions.py`):

| Stage | What happens | No model? |
|---|---|---|
| CREATED | admission: budget and wall-clock ceilings, live runs per repository | yes |
| VALIDATING | the task holds no secret; preferences resolve; the repository has a commit | yes |
| CONTEXT | base commit and tree, worktrees, repository map | yes |
| PLANNING, PLAN_APPROVAL | the Planner; a person approves if `plan_gate = "human"` | |
| IMPLEMENTING | the Coder and the blind Tester in parallel, in separate worktrees | |
| STATIC_ANALYSIS | ruff, the format check, mypy, preference checks, the pytest-config guard | yes |
| UNIT_TESTING | the repository's tests; acceptance tests must fail before and pass after | triage only |
| INTEGRATION_TESTING | the integration suite, without network (`unshare -rn`) where possible | yes |
| SECURITY_REVIEW | secrets and insecure calls in added lines; dependency changes listed; vulnerable pinned additions (pip-audit) | yes |
| CODE_REVIEW, NEGOTIATION | the Reviewer, the author's moves, the arbitration ladder | |
| REWORK | the Coder or the Tester revises; every gate runs again on the new tree | |
| FINAL_VERIFICATION | the patch again: non-empty, in scope, no secret, every gate's record; the PR | yes |
| HUMAN_APPROVAL | a person approves, sends it back, or stops it; then a local branch | yes |
| COMPLETED, FAILED, CANCELLED | the outcome, published once | |
| PAUSED, BLOCKED | an operator paused; or a person must fix the environment or rule | |

The four gates are not negotiable: a security finding goes straight to rework. No agent can
approve, rule, unblock or resume anything: the core refuses any `agent:` principal. Time
spent waiting on a person does not count against the run's wall clock.

## Try it

Requires [uv](https://docs.astral.sh/uv/). Preference commands also require
[ripgrep](https://github.com/BurntSushi/ripgrep) to validate regex checks when loading them.

```bash
uv sync
```

```bash
uv run msa simulate --seed 11 --run-id demo
```

```bash
uv run msa inspect demo --at 9
```

```bash
uv run msa replay demo
```

`simulate` prints one line per committed transition. `inspect --at` shows the state as of any
past event. `replay` re-folds the whole journal from scratch and checks every snapshot
against it. Interrupt a run with Ctrl-C and `msa resume <run>` continues it: completed
effects are never executed again.

Run the agent on a repository of your own (it needs `ANTHROPIC_API_KEY`):

```bash
uv run msa run --repo path/to/repo --task "Add a --verbose flag to the CLI"
```

The checkout is never modified. The work happens in git worktrees under `.msa/worktrees/`.
When every gate has passed, the run writes the pull request to `.msa/runs/<run>/pr.md`
(with `patch.diff` beside it) and waits for you. It lists any risks the Planner named, such
as an ambiguity in the task it had to resolve by guessing: no gate checks those, so you are
the one who judges them. Approve it, and it is committed onto the base commit as the
branch `aegis/<run>`, with `Approved-by:` naming you. By default nothing is pushed:

```bash
uv run msa approve <run>
```

To push the branch, or push it and open the pull request with `pr.md` as its body, set
`[publish].mode` to `"push"` or `"pr"` in `msa.toml`, or ask for it once (ADR-018):

```bash
uv run msa approve <run> --publish pr
```

The push uses your git credentials. The pull request uses `gh` if it is logged in,
otherwise the GitHub API with the token in `$GITHUB_TOKEN` (`[publish].token_env`); the
token is never written anywhere. A push or pull request that fails is recorded in the run
(`msa inspect <run>`, the API, the workbench's run view) and the release stays committed.

```bash
uv run msa approve <run> --reject "rename the flag to --debug"
```

Whenever a run needs you, it parks, writes the question to `.msa/runs/<run>/question.md`,
and stops its clock until you answer. Besides the release, that is a plan gate (with
`plan_gate = "human"`), a dispute nothing objective could settle, or an environment problem
no agent can fix:

```bash
uv run msa rule <run> c0-1=upheld
```

```bash
uv run msa unblock <run> --note "started postgres"
```

Pause a run, and continue it later where it stopped:

```bash
uv run msa pause <run>
```

```bash
uv run msa resume <run>
```

Every answer is recorded with who gave it (`--by`, your login by default).

### The HTTP API

`msa serve` exposes the same runs and gates over HTTP, on 127.0.0.1 by default, for the
workbench and any other client ([`src/msa/service`](src/msa/service)). Every request but
`/api/health` needs a bearer token, and each route needs a role: **viewer** reads,
**operator** starts, pauses, resumes, cancels and unblocks, **approver** approves, sends
back, rules and aborts, **admin** reads the audit log. A token names one person, who is
recorded on every event the API journals for them; a token cannot belong to an agent.

```bash
uv run msa tokens add grace --role approver
```

```bash
uv run msa serve
```

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:8765/api/runs/<run>/stream
```

### The workbench

[`web/`](web) is a Next.js, React, TypeScript and Tailwind app over that API, with
shadcn-style components vendored in `web/components/ui` (Radix tabs underneath):

- **Mission Control:** live runs, what waits on a person, run history, and starting a
  simulated run.
- **The run page** has the agent timeline (streamed live), the workflow graph drawn from the
  transition table with the run's path, the code diff, the Review and Negotiation Center,
  the test and gate dashboard, the pull request, and the actions the run is waiting for.
  The progress bar shows the stage the run is actually in ("stage 14 of 15: Human approval"),
  never an animation.
- **The Approval Center** lists every question for a person in one place.
- **Metrics** are computed by the API from every run's state.
- **The Preferences Studio** shows preferences with their origin, the security policy and
  the skills.

Light and dark themes follow the system unless you pick one. Everything works from the
keyboard, with a skip link and arrow-key tabs. Every page is checked with axe in both
themes. The token stays in the browser tab, and is gone when the tab closes.

```bash
uv run msa serve
```

```bash
cd web && npm ci && npm run dev
```

`/stream` sends each journaled event as a server-sent event (reconnect with `?after=`),
then `end`. Every change and every refused request goes to the audit log, without the
token. Traces (one span per effect, agent invocation and tool call) are printed with
`[observability] traces = "console"`.

## What exists today

**The core** (`src/msa/core`) is a pure function:

```python
step = start(RunStarted(...))         # -> Step(state, effects, edge)
step = transition(step.state, event)  # raises TransitionRejected for stale/duplicate/illegal events
state = replay(journal)               # fold the journal back into the exact same state
```

It performs no I/O and reads no clock or randomness, which a test enforces. Every transition:

- rejects events for effects that aren't pending, so delivery is idempotent;
- binds evidence to the git tree it checked, so stale test results can't finalize a change;
- funds LLM effects from disjoint budget reservations, so parallel work can't overspend;
- runs all invariants in production, not only in tests.

**The runtime** (`src/msa/runtime`) is the imperative shell:

- **The interpreter** launches pending effects, stamps each completion with the clock, and
  commits it to the journal before acting on it.
- **The journal** is append-only, with compare-and-set appends, snapshots,
  schema-versioned events, and upcasters.
  - It runs on SQLite (WAL, `synchronous=FULL`) or on PostgreSQL (`[journal].url`, with
    synchronous commits), and both pass the same contract and crash tests.
  - A PostgreSQL URL may not hold a password; libpq reads `PGPASSWORD` or `~/.pgpass`.
- **Temporal** (`msa.runtime.temporal`, optional) drives the same core as a durable
  workflow: effects are activities, people's answers are signals, and the journal stays the
  record. With `[temporal].address` set:
  - `msa worker` drives runs;
  - `--temporal` starts or hands runs over;
  - answers from the CLI and the API reach the workflow.
- **The simulator** answers every effect deterministically from `(seed, effect)`.

Crash semantics are in [ADR-007](docs/adr/007-commit-point-and-crash-semantics.md).

**The LLM gateway** (`src/msa/llm`) runs one agent invocation as a bounded tool loop on the
Messages API ([ADR-008](docs/adr/008-llm-gateway.md)):

- every request follows the model family's rules: adaptive thinking, explicit effort, a
  strict answer schema, strict tools, and no sampling parameters, forced `tool_choice` or
  prefill;
- the transcript is append-only by type, and the API is told to reject any edited history;
- each turn's `max_tokens` is planned so its worst case fits the remaining reservation, and
  refusal fallbacks are billed per attempt at each model's price;
- failures after the stream started, which the SDK cannot retry, are retried by the gateway;
- paid turns are stored before use, so an invocation re-run after a crash replays them.

Check a real connection (a two-turn tool round trip, well under a cent):

```bash
ANTHROPIC_API_KEY=... uv run msa llm ping
```

`--record PATH` also writes a cassette and `--replay PATH` answers from one offline; see
[`tests/cassettes/README.md`](tests/cassettes/README.md).

**The agents** (`src/msa/agents`, `src/msa/runtime/live.py`,
[ADR-011](docs/adr/011-first-slice-worktree-and-stand-ins.md)):

- the **Planner** reads the base tree with read-only tools and answers a typed `Plan`, with
  acceptance criteria a test can check;
- the **Coder** implements it in the run's worktree with every tool, and revises when a
  check fails;
- the **Tester** writes acceptance tests at the same time, in its own worktree at the base
  tree, so it never sees the implementation it is testing;
- **STATIC_ANALYSIS** runs ruff, the ruff format check, mypy and the preference checks on
  the head tree, every one of them, and the failure report goes back to the Coder verbatim;
- **UNIT_TESTING** proves the acceptance tests mean something: each must fail on the base tree
  and pass on the head; a failing test is rerun alone, and a flaky one is reported and sent
  back, never skipped; consistent failures are triaged to the Coder or the Tester; and a
  Tester revision that drops a check is refused
  ([ADR-012](docs/adr/012-tester-and-evidence.md));
- **no test that did not run counts**: skipped, xfailed and uncollected acceptance tests
  are refused; they run in a pytest session of their own, under a pytest configuration the
  Coder may not change; and every check runs on exactly the files that will be delivered
  ([ADR-013](docs/adr/013-evidence-integrity.md)).
- the **Reviewer** reads the change, never the Coder's reasoning, and raises findings
  only on lines the change touched (after a revision: lines the revision touched);
- **NEGOTIATION** has the Coder answer each finding (accept, rebut with evidence,
  counter, defer) and the Reviewer reply; a finding closes only by an accept or the other
  side's assent, and silence closes nothing;
- the **arbitration ladder** settles what is left, cheapest rung first: a repro test the
  Tester writes for correctness and security disputes (if it fails on the head tree, the
  finding is upheld and the test ships as a regression test), the preference store,
  "taste never blocks", and only then the Arbiter or a person
  ([ADR-014](docs/adr/014-review-negotiation-arbitration.md));
- **INTEGRATION_TESTING** runs the integration suite in a network namespace of its own; a
  test that fails on the base tree too is not blamed on the change, and blocks for a person;
- **SECURITY_REVIEW** finds secrets and insecure calls (`eval`, shell commands, unpickling,
  unsafe YAML, TLS verification off) in the syntax tree of what the change adds, and lists
  every dependency manifest it touched. It audits the pinned Python dependencies the
  change adds with pip-audit, and a known vulnerability blocks; an audit that cannot run
  says why without blocking, and `[security].audit_command = []` turns it off
  ([ADR-020](docs/adr/020-dependency-audit.md));
- the **Release Verifier** rebuilds the patch, checks it once more, and writes the PR;
  a person approves it ([ADR-015](docs/adr/015-aegis-architecture.md)).

The harness, not an agent, turns the worktree into the tree hashes evidence is bound to.

**The tool layer** (`src/msa/tools`, [ADR-009](docs/adr/009-tool-layer-and-local-sandbox.md))
is what agents act through: `read_file`, `search`, `edit`, `create_file`, `run` and
`git_diff`.

- Every path is resolved once and confined to the worktree, then opened component by
  component without following symlinks, so a symlink swapped in mid-call is refused.
- Writes are checked against the role's capability (ADR-006) on the *resolved* path, and
  land atomically.
- Commands are an allowlist with typed arguments, a scrubbed environment, rlimits, a
  timeout and an output cap.
- Refusals go back to the model as tool errors. Security refusals are also recorded for
  the operator.

The local sandbox is for trusted repositories: it confines paths, but it is not a network
or syscall boundary. For a repository you do not trust, set `[sandbox].backend =
"container"` ([ADR-019](docs/adr/019-container-sandbox.md)). The agents' commands and the
test gates then run in a fresh Docker or Podman container each:
- the worktree is mounted, and nothing else is writable;
- `--network none`, a non-root user, every capability dropped;
- CPU, memory, process and time limits.

`[sandbox.container].image` must hold the repository's toolchain and dependencies, since
nothing is installed in a container without network. For example:

```dockerfile
FROM python:3.13-slim
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt pytest ruff mypy
```

With no usable runtime, a run stops before it starts.

**Preferences** (`src/msa/skills/preferences`, [ADR-010](docs/adr/010-preferences.md)) are
your coding rules. Each one has a scope, an enforcement level (hard, soft, info or off) and,
where a linter can enforce it, a check. They come in layers, lowest first:

1. built-in defaults;
2. your user layer, `~/.config/msa/preferences.yaml`;
3. the repo layer, `.msa/preferences.yaml`;
4. `--pref <id>=<level>` for a single run.

Layers merge field by field, and a run fails in VALIDATING if two hard rules contradict each
other where both apply.

```bash
uv run msa prefs list
```

```bash
uv run msa prefs explain src/app.py:42
```

```bash
uv run msa prefs check
```

**Skills** (`src/msa/skills/registry.py`, [ADR-016](docs/adr/016-skills-and-security-policy.md))
are versioned instructions for a role: how this repository writes migrations, say, with the
tools that takes and what it hands back. A role's skills join its system prompt and narrow
its tools. Versions are immutable, and a skill that extends another can only narrow it:

```bash
uv run msa skills add migrations.yaml
```

```bash
uv run msa skills show py.migrations
```

**The security policy** (`.msa/policy.yaml`) outranks every preference and every skill: a
locked preference keeps its enforcement whatever a layer or `--pref` says, and a forbidden
tool is never offered to that role. Skills, preferences and the policy all refuse to store
anything that looks like a secret.

```bash
uv run msa policy show
```

## How it's verified

| Layer | What it shows |
|---|---|
| Exhaustive model check | Every reachable state at the default limits, with an operator's pause offered in each (about 225k across 4 configurations): none stuck, all can still reach COMPLETED, every declared edge taken, and pause then resume returns to exactly the state paused |
| Crash recovery | Forked runs `SIGKILL` themselves up to three times at random boundaries. Each resumes to *exactly* the uninterrupted final state, and no journaled effect executes again. |
| Hypothesis stateful | Random interleavings, including overspend, deadlines, cancellation, redelivered and forged events. The journal replays exactly and survives a JSON round trip. |
| Property tests | Every run terminates and publishes exactly one outcome. Every journal prefix replays. Interpreted runs execute each effect exactly once. |
| Mutation testing | All 1,475 mutants of the core are caught: 1,472 killed, 3 timed out by hanging (zero-survivor gate, nightly) |
| Static analysis | Reachability and liveness over the declared transition graph (`msa fsm check`) |
| LLM request contract | Generated multi-turn scripts (parallel and sequential tools, pauses, mid-output fallbacks): every request is well-formed, the cached prefix never changes, `messages` only grows, nothing from a declined attempt is echoed |
| SDK adapter | A mock transport serving SSE streams pins paths, headers, error classes and mid-stream failures at the locked SDK version |
| Path confinement | Fuzzed hostile paths agree with a reference resolver and never read, write or change anything they must not. Every known escape is always tried. Each property was shown to fail with its defence removed. |
| Symlink race | About 100k atomic directory/symlink swaps per run while tools work through them. Several hundred calls per run hit the race window, and all are refused. |
| Preference merge laws | Associativity, identity, idempotence and right bias of the layer merge (Hypothesis) |
| Conflict soundness | Scope overlap never misses a path both scopes apply to. The test fails when the overlap rule is weakened. |
| End-to-end runs | I-01–I-10 and I-14–I-17 on a fixture repository: real admission, validation, worktrees, tool layer, ruff and pytest in the sandbox, every gate, repro tests, parking and `msa rule`, release approval and publishing to a branch, with only the model scripted. I-01's event sequence must match a golden file. |
| Definition-of-done run | One multi-file task through every stage ([docs/demo](docs/demo/README.md)). The scripted Coder makes four deliberate mistakes, and each is caught by its own stage: a hard repository preference, a triaged acceptance test, `eval()` in security review, and a Reviewer finding accepted in negotiation. A real `SIGKILL` lands mid-revision; the resumed run re-executes nothing already journaled, replays the revision's paid turns, and ends on an approved branch whose PR lists every gate's evidence and what was caught. |
| PostgreSQL journal | The journal contract tests, the crash-recovery suite and the API tests run on PostgreSQL too (CI's `backends` and `check` jobs, against `postgres:17`), plus a race between two connections in which exactly one append must win |
| Temporal driver | Tested against Temporal's test server (CI's `backends` job). A Temporal-driven run must end in exactly the interpreter's state (three seeds, both journals), and its history must replay deterministically. It must park on signals and honor a cancel. A worker SIGKILLed mid-effect must hand the run to another without re-running anything journaled. The API and the CLI start, answer and complete runs on it. |
| Stages without a model | Each on a real worktree: a suite the change broke goes to the Coder, one broken before the run blocks; an isolated suite cannot reach the network; secrets block without being repeated; publishing is idempotent and never moves someone else's branch. Hand mutants of each rule were shown to fail these tests. |
| Negotiation rules | Silence never closes a finding, only the other side's assent closes a contested one, and admission never widens a finding's scope (Hypothesis) |
| The workbench | Unit tests of progress, the diff and stream parsers, and the approval panel's role gating (Vitest). End to end, the real workbench drives a real `msa serve` (Playwright) to approve a release, rule on a dispute and follow the run to the end, from the keyboard, with axe finding no serious or critical WCAG 2.1 AA violation on any page in either theme. |
| Dependency audit | Every locked Python dependency, dev tools and extras included, is checked against PyPI's advisory data (`make audit`, CI's `audit` job), and the workbench's production npm dependencies with `npm audit` |
| Network isolation | CI lets unprivileged users create network namespaces, which Ubuntu 24.04 refuses by default, and then requires them (`MSA_REQUIRE_NETNS=1`): an agent command and an integration suite must each fail to reach the network, and a runner that cannot isolate fails those tests instead of skipping them. Before this, CI skipped both. |
| Live crash | The `live run` workflow SIGKILLs the real-model run's whole process tree once its journal reaches a given seq, then resumes it with `msa resume`. Its summary fails the job if any effect the journal had answered before the kill started again, model call or not: every effect start is logged durably before it runs (`effects.jsonl`). It lists the turns the resumed invocations replayed instead of buying them again. The same kill, from outside, lands while the unit-test stage's pytest runs in the sandbox, and the resumed run completes (`tests/integration/test_crash_mid_command.py`). A kill that missed the sandboxed pytest was shown to fail that test. |
| Contracts | Core purity (AST allowlist), schema completeness, `docs/fsm.md` freshness, layering (`import-linter`), only `msa.llm` imports the SDK, no credential in any cassette |

## The eval (PLAN §11)

Every part of the system has to earn its cost on an eval. A part that doesn't gets turned
off by default. `evals/` holds the corpus:
- **Tasks:** 20 hand-built ones on four small base repositories (8 bug fixes, 6 features,
  4 refactors, and 2 traps: an ambiguous spec and a task that cannot be done offline).
- **Hidden tests:** each task to resolve has tests the agents never see.
- **Reference solutions:** each also has a solution laid over its base repository.
- **The rule:** `evals/decision.toml` is the decision rule, registered before the first
  paid run.

```bash
uv run msa eval check                      # every task is fit to score; spends nothing
uv run msa eval run --repeats 3 --max-total-usd 120  # arms B and D, 3 times, real model
uv run msa eval report .msa/evals          # the metrics, and what the rule decides
```

- **What check proves.** A task is fit to score only if, on its base repository and with
  its reference:
  - the static gate (ruff, ruff format and mypy, with the system's own command lines)
    passes;
  - the repository's visible tests pass;
  - the hidden tests fail on the base, and all pass with the reference.

  All 20 are fit. Tests show the check failing a task with nothing to do, a reference that
  resolves nothing, and a reference the gate rejects.
- **How run scores.** Each (task, arm) is one live run on a fresh copy of the base
  repository, with the same interpreter as `msa run`. It escalates to the Arbiter, since
  nobody is there. A run that reaches HUMAN_APPROVAL has delivered. Its tree is exported and
  scored by the hidden tests in the sandbox, without network. So a change the run's own
  gates accepted can still be delivered and not resolved, which a test shows with a fix that
  passes the Tester's test but not the hidden ones.
- **Resuming, retries and caps.** Results are appended to `results.jsonl` as each run
  finishes, so a rerun resumes without paying twice. A run that raises (the harness, the
  model client or the scoring broke) is recorded as an error, not evidence, and tried once
  more, so one broken run doesn't leave an arm short of the 54 results the rule needs. Its
  spend, from its model calls' records, stays in the results and counts against
  `--max-total-usd`, which stops new runs once that much is spent.
- **Arms.** B is every stage, with the Reviewer as a pass-through stand-in. D is the full
  system. A (no Tester) and C (the author must accept every finding) are refused for now:
  A needs the unit-test stage to accept a run without acceptance tests, and C needs a
  negotiation that cannot dispute.
- **What report measures.**
  - Per arm: resolve rate (overall and by kind), cost per resolved task, wall clock,
    revisions, negotiation rounds, a false-blocker proxy, PLAN §4's degenerate-negotiation
    flags, and the cache hit rate.
  - The decision: it applies `decision.toml`. Arm D stays enabled only if it gains 10
    points of resolve rate, a one-sided exact paired test (McNemar) gives p ≤ 0.05, and it
    costs no more than twice as much per resolved task, over 54 results per arm (every task
    three times). On one pass of 18 tasks, no gain under 28 points could pass the test.
  - For a person: whether each trap's plan named the real problem, and the findings the
    system acted on, so reviewer precision can be judged.
- **The workflow.** The `eval` workflow runs it on demand, with a cap per run and on the
  whole eval, after `msa eval check`.

No paid eval has run yet.

## Develop

```bash
make check      # lint, mypy --strict, architecture contracts, FSM analysis, tests, coverage gates
```

```bash
make nightly    # 10,000 Hypothesis examples per property, 100 crash trials, mutation testing
```

```bash
make mutation   # mutation-test the core; fails if any mutant survives
```

```bash
make docs       # regenerate docs/fsm.md and schemas/msa.schema.json
```

```bash
make changelog  # regenerate CHANGELOG.md from commit subjects
```

Settings (model ids, efforts, beta flags, prices, limits, journal path) live in
[`msa.toml`](msa.toml); check them with `uv run msa config check`. Commit subjects follow
[Conventional Commits](https://www.conventionalcommits.org/), checked by a `commit-msg`
hook (`uv run --with pre-commit pre-commit install`) and in CI.

## License

[MIT](LICENSE).

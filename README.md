# multistep-agent

A multistep coding agent built around a pure, event-sourced state machine:
planner → coder ∥ blind tester → deterministic verification → reviewer ⇄ author
negotiation with executable arbitration → finalize, all shaped by a layered
preferences skill.

**Status:** M0 (skeleton), M1 (the state machine), M2 (journal and interpreter), M4
(tools, capabilities and the local sandbox) and M5 (preferences) are done. M3 (the LLM
gateway) is built and contract-tested offline; its live cassette awaits a recording with an
API key. M6 (the first end-to-end slice) is built: `msa run` plans, implements and verifies
a change with the real Planner and Coder. The Tester (M7) and Reviewer (M8) stages pass
through until they exist, and the run report says so. The full design is in [`docs/PLAN.md`](docs/PLAN.md), decisions are in
[`docs/adr/`](docs/adr/), and the generated state diagram is in [`docs/fsm.md`](docs/fsm.md).

## Try it

Requires [uv](https://docs.astral.sh/uv/).

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

The checkout is never modified. The work happens in a git worktree under `.msa/worktrees/`,
and the result is a patch plus a report in `.msa/runs/<run>/` (`patch.diff`, `report.md`).
Apply the patch with `git apply`, or don't.

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
- **The journal** (SQLite, WAL, `synchronous=FULL`) is append-only, with compare-and-set
  appends, snapshots, schema-versioned events, and upcasters.
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
- **VERIFYING** runs ruff, the ruff format check, mypy, pytest and the preference checks on
  the head tree, every one of them, and the failure report goes back to the Coder verbatim.

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
or syscall boundary.

**Preferences** (`src/msa/skills/preferences`, [ADR-010](docs/adr/010-preferences.md)) are
your coding rules. Each one has a scope, an enforcement level (hard, soft, info or off) and,
where a linter can enforce it, a check. They come in layers, lowest first:

1. built-in defaults;
2. your user layer, `~/.config/msa/preferences.yaml`;
3. the repo layer, `.msa/preferences.yaml`;
4. `--pref <id>=<level>` for a single run.

Layers merge field by field, and a run fails at intake if two hard rules contradict each
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

## How it's verified

| Layer | What it shows |
|---|---|
| Exhaustive model check | Every reachable state at the default limits (about 81k): none stuck, all can still reach DONE, every declared edge taken |
| Crash recovery | Forked runs `SIGKILL` themselves up to three times at random boundaries. Each resumes to *exactly* the uninterrupted final state, and no journaled effect executes again. |
| Hypothesis stateful | Random interleavings, including overspend, deadlines, cancellation, redelivered and forged events. The journal replays exactly and survives a JSON round trip. |
| Property tests | Every run terminates and publishes exactly one outcome. Every journal prefix replays. Interpreted runs execute each effect exactly once. |
| Mutation testing | All 1,070 mutants of the core are caught: killed, or timed out by hanging (zero-survivor gate, nightly) |
| Static analysis | Reachability and liveness over the declared transition graph (`msa fsm check`) |
| LLM request contract | Generated multi-turn scripts (parallel and sequential tools, pauses, mid-output fallbacks): every request is well-formed, the cached prefix never changes, `messages` only grows, nothing from a declined attempt is echoed |
| SDK adapter | A mock transport serving SSE streams pins paths, headers, error classes and mid-stream failures at the locked SDK version |
| Path confinement | Fuzzed hostile paths agree with a reference resolver and never read, write or change anything they must not. Every known escape is always tried. Each property was shown to fail with its defence removed. |
| Symlink race | About 100k atomic directory/symlink swaps per run while tools work through them. Several hundred calls per run hit the race window, and all are refused. |
| Preference merge laws | Associativity, identity, idempotence and right bias of the layer merge (Hypothesis) |
| Conflict soundness | Scope overlap never misses a path both scopes apply to. The test fails when the overlap rule is weakened. |
| End-to-end runs | I-01, I-02 and I-14 on a fixture repository: real intake, worktree, tool layer, ruff and pytest in the sandbox, and finalize, with only the model scripted. I-01's event sequence must match a golden file. |
| Contracts | Core purity (AST allowlist), schema completeness, `docs/fsm.md` freshness, layering (`import-linter`), only `msa.llm` imports the SDK, no credential in any cassette |

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

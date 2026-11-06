# multistep-agent

A multistep coding agent built around a pure, event-sourced state machine:
planner → coder ∥ blind tester → deterministic verification → reviewer ⇄ author
negotiation with executable arbitration → finalize, all shaped by a layered
preferences skill.

**Status:** M0 (skeleton), M1 (the state machine) and M2 (journal and interpreter) are
done. M3 (the LLM gateway) is built and contract-tested offline; its live cassette awaits a
recording with an API key. Real agents arrive in M6–M8; until then every effect runs
against deterministic stub executors. The full design is in [`docs/PLAN.md`](docs/PLAN.md), decisions are in
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

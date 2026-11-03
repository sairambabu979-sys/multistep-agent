# multistep-agent

A multistep coding agent built around a pure, event-sourced state machine:
planner → coder ∥ blind tester → deterministic verification → reviewer ⇄ author
negotiation with executable arbitration → finalize, all shaped by a layered
preferences skill.

**Status:** M0 (skeleton), M1 (the state machine) and M2 (journal and interpreter) are
done. Real agents arrive in M6–M8; until then every effect runs against deterministic
stub executors. The full design is in [`docs/PLAN.md`](docs/PLAN.md), decisions are in
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

## How it's verified

| Layer | What it shows |
|---|---|
| Exhaustive model check | Every reachable state at the default limits (about 81k): none stuck, all can still reach DONE, every declared edge taken |
| Crash recovery | Forked runs `SIGKILL` themselves up to three times at random boundaries. Each resumes to *exactly* the uninterrupted final state, and no journaled effect executes again. |
| Hypothesis stateful | Random interleavings, including overspend, deadlines, cancellation, redelivered and forged events. The journal replays exactly and survives a JSON round trip. |
| Property tests | Every run terminates and publishes exactly one outcome. Every journal prefix replays. Interpreted runs execute each effect exactly once. |
| Static analysis | Reachability and liveness over the declared transition graph (`msa fsm check`) |
| Contracts | Core purity (AST allowlist), schema completeness, `docs/fsm.md` freshness, layering (`import-linter`) |

## Develop

```bash
make check      # lint, mypy --strict, architecture contracts, FSM analysis, tests, coverage gates
```

```bash
make nightly    # 10,000 Hypothesis examples per property and 100 crash trials
```

```bash
make docs       # regenerate docs/fsm.md from the transition table
```

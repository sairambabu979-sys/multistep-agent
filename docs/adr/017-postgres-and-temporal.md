# ADR-017: PostgreSQL storage and Temporal orchestration, around the same core

- **Status:** Accepted (2026-11-23)
- **Refines:** ADR-007 (commit point and crash semantics), ADR-015 §3 (the stack)

## Context

AEGIS names PostgreSQL for storage and Temporal for orchestration. Until now the journal
was SQLite, and runs were driven by the in-process interpreter. Both are sound and tested,
but they bound a deployment to one machine: one file holds the journal, and a run lives
only as long as the process driving it. Nothing about the core forces either: it is a
pure function from state and event to the next state, and every effect is idempotent by
its id.

## Decision

1. **One journal, two databases.** `SqlJournal` holds every rule of the journal:
   - compare-and-set appends;
   - contiguous sequences;
   - snapshots as caches;
   - `verify` re-folding from scratch.

   `SqliteJournal` and `PostgresJournal` supply only a connection, transactions, a dialect
   and a migration. PostgreSQL commits synchronously on every connection, and its
   migration takes an advisory lock, so two processes starting at once cannot race.
   `[journal].url` selects PostgreSQL, in `[journal].schema_name`. Artifacts, tokens and
   the audit log stay in the state directory either way.
2. **No password in settings.** A journal URL that holds a password is refused, and the
   refusal does not repeat it. libpq reads `PGPASSWORD` or `~/.pgpass`.
3. **A second driver, not a second core.** `RunWorkflow` (`msa.runtime.temporal`) drives
   the same core:
   - it folds events through `transition` inside the workflow, which is deterministic
     because the core is pure;
   - each pending effect is an activity;
   - people's answers, pause and cancel are signals;
   - the deadline is a server timer.

   The interpreter's per-effect rules are shared functions (`execute_effect`, `decide`,
   `snapshot_due`, `failure`), so both drivers park, fail and snapshot alike.
4. **The journal stays the system of record.** The workflow commits every event through an
   activity before it adopts the new state, as the interpreter commits before it acts.
   `msa inspect`, the API and the workbench therefore read a Temporal-driven run like any
   other. Every activity is safe for Temporal to retry:
   - `create` and `commit` recognize their own earlier attempt;
   - `execute` runs an effect only while the journal has it pending, so an effect whose
     completion was journaled never runs again (ADR-007).
5. **Completions in launch order.** The workflow takes the oldest started activity's
   result first. That is deterministic whatever the timing. It costs little: the stages
   that run several effects at once wait for all of them anyway. A simulated run
   therefore ends in exactly the state the interpreter reaches.
6. **One driver per run.** With `[temporal].address` set:
   - `msa worker` drives runs;
   - `--temporal` on `msa run`, `msa simulate` and `msa resume` starts or hands a run over;
   - the API starts its runs there.

   An answer to a run a workflow drives is validated by the core as before. It is then
   sent as a signal, never journaled from outside: a parked workflow waits on the server,
   and would not see a journal write. An in-process `msa resume` of such a run is refused.
7. **Executor errors are events, worker loss is a retry.** An executor that raises yields
   a failure event inside the activity, exactly as in the interpreter, so Temporal never
   retries a bug. A worker that dies stops heartbeating. Its effect is then retried on
   another worker, at most three attempts, which is the interpreter's at-least-once
   execution after a crash.

## Verification

What the tests check, and where they run. Results are CI's: this record names the tests,
and the PR links each run.

The PostgreSQL backend runs the same tests as SQLite, in CI, against a `postgres:17`
container:

- the journal contract tests;
- the crash-recovery suite, with real SIGKILLs at random boundaries;
- the API tests;
- a race between two connections, in which exactly one append wins.

The Temporal driver is tested against Temporal's own test server, in CI
(`tests/integration/test_temporal.py`):

- **Equivalence:** a run driven by Temporal ends in exactly the interpreter's final
  state, on three seeds and both journals, and each effect runs once.
- **Determinism:** the recorded history replays without a nondeterminism error.
- **Parking:** a parked run waits on the server and carries on when signalled; an answer
  for an effect the run is not waiting on is dropped; a cancel ends the run.
- **Worker loss:** a worker process is SIGKILLed mid-effect, and a second one finishes
  the run. Nothing journaled runs again, the effect in flight runs once more, and the
  final state equals the uninterrupted run's. This test runs on Temporal's real dev
  server: the time-skipping test server never timed out the dead worker's heartbeats,
  so the run waited forever (found in CI).
- **Through the API and the CLI:** a run is started, approved and completed.

The activities are also tested in-process, with no server
(`tests/unit/runtime/test_temporal_activities.py`), so they run everywhere.

Neither server can run in the development container this was built in: running
downloaded server binaries there was refused. Both suites skip there and say why, and CI
fails rather than skips when a server is missing (`MSA_REQUIRE_POSTGRES`,
`MSA_TEST_TEMPORAL`).

## Consequences

- psycopg is a dependency. temporalio is an optional extra
  (`multistep-agent[temporal]`), imported only when Temporal is configured.
- A Temporal deployment needs a Temporal server, at least one `msa worker`, and a
  journal every worker can reach. With several workers on several machines, that means
  PostgreSQL.
- Runs that were driven in-process can be handed to Temporal (`msa resume --temporal`)
  from wherever their journal stands. A workflow's own history is not migrated anywhere,
  since the journal is the record.

## Rejected

- **Temporal history as the only record.** It would make the journal a projection that
  could lag. It would also tie `msa inspect`, the API and replay to a running Temporal
  server.
- **Arrival-order completions.** They are marginally faster for stages with several
  effects in flight. But they make a workflow's result depend on timing, and lose exact
  equivalence with the interpreter, which is the strongest test of the driver.
- **Journaling answers from outside a workflow.** A parked workflow would never notice,
  and a second writer would race the workflow's commits.

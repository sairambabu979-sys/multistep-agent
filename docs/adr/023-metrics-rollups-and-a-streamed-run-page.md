# ADR-023: Metrics from rollups, and a run page driven by its stream

- **Status:** Accepted (2026-11-27)
- **Refines:** ADR-007 (the journal), ADR-016 (the service and the workbench)

## Context

`/api/metrics` loaded every run's full state from the journal on every request, to sum a
few numbers. Each load folds a snapshot and the events after it. So the endpoint's cost
grew with the whole history of every run, and the Metrics page asked for it again on every
visit.

The run page polled on four timers while the run was live:
- its state every 2 s;
- its timeline every 2 s;
- its records every 4 s;
- its files every 5 s.

The Timeline tab streamed the same events over server-sent events, but only while that tab
was open, and nothing else used the stream.

## Decision

1. **Rollups on the `runs` table** (journal schema v2).
   - **Columns:** spent µUSD, paused seconds, whether the run waits on a person, revisions,
     and working seconds (started to now, less paused).
   - **Written with every append, in the same transaction** as the compare-and-set
     `UPDATE` that moves `head_seq`. They can never be behind the journal, on SQLite or
     PostgreSQL.
   - **Migration:** a v1 journal gains the columns on open (`ALTER TABLE ... ADD COLUMN`),
     and they are filled once from each run's state: in SQLite's `BEGIN IMMEDIATE`, and in
     PostgreSQL under the migration's advisory lock. Older msa versions already refuse a
     newer schema, so no old writer can leave a rollup stale. SQLite's `synchronous=FULL`
     (ADR-007) is unchanged.
   - **`/api/metrics` reads them with one query** (`SqlJournal.rollups()`) and loads no
     state. Its response does not change.
2. **The run page is driven by its stream.**
   - **One stream for the page** (`useRunStream`), not one per Timeline tab. A lost stream
     is opened again after 10 s, from the last event seen, so nothing is missed or
     repeated.
   - **Each event refetches only what it can change** (`refreshesFor`):
     - every event: the state and the timeline;
     - a gate, the review, a revision or a ruling: the records;
     - the release events: the patch and the PR text.

     A burst of events within 250 ms becomes one refetch.
   - **Polling only while the stream is down:** every 15 s until it reconnects. Once the
     run ends, the page reads everything once more and stops.

## Consequences

- **The metrics query costs the same at 10 runs as at 10,000 events per run.** Each append
  writes five more columns of a row it already updates.
- **A live run's page sends requests when something happens**, not 15 times a minute. An
  idle run parked at a human gate sends none.
- **Tests:**
  - the rollups equal the folded state after a run, on both backends;
  - a v1 journal, made by dropping the columns and setting the version back, gains correct
    rollups when opened, on both backends, and loads to the same state;
  - `/api/metrics` loads no run (a counting stub of `SqlJournal.load` stays at zero) and
    returns what it did;
  - on the web, the event-to-refresh map is pinned, and the timeline renders the stream's
    events. The Playwright suite follows a real run to its end.

## Rejected

- **Caching `/api/metrics` in the service.** A cache must be invalidated on every append,
  by every process that can append (the CLI, the service, a Temporal worker). The table is
  already the one place they all write.
- **Deriving the page's state from the events in the browser.** The core's fold would have
  to be duplicated in TypeScript and kept in step.

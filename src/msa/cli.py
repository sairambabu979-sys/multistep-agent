"""Command-line entry point: ``msa``."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from msa import __version__
from msa.core import RunState, Step, TransitionRejected, transition
from msa.core.events import AnyEvent, CancelRequested
from msa.core.graph import analyze, render_markdown
from msa.core.ids import RunId
from msa.core.transitions import TABLE
from msa.core.values import Escalation, Limits, Phase, PlanGate
from msa.runtime.clock import LogicalClock, SystemClock
from msa.runtime.interpreter import Interpreter, Observer
from msa.runtime.journal import JournalError, SqliteJournal
from msa.runtime.simulation import (
    Profile,
    SimulatedEnvironment,
    SimulationConfig,
    digest,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Multistep coding agent.")
fsm_app = typer.Typer(no_args_is_help=True, help="Inspect the orchestrator state machine.")
app.add_typer(fsm_app, name="fsm")

DEFAULT_DB = Path(".msa/runs.db")
Db = Annotated[Path, typer.Option("--db", envvar="MSA_DB", help="Journal database.")]
Run = Annotated[str, typer.Argument(help="Run id (see `msa runs`).")]
Quiet = Annotated[bool, typer.Option("--quiet", "-q", help="No per-event timeline.")]


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:,.4f}"


def _outcome_line(state: RunState) -> str:
    if state.outcome is None:
        return f"{state.run_id}: {state.phase.name} (live, seq {state.seq})"
    detail = f" ({state.outcome.detail})" if state.outcome.detail else ""
    return f"{state.run_id}: {state.phase.name}: {state.outcome.reason}{detail}"


class Timeline(Observer):
    """Prints one line per committed transition."""

    def __init__(self) -> None:
        typer.echo(f"{'seq':>5}  {'transition':<34}{'event':<28}issues")

    def after_commit(self, event: AnyEvent, step: Step) -> None:
        edge = step.edge
        source = edge.source.name if edge.source else "START"
        arrow = f"{source} -> {edge.target.name}"
        issued = ", ".join(e.command.kind for e in step.effects) or "-"
        typer.echo(f"{step.state.seq:>5}  {arrow:<34}{edge.event:<28}{issued}")


# ---------- version and state machine


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@fsm_app.command()
def diagram(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write to this file instead of stdout.")
    ] = None,
) -> None:
    """Render the state machine as Markdown with a Mermaid diagram."""
    text = render_markdown(TABLE)
    if output is None:
        typer.echo(text, nl=False)
        return
    output.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {output}")


@fsm_app.command()
def check() -> None:
    """Statically verify the transition table: reachability, liveness, terminal shape."""
    problems = analyze(TABLE)
    for problem in problems:
        typer.echo(f"error: {problem}", err=True)
    if problems:
        raise typer.Exit(code=1)
    typer.echo(f"ok: {len(Phase)} phases, {len(TABLE)} rules, every live phase can reach DONE")


# ---------- runs


def _drive(rid: RunId, coroutine: Coroutine[Any, Any, RunState]) -> RunState:
    """Run the interpreter. Ctrl-C is a crash as far as the journal is concerned (the
    commit point makes that safe), so the run stays live and resumable."""
    try:
        return asyncio.run(coroutine)
    except KeyboardInterrupt:
        typer.echo(f"\ninterrupted: {rid} is journaled; continue with `msa resume {rid}`", err=True)
        raise typer.Exit(code=130) from None


def _interpreter(
    journal: SqliteJournal, environment: dict[str, Any], *, quiet: bool
) -> Interpreter:
    try:
        config = SimulationConfig.model_validate(environment)
    except ValidationError as exc:
        kind = environment.get("kind", "?")
        raise _fail(f"no executors for environment kind {kind!r} in this milestone") from exc
    return Interpreter(
        journal,
        SimulatedEnvironment(config),
        clock=LogicalClock(),
        observer=Observer() if quiet else Timeline(),
    )


@app.command()
def simulate(
    seed: Annotated[int, typer.Option(help="Seed for the simulated environment.")] = 0,
    profile: Annotated[Profile, typer.Option(help="happy: always succeed; mixed: anything.")] = (
        Profile.MIXED
    ),
    run_id: Annotated[str | None, typer.Option(help="Defaults to sim-<seed>-<random>.")] = None,
    plan_gate: Annotated[PlanGate, typer.Option()] = PlanGate.AUTO,
    escalation: Annotated[Escalation, typer.Option()] = Escalation.HUMAN,
    budget_usd: Annotated[float, typer.Option(min=0.000001)] = 5.0,
    quiet: Quiet = False,
    db: Db = DEFAULT_DB,
) -> None:
    """Drive a full run against simulated (stub) executors and journal every event."""
    config = SimulationConfig(seed=seed, profile=profile)
    rid = RunId(run_id or f"sim-{seed}-{uuid.uuid4().hex[:6]}")
    limits = Limits(
        budget_usd_micros=round(budget_usd * 1_000_000),
        # Logical time advances one tick per event, so this is effectively an event budget.
        wall_clock=LogicalClock().tick * 10_000,
        plan_gate=plan_gate,
        escalation=escalation,
    )
    environment = config.model_dump(mode="json")
    with SqliteJournal(db) as journal:
        interpreter = _interpreter(journal, environment, quiet=quiet)
        try:
            state = _drive(
                rid,
                interpreter.start(
                    run_id=rid, task_digest=digest(rid), limits=limits, environment=environment
                ),
            )
        except (JournalError, ValidationError) as exc:
            raise _fail(str(exc)) from exc
    typer.echo(_outcome_line(state))


@app.command()
def resume(run_id: Run, db: Db = DEFAULT_DB, quiet: Quiet = False) -> None:
    """Continue a run from its journal. Completed effects are never re-executed."""
    rid = RunId(run_id)
    with SqliteJournal(db) as journal:
        try:
            environment = journal.environment(rid)
            state = _drive(rid, _interpreter(journal, environment, quiet=quiet).resume(rid))
        except JournalError as exc:
            raise _fail(str(exc)) from exc
    typer.echo(_outcome_line(state))


@app.command()
def cancel(
    run_id: Run,
    reason: Annotated[str, typer.Option()] = "cancelled by operator",
    db: Db = DEFAULT_DB,
) -> None:
    """Journal a cancellation. A process still driving the run stops at its next commit."""
    rid = RunId(run_id)
    with SqliteJournal(db) as journal:
        try:
            state = journal.load(rid)
            event = CancelRequested(at=SystemClock().stamp(state), reason=reason)
            step = transition(state, event)
            journal.append(event, step.state, snapshot=True)
        except TransitionRejected as exc:
            raise _fail(f"cannot cancel: {exc.detail}") from exc
        except JournalError as exc:  # includes JournalConflict: another writer moved first
            raise _fail(str(exc)) from exc
    typer.echo(_outcome_line(step.state))


@app.command(name="runs")
def list_runs(db: Db = DEFAULT_DB) -> None:
    """List journaled runs, newest first."""
    with SqliteJournal(db) as journal:
        summaries = journal.runs()
    if not summaries:
        typer.echo("no runs")
        return
    typer.echo(f"{'run':<24}{'phase':<16}{'seq':>5}  {'outcome':<22}environment")
    for run in summaries:
        outcome = run.outcome["reason"] if run.outcome else "-"
        env = ", ".join(f"{k}={v}" for k, v in sorted(run.environment.items()))
        typer.echo(f"{run.run_id:<24}{run.phase.upper():<16}{run.head_seq:>5}  {outcome:<22}{env}")


@app.command()
def inspect(
    run_id: Run,
    at: Annotated[int | None, typer.Option(min=1, help="Show the state after this event.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full state as JSON.")] = False,
    db: Db = DEFAULT_DB,
) -> None:
    """Show a run's state, now or as of any past event (time travel)."""
    with SqliteJournal(db) as journal:
        try:
            state = journal.load(RunId(run_id), upto=at)
        except JournalError as exc:
            raise _fail(str(exc)) from exc
    if as_json:
        typer.echo(state.model_dump_json(indent=2))
        return
    lim, ledger = state.limits, state.ledger
    rows = [
        ("run", _outcome_line(state)),
        ("seq", str(state.seq)),
        ("base", state.base_tree or "-"),
        ("head", state.head_tree or "-"),
        ("plan", state.plan.id if state.plan else "-"),
        (
            "budget",
            f"spent {_usd(ledger.spent.usd_micros)}, reserved {_usd(ledger.reserved_usd_micros)}"
            f" of {_usd(ledger.limit_usd_micros)}"
            f" ({ledger.spent.input_tokens:,} in / {ledger.spent.output_tokens:,} out tokens)",
        ),
        (
            "counters",
            f"replans {state.replans}/{lim.max_replans}, revisions {state.revisions}/"
            f"{lim.max_revisions}, round {state.negotiation_round}/{lim.max_negotiation_rounds}",
        ),
        (
            "findings",
            ", ".join(f"{f.id}[{f.severity}:{f.status}]" for f in state.findings) or "-",
        ),
        ("pending", ", ".join(f"{e.id} {e.command.kind}" for e in state.pending) or "-"),
    ]
    for label, value in rows:
        typer.echo(f"{label:<10}{value}")


@app.command()
def replay(run_id: Run, db: Db = DEFAULT_DB) -> None:
    """Re-fold the whole journal from scratch and check every snapshot against it."""
    with SqliteJournal(db) as journal:
        try:
            result = journal.verify(RunId(run_id))
        except JournalError as exc:
            raise _fail(str(exc)) from exc
    for divergence in result.divergences:
        typer.echo(f"divergence: {divergence}", err=True)
    if not result.ok:
        raise typer.Exit(code=1)
    typer.echo(
        f"ok: {result.events} events replay deterministically; "
        f"{result.snapshots_checked} snapshot(s) match"
    )


def main() -> None:
    app()

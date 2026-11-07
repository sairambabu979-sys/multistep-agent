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
from msa.config import (
    DEFAULT_CONFIG,
    ConfigError,
    JournalConfig,
    LimitsConfig,
    MsaConfig,
    Role,
    SandboxConfig,
    json_schema,
    load_config,
)
from msa.core import RunState, Step, TransitionRejected, transition
from msa.core.events import AnyEvent, CancelRequested
from msa.core.graph import analyze, render_markdown
from msa.core.ids import RunId
from msa.core.transitions import TABLE
from msa.core.values import Escalation, Limits, Phase, PlanGate
from msa.llm.anthropic_client import AnthropicMessages
from msa.llm.cassette import CassetteClient, CassetteMiss
from msa.llm.client import MessagesClient
from msa.llm.gateway import InvocationError
from msa.llm.ping import TOKEN, PingReport, ping
from msa.llm.store import SqliteResponseStore
from msa.runtime import live
from msa.runtime.checks import CheckError, CheckFinding, run_checks
from msa.runtime.clock import LogicalClock, SystemClock
from msa.runtime.interpreter import Interpreter, Observer
from msa.runtime.journal import JournalError, SqliteJournal
from msa.runtime.live import LiveEnvironment
from msa.runtime.simulation import (
    Profile,
    SimulatedEnvironment,
    SimulationConfig,
    digest,
)
from msa.skills.preferences import Preference, PreferenceError, Resolved, Scope, store
from msa.skills.preferences.lint import lint
from msa.skills.preferences.resolve import Layer, resolve
from msa.skills.preferences.schema import FIELDS, Examples, RegexCheck, RuffCheck
from msa.tools.errors import ToolError
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Multistep coding agent.")
fsm_app = typer.Typer(no_args_is_help=True, help="Inspect the orchestrator state machine.")
config_app = typer.Typer(no_args_is_help=True, help="Validate msa.toml and export its schema.")
llm_app = typer.Typer(no_args_is_help=True, help="Check and record the model API connection.")
prefs_app = typer.Typer(no_args_is_help=True, help="Inspect, check and change coding preferences.")
app.add_typer(fsm_app, name="fsm")
app.add_typer(config_app, name="config")
app.add_typer(llm_app, name="llm")
app.add_typer(prefs_app, name="prefs")

Db = Annotated[
    Path | None,
    typer.Option("--db", envvar="MSA_DB", help="Journal database [default: from msa.toml]."),
]
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


# ---------- configuration


@app.callback()
def options(
    ctx: typer.Context,
    config: Annotated[
        Path, typer.Option("--config", envvar="MSA_CONFIG", help="Settings file.")
    ] = DEFAULT_CONFIG,
) -> None:
    """Multistep coding agent."""
    ctx.obj = config


def _settings(ctx: typer.Context) -> MsaConfig | None:
    """The loaded settings, or None when the default msa.toml does not exist (built-in
    defaults then apply). A missing file that was named explicitly is an error."""
    path: Path = ctx.obj or DEFAULT_CONFIG
    if not path.exists() and path == DEFAULT_CONFIG:
        return None
    try:
        return load_config(path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


def _journal_settings(ctx: typer.Context) -> JournalConfig:
    settings = _settings(ctx)
    return settings.journal if settings else JournalConfig()


def _journal(ctx: typer.Context, db: Path | None) -> SqliteJournal:
    return SqliteJournal(db or _journal_settings(ctx).path)


@config_app.command(name="check")
def config_check(ctx: typer.Context) -> None:
    """Validate the settings file and summarize what it configures."""
    path: Path = ctx.obj or DEFAULT_CONFIG
    try:
        settings = load_config(path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"ok: {path}")
    for role in Role:
        price = settings.pricing_for(role)
        model, effort = settings.models.model_for(role), settings.models.effort_for(role)
        typer.echo(
            f"  {role:<9}{model:<20}effort {effort:<7}"
            f"${price.input:.2f} in / ${price.output:.2f} out per MTok"
        )
    lim, journal = settings.limits, settings.journal
    typer.echo(
        f"  limits   ${lim.budget_usd:.2f} and {lim.wall_clock_minutes} min per run, "
        f"{lim.max_replans} replans, {lim.max_revisions} revisions, "
        f"{lim.max_negotiation_rounds} rounds, plan gate {lim.plan_gate}, "
        f"escalation {lim.escalation}"
    )
    typer.echo(f"  journal  {journal.path} (snapshot every {journal.snapshot_every} events)")


@config_app.command(name="schema")
def config_schema(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write to this file instead of stdout.")
    ] = None,
) -> None:
    """Print the JSON Schema of msa.toml (committed as schemas/msa.schema.json)."""
    text = json_schema()
    if output is None:
        typer.echo(text, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {output}")


# ---------- model API


def _live_client(settings: MsaConfig) -> AnthropicMessages:
    return AnthropicMessages.from_config(settings)


async def _ping(settings: MsaConfig, record: Path | None, replay: Path | None) -> PingReport:
    if replay is not None:
        return await ping(CassetteClient(replay, mode="replay"), settings)
    live = _live_client(settings)
    client: MessagesClient = (
        live if record is None else CassetteClient(record, mode="record", inner=live)
    )
    try:
        return await ping(client, settings)
    finally:
        await live.aclose()


@llm_app.command(name="ping")
def llm_ping(
    ctx: typer.Context,
    record: Annotated[
        Path | None,
        typer.Option("--record", help="Also write the exchange to this cassette.", dir_okay=False),
    ] = None,
    replay: Annotated[
        Path | None,
        typer.Option(
            "--replay", help="Answer from this cassette, offline.", exists=True, dir_okay=False
        ),
    ] = None,
) -> None:
    """A two-turn tool round trip against the API: proves the key, model id, betas,
    strict schemas and the append-only transcript, for well under a cent."""
    if record is not None and replay is not None:
        raise _fail("--record and --replay cannot be combined")
    settings = _settings(ctx)
    if settings is None:
        raise _fail("msa llm ping needs msa.toml: model ids and prices live there")
    try:
        report = asyncio.run(_ping(settings, record, replay))
    except InvocationError as exc:
        raise _fail(f"{exc} (spent {_usd(exc.spend.usd_micros)})") from exc
    except (CassetteMiss, ValueError) as exc:
        raise _fail(str(exc)) from exc
    result = report.result
    source = f"replayed from {replay}" if replay else "live"
    typer.echo(
        f"{'ok' if report.ok else 'FAILED'}: {', '.join(report.models)} answered "
        f"{result.output.token!r} in {len(result.turns)} turns ({source})"
    )
    for turn in result.turns:
        typer.echo(
            f"  turn {turn.index}  {turn.stop_reason:<9} max_tokens {turn.max_tokens:<6} "
            f"{_usd(turn.spend.usd_micros)}  {turn.spend.input_tokens} in / "
            f"{turn.spend.output_tokens} out, {turn.cache_read_tokens} cached"
            + (f", {turn.retries} retries" if turn.retries else "")
        )
    typer.echo(f"  spent {_usd(result.spend.usd_micros)}")
    if record is not None:
        typer.echo(f"  recorded {record}")
    if not report.ok:
        problem = (
            "the model answered without calling the tool"
            if not report.used_the_tool
            else f"expected token {TOKEN!r}"
        )
        raise _fail(problem)


# ---------- preferences (PLAN §5)

Repo = Annotated[
    Path, typer.Option("--repo", help="Repository root.", file_okay=False, exists=True)
]
Pref = Annotated[
    list[str] | None,
    typer.Option("--pref", help="Run override, <id>=hard|soft|info|off (repeatable)."),
]


def _layers(repo: Path, pref: list[str] | None) -> list[Layer]:
    try:
        return store.load_layers(repo.resolve(), pref or ())
    except PreferenceError as exc:
        raise _fail(str(exc)) from exc


def _resolved(repo: Path, pref: list[str] | None) -> Resolved:
    try:
        return resolve(_layers(repo, pref))
    except PreferenceError as exc:
        raise _fail(str(exc)) from exc


def _check_label(p: Preference) -> str:
    if isinstance(p.check, RuffCheck):
        return f"ruff {p.check.rule}"
    if isinstance(p.check, RegexCheck):
        return "regex"
    return "-"


@prefs_app.command(name="list")
def prefs_list(repo: Repo = Path("."), pref: Pref = None) -> None:
    """Every preference after merging the layers, with where each came from."""
    resolved = _resolved(repo, pref)
    width = max((len(p.id) for p in resolved.preferences), default=10)
    for p in resolved.preferences:
        typer.echo(
            f"{p.id:<{width}}  {p.enforcement:<5} {_check_label(p):<10} "
            f"{p.scope.describe():<28} {resolved.origin(p.id)}"
        )


@prefs_app.command(name="show")
def prefs_show(
    preference_id: Annotated[str, typer.Argument(metavar="ID")],
    repo: Repo = Path("."),
    pref: Pref = None,
) -> None:
    """One preference in full, and the layer that set each of its fields."""
    resolved = _resolved(repo, pref)
    found = resolved.get(preference_id)
    if found is None:
        raise _fail(f"no preference {preference_id!r}; see msa prefs list")
    data = found.model_dump(mode="json", exclude_defaults=True)
    origins = resolved.provenance[preference_id]
    typer.echo(preference_id)
    for name in FIELDS:
        if name in data:
            typer.echo(f"  {name:<15}{data[name]!s:<60} [{origins.get(name, 'default')}]")


@prefs_app.command(name="explain")
def prefs_explain(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(metavar="PATH[:LINE]")],
    repo: Repo = Path("."),
    pref: Pref = None,
) -> None:
    """The preferences that apply to a file and, with a line, what its checks find there."""
    path, _, line_text = target.partition(":")
    line = int(line_text) if line_text.isdigit() else None
    resolved = _resolved(repo, pref)
    applicable = resolved.select([path])
    if not applicable:
        typer.echo(f"{path}: no preference applies")
        return
    typer.echo(f"{path}: {len(applicable)} preference(s) apply")
    for p in applicable:
        typer.echo(f"  {p.id} ({p.enforcement}, {p.scope.describe()}) from {resolved.origin(p.id)}")
    if line is not None:
        findings = [f for f in _run_checks(ctx, repo, resolved, [path]) if f.line == line]
        typer.echo(f"line {line}: " + ("; ".join(f.render() for f in findings) or "no findings"))


@prefs_app.command(name="add")
def prefs_add(
    preference_id: Annotated[str, typer.Argument(metavar="ID")],
    rule: Annotated[str, typer.Option(help="The rule, in one or two sentences.")],
    enforcement: Annotated[str, typer.Option(help="hard, soft or info.")] = "soft",
    language: Annotated[str | None, typer.Option(help="Limit to one language.")] = None,
    path: Annotated[
        list[str] | None, typer.Option("--path", help="Path glob (repeatable).")
    ] = None,
    rationale: Annotated[str | None, typer.Option(help="Why the rule exists.")] = None,
    ruff: Annotated[str | None, typer.Option(help="Enforce with this ruff rule code.")] = None,
    regex: Annotated[str | None, typer.Option(help="Enforce: this pattern must not occur.")] = None,
    good: Annotated[str | None, typer.Option(help="An example that follows the rule.")] = None,
    bad: Annotated[str | None, typer.Option(help="An example that breaks it.")] = None,
    user: Annotated[bool, typer.Option("--user", help="Add to your user layer.")] = False,
    repo: Repo = Path("."),
) -> None:
    """Add a preference to the repo layer (or --user), keeping the file's comments.

    Refused if it would leave the preferences unresolvable (a hard conflict, say)."""
    if ruff is not None and regex is not None:
        raise _fail("--ruff and --regex cannot be combined")
    check: dict[str, str] | None = (
        {"kind": "ruff", "rule": ruff}
        if ruff
        else {"kind": "regex", "pattern": regex}
        if regex
        else None
    )
    try:
        preference = Preference.model_validate(
            {
                "id": preference_id,
                "rule": rule,
                "enforcement": enforcement,
                "scope": Scope.model_validate({"language": language, "paths": path or []}),
                "rationale": rationale,
                "check": check,
                "examples": Examples(good=good, bad=bad) if good or bad else None,
            }
        )
    except ValidationError as exc:
        raise _fail(f"{preference_id}: " + "; ".join(e["msg"] for e in exc.errors())) from exc
    target = store.user_path() if user else repo.resolve() / store.REPO_FILE
    try:
        store.add(target, preference, others=_layers(repo, None), name="user" if user else "repo")
    except PreferenceError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"added {preference_id} to {target}")


@prefs_app.command(name="lint")
def prefs_lint(repo: Repo = Path("."), pref: Pref = None) -> None:
    """Problems resolution lets through: unchecked hard rules, checks that miss their examples."""
    findings = lint(_resolved(repo, pref))
    for finding in findings:
        typer.echo(finding.render())
    errors = sum(f.level == "error" for f in findings)
    typer.echo(f"{errors} error(s), {len(findings) - errors} warning(s)")
    if errors:
        raise typer.Exit(code=1)


@prefs_app.command(name="review")
def prefs_review(
    approve: Annotated[
        str | None, typer.Option(help="Apply this proposal to the repo layer.")
    ] = None,
    reject: Annotated[str | None, typer.Option(help="Discard this proposal.")] = None,
    repo: Repo = Path("."),
) -> None:
    """Learned preferences wait in .msa/proposals/ until a human approves them (PLAN §5.5)."""
    root = repo.resolve()
    try:
        pending = {p.preference.id: p for p in store.proposals(root)}
    except PreferenceError as exc:
        raise _fail(str(exc)) from exc
    chosen = approve or reject
    if chosen is None:
        if not pending:
            typer.echo("no proposals")
        for p in pending.values():
            typer.echo(f"{p.preference.id} ({p.preference.enforcement}): {p.preference.rule}")
            typer.echo(f"  why proposed: {p.reason}")
        return
    if approve and reject:
        raise _fail("--approve and --reject cannot be combined")
    if chosen not in pending:
        raise _fail(f"no proposal {chosen!r}; run msa prefs review to list them")
    if reject:
        store.reject(pending[chosen])
        typer.echo(f"rejected {chosen}")
        return
    try:
        store.approve(root, pending[chosen], others=_layers(repo, None))
    except PreferenceError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"approved {chosen} into {root / store.REPO_FILE}")


@prefs_app.command(name="diff-layers")
def prefs_diff_layers(repo: Repo = Path("."), pref: Pref = None) -> None:
    """Which layer sets which field of each preference; later layers win."""
    layers = _layers(repo, pref)
    resolved = _resolved(repo, pref)
    for p in resolved.preferences:
        typer.echo(p.id)
        for layer in layers:
            fields = layer.patches.get(p.id)
            if fields:
                shown = ", ".join(f"{k}={v}" for k, v in sorted(fields.items()) if k != "id")
                typer.echo(f"  {layer.name:<9}{shown}")


@prefs_app.command(name="check")
def prefs_check(
    ctx: typer.Context,
    paths: Annotated[list[str] | None, typer.Argument(help="Files to check; default: all.")] = None,
    repo: Repo = Path("."),
    pref: Pref = None,
) -> None:
    """Run the machine checks (ruff rules, patterns) of the preferences that apply."""
    resolved = _resolved(repo, pref)
    files = paths or _all_files(ctx, repo)
    findings = _run_checks(ctx, repo, resolved, files)
    for finding in findings:
        typer.echo(finding.render())
    hard = sum(f.enforcement == "hard" for f in findings)
    typer.echo(f"{len(findings)} finding(s), {hard} hard, in {len(files)} file(s)")
    if hard:
        raise typer.Exit(code=1)


def _sandbox(ctx: typer.Context, repo: Path) -> LocalSandbox:
    settings = _settings(ctx)
    config = settings.sandbox if settings else SandboxConfig()
    return LocalSandbox(Root.at(repo), config, role="cli")


def _run_checks(
    ctx: typer.Context, repo: Path, resolved: Resolved, files: list[str]
) -> list[CheckFinding]:
    try:
        return asyncio.run(run_checks(_sandbox(ctx, repo), resolved, files))
    except (CheckError, ToolError) as exc:
        raise _fail(str(exc)) from exc


def _all_files(ctx: typer.Context, repo: Path) -> list[str]:
    try:
        done = asyncio.run(
            _sandbox(ctx, repo).execute(["rg", "--files", "--sort=path"], shown="rg", merge=False)
        )
    except ToolError as exc:
        raise _fail(str(exc)) from exc
    return done.stdout.splitlines()


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
    ctx: typer.Context, journal: SqliteJournal, environment: dict[str, Any], *, quiet: bool
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
        snapshot_every=_journal_settings(ctx).snapshot_every,
    )


def _client(settings: MsaConfig, environment: LiveEnvironment) -> MessagesClient:
    if environment.cassette_mode == "replay" and environment.cassette is not None:
        return CassetteClient(Path(environment.cassette), mode="replay")
    live = _live_client(settings)
    if environment.cassette_mode == "record" and environment.cassette is not None:
        return CassetteClient(Path(environment.cassette), mode="record", inner=live)
    return live


async def _drive_live(
    settings: MsaConfig,
    journal: SqliteJournal,
    environment: LiveEnvironment,
    rid: RunId,
    *,
    quiet: bool,
    limits: Limits | None,
) -> RunState:
    """Start the run with ``limits``, or resume it (``limits`` is None)."""
    client = _client(settings, environment)
    state_dir = Path(environment.state_dir)
    with SqliteResponseStore(state_dir / "responses.db") as responses:
        interpreter = live.interpreter(
            environment,
            settings,
            run_id=rid,
            journal=journal,
            client=client,
            store=responses,
            clock=SystemClock(),
            observer=Observer() if quiet else Timeline(),
            on_report=lambda path: typer.echo(f"report: {path}"),
        )
        try:
            if limits is None:
                return await interpreter.resume(rid)
            return await interpreter.start(
                run_id=rid,
                task_digest=environment.task_digest,
                limits=limits,
                environment=environment.model_dump(mode="json"),
            )
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()


@app.command(name="run")
def run_task(
    ctx: typer.Context,
    task: Annotated[str | None, typer.Option(help="What to do, in plain words.")] = None,
    task_file: Annotated[
        Path | None,
        typer.Option(help="Read the task from this file.", exists=True, dir_okay=False),
    ] = None,
    repo: Repo = Path("."),
    pref: Pref = None,
    run_id: Annotated[str | None, typer.Option(help="Defaults to run-<random>.")] = None,
    budget_usd: Annotated[
        float | None, typer.Option(min=0.000001, help="[default: msa.toml]")
    ] = None,
    record: Annotated[
        Path | None,
        typer.Option(
            "--record", help="Also write every exchange to this cassette.", dir_okay=False
        ),
    ] = None,
    replay: Annotated[
        Path | None,
        typer.Option(
            "--replay", help="Answer from this cassette, offline.", exists=True, dir_okay=False
        ),
    ] = None,
    quiet: Quiet = False,
    db: Db = None,
) -> None:
    """Plan, implement and verify a change to a git repository (M6: Planner, Coder, VERIFYING).

    The Tester and the Reviewer arrive in M7 and M8; until then their stages pass through,
    and the run report says so. The repository's checkout is never modified: the work
    happens in a worktree under the state directory, and the result is a patch."""
    if (task is None) == (task_file is None):
        raise _fail("give exactly one of --task and --task-file")
    if record is not None and replay is not None:
        raise _fail("--record and --replay cannot be combined")
    settings = _settings(ctx)
    if settings is None:
        raise _fail("msa run needs msa.toml: model ids and prices live there")
    if task_file is not None:
        task = task_file.read_text(encoding="utf-8")
    text = task or ""
    if not text.strip():
        raise _fail("the task is empty")
    overrides: dict[str, Any] = {}
    if budget_usd is not None:
        overrides["budget_usd_micros"] = round(budget_usd * 1_000_000)
    limits = settings.limits.to_limits(**overrides)
    if limits.plan_gate is PlanGate.HUMAN:
        raise _fail("plan_gate = human needs an approval command, which is not built yet")
    journal_path = (db or settings.journal.path).resolve()
    cassette = record or replay
    environment = LiveEnvironment(
        repo=str(repo.resolve()),
        state_dir=str(journal_path.parent),
        task=text,
        overrides=tuple(pref or ()),
        cassette=str(cassette.resolve()) if cassette else None,
        cassette_mode="record" if record else "replay" if replay else None,
    )
    rid = RunId(run_id or f"run-{uuid.uuid4().hex[:8]}")
    with SqliteJournal(journal_path) as journal:
        try:
            state = _drive(
                rid,
                _drive_live(settings, journal, environment, rid, quiet=quiet, limits=limits),
            )
        except (JournalError, ValidationError, CassetteMiss) as exc:
            raise _fail(str(exc)) from exc
    typer.echo(_outcome_line(state))


@app.command()
def simulate(
    ctx: typer.Context,
    seed: Annotated[int, typer.Option(help="Seed for the simulated environment.")] = 0,
    profile: Annotated[Profile, typer.Option(help="happy: always succeed; mixed: anything.")] = (
        Profile.MIXED
    ),
    run_id: Annotated[str | None, typer.Option(help="Defaults to sim-<seed>-<random>.")] = None,
    plan_gate: Annotated[PlanGate | None, typer.Option(help="[default: msa.toml]")] = None,
    escalation: Annotated[Escalation | None, typer.Option(help="[default: msa.toml]")] = None,
    budget_usd: Annotated[
        float | None, typer.Option(min=0.000001, help="[default: msa.toml]")
    ] = None,
    quiet: Quiet = False,
    db: Db = None,
) -> None:
    """Drive a full run against simulated (stub) executors and journal every event."""
    config = SimulationConfig(seed=seed, profile=profile)
    rid = RunId(run_id or f"sim-{seed}-{uuid.uuid4().hex[:6]}")
    settings = _settings(ctx)
    overrides: dict[str, Any] = {
        key: value
        for key, value in (("plan_gate", plan_gate), ("escalation", escalation))
        if value is not None
    }
    if budget_usd is not None:
        overrides["budget_usd_micros"] = round(budget_usd * 1_000_000)
    limits = (settings.limits if settings else LimitsConfig()).to_limits(**overrides)
    environment = config.model_dump(mode="json")
    with _journal(ctx, db) as journal:
        interpreter = _interpreter(ctx, journal, environment, quiet=quiet)
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
def resume(ctx: typer.Context, run_id: Run, db: Db = None, quiet: Quiet = False) -> None:
    """Continue a run from its journal. Completed effects are never re-executed."""
    rid = RunId(run_id)
    with _journal(ctx, db) as journal:
        try:
            environment = journal.environment(rid)
            if environment.get("kind") == "live":
                state = _resume_live(ctx, journal, environment, rid, quiet=quiet)
            else:
                interpreter = _interpreter(ctx, journal, environment, quiet=quiet)
                state = _drive(rid, interpreter.resume(rid))
        except JournalError as exc:
            raise _fail(str(exc)) from exc
    typer.echo(_outcome_line(state))


def _resume_live(
    ctx: typer.Context,
    journal: SqliteJournal,
    environment: dict[str, Any],
    rid: RunId,
    *,
    quiet: bool,
) -> RunState:
    settings = _settings(ctx)
    if settings is None:
        raise _fail("resuming a live run needs msa.toml: model ids and prices live there")
    try:
        live_environment = LiveEnvironment.model_validate(environment)
    except ValidationError as exc:
        raise _fail(f"run {rid} has an unreadable environment: {exc}") from exc
    if live_environment.cassette_mode == "record":
        raise _fail(
            "a run that was recording a cassette cannot be resumed: the cassette would hold "
            "only the resumed half. Record it again from the start."
        )
    return _drive(
        rid, _drive_live(settings, journal, live_environment, rid, quiet=quiet, limits=None)
    )


@app.command()
def cancel(
    ctx: typer.Context,
    run_id: Run,
    reason: Annotated[str, typer.Option()] = "cancelled by operator",
    db: Db = None,
) -> None:
    """Journal a cancellation. A process still driving the run stops at its next commit."""
    rid = RunId(run_id)
    with _journal(ctx, db) as journal:
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
def list_runs(ctx: typer.Context, db: Db = None) -> None:
    """List journaled runs, newest first."""
    with _journal(ctx, db) as journal:
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
    ctx: typer.Context,
    run_id: Run,
    at: Annotated[int | None, typer.Option(min=1, help="Show the state after this event.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full state as JSON.")] = False,
    db: Db = None,
) -> None:
    """Show a run's state, now or as of any past event (time travel)."""
    with _journal(ctx, db) as journal:
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
def replay(ctx: typer.Context, run_id: Run, db: Db = None) -> None:
    """Re-fold the whole journal from scratch and check every snapshot against it."""
    with _journal(ctx, db) as journal:
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

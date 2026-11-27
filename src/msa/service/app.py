"""The AEGIS HTTP API (ADR-015 §3, §4): runs, their events as a live stream, their files,
the human gates, and the workflow, skills and policy that govern them.

``msa serve`` binds it to 127.0.0.1 by default. Every request but ``/api/health`` needs a
bearer token (``msa tokens add``), and each route needs a role (``msa.service.tokens``).
Every answer to a run goes through the core, exactly as the CLI's does
(``msa.runtime.control``), with the token's person as the one who gave it. Every change
and every refused request is written to the audit log first.

Errors are JSON ``{"detail": ...}``: 401 without a valid token, 403 below the role a route
needs, 404 for an unknown run or file, 409 when the core refuses the action (the run is
not waiting for it, or it is already over), 422 for a malformed request.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from msa import __version__
from msa.config import LimitsConfig, MsaConfig, TemporalConfig
from msa.core import RunState, TransitionRejected
from msa.core.events import AnyEvent
from msa.core.graph import GLOBAL_ESCAPES
from msa.core.ids import RUN_ID_PATTERN, Digest, RunId
from msa.core.transitions import TABLE
from msa.core.values import TERMINAL_PHASES, Phase, PublishMode
from msa.llm.anthropic_client import AnthropicMessages
from msa.llm.store import SqliteResponseStore
from msa.observability import tracing
from msa.observability.report import timeline
from msa.runtime import control, live
from msa.runtime.artifacts import RunFiles, digest_of
from msa.runtime.clock import Clock, LogicalClock, SystemClock
from msa.runtime.intake import secrets_in_task
from msa.runtime.interpreter import Interpreter, Observer
from msa.runtime.journal import JournalConflict, SqlJournal, UnknownRun, open_journal
from msa.runtime.simulation import Profile, SimulatedEnvironment, SimulationConfig
from msa.runtime.telemetry import Traced
from msa.service.audit import AuditLog, Entry
from msa.service.runner import Factory, Runner
from msa.service.temporal import TemporalBridge
from msa.service.tokens import Principal, Role, TokenStore
from msa.skills import policy, registry
from msa.skills.preferences import PreferenceError

FILES = ("report.md", "pr.md", "patch.diff", "question.md", "context.json")
RECORDS = (
    "findings",
    "reviews",
    "negotiation",
    "arbitration",
    "tests",
    "verifications",
    "integrations",
    "security",
    "releases",
    "invocations",
    "summaries",
)
TERMINAL = frozenset(p.value for p in TERMINAL_PHASES)
RunPath = Annotated[str, PathParam(pattern=RUN_ID_PATTERN)]


@dataclass
class Settings:
    repo: Path
    state_dir: Path
    config: MsaConfig | None = None  # needed for live runs
    cors_origins: tuple[str, ...] = ()
    poll_seconds: float = 0.5
    heartbeat_seconds: float = 15.0
    clock: Clock = field(default_factory=SystemClock)
    journal_url: str | None = None  # the journal in PostgreSQL; else runs.db in state_dir
    journal_schema: str = "msa"
    temporal: TemporalConfig | None = None  # with an address: runs are driven on Temporal

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "runs.db"

    def open_journal(self) -> SqlJournal:
        return open_journal(self.journal_path, self.journal_url, schema=self.journal_schema)


@dataclass
class Service:
    settings: Settings
    tokens: TokenStore
    audit: AuditLog
    runner: Runner
    bridge: TemporalBridge | None = None

    @contextmanager
    def journal(self) -> Iterator[SqlJournal]:
        with self.settings.open_journal() as opened:
            yield opened

    def files(self, run_id: str) -> RunFiles:
        return RunFiles.for_run(self.settings.state_dir, run_id)

    def driving(self, run_id: RunId, state: RunState | None = None) -> bool:
        """Whether the run is being worked on: by a driver in this process, or by a workflow
        (while it is not waiting on a person, and not over)."""
        if self.runner.driving(run_id):
            return True
        if self.bridge is None or run_id not in self.bridge.driven:
            return False
        return state is None or (not state.is_terminal and state.waiting_since is None)


# ---------- request bodies


class Simulate(BaseModel):
    seed: int = 0
    profile: Profile = Profile.HAPPY


class StartRun(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)] | None = None
    simulate: Simulate | None = None  # a simulated run: no model, no repository changes
    budget_usd: float | None = Field(default=None, gt=0)


class Reason(BaseModel):
    reason: str = Field(default="", max_length=2_000)


class Feedback(BaseModel):
    feedback: str = Field(default="", max_length=10_000)


class Note(BaseModel):
    note: str = Field(default="", max_length=2_000)


class Approval(BaseModel):
    # For a release: local, push or pr (ADR-018). None: [publish].mode, else local.
    publish: PublishMode | None = None


class Ruling(BaseModel):
    verdicts: dict[str, Literal["upheld", "dismissed", "deferred"]] = Field(min_length=1)


# ---------- who is asking


def service(request: Request) -> Service:
    found: Service = request.app.state.service
    return found


def _principal(request: Request, svc: Service) -> Principal:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    found = svc.tokens.verify(token.strip()) if scheme.lower() == "bearer" else None
    if found is None:
        action = f"{request.method} {request.url.path}"
        svc.audit.record(Entry("anonymous", "-", action, None, 401))
        raise HTTPException(401, "a valid bearer token is required (msa tokens add)")
    return found


def needs(role: Role) -> Callable[[Request], Principal]:
    def check(request: Request) -> Principal:
        svc = service(request)
        who = _principal(request, svc)
        if who.role < role:
            svc.audit.record(
                Entry(
                    who.name,
                    who.role.name.lower(),
                    f"{request.method} {request.url.path}",
                    request.path_params.get("run_id"),
                    403,
                    f"needs {role.name.lower()}",
                )
            )
            raise HTTPException(403, f"this needs the {role.name.lower()} role")
        return who

    return check


Svc = Annotated[Service, Depends(service)]
Viewer = Annotated[Principal, Depends(needs(Role.VIEWER))]
Operator = Annotated[Principal, Depends(needs(Role.OPERATOR))]
Approver = Annotated[Principal, Depends(needs(Role.APPROVER))]
Admin = Annotated[Principal, Depends(needs(Role.ADMIN))]


def act(
    svc: Service, who: Principal, action: str, run_id: str | None, work: Callable[[], Any]
) -> Any:
    """Do ``work`` and audit the result, mapping the core's refusals to HTTP errors."""
    try:
        result = work()
    except UnknownRun as exc:
        status, detail = 404, str(exc)
    except (TransitionRejected, control.NotWaiting, JournalConflict) as exc:
        status, detail = 409, getattr(exc, "detail", None) or str(exc)
    except ValueError as exc:  # includes pydantic's ValidationError
        status, detail = 422, str(exc).splitlines()[0]
    else:
        svc.audit.record(Entry(who.name, who.role.name.lower(), action, run_id, 200))
        return result
    svc.audit.record(Entry(who.name, who.role.name.lower(), action, run_id, status, detail))
    raise HTTPException(status, detail)


def summary(svc: Service, run_id: RunId, opened: SqlJournal) -> dict[str, Any]:
    state = opened.load(run_id)
    asking = state.waiting_since is not None
    return {
        "run_id": run_id,
        "phase": state.phase.value,
        "outcome": state.outcome.model_dump(mode="json") if state.outcome else None,
        "seq": state.seq,
        "pending": [e.command.kind for e in state.pending],
        "waiting_on_person": asking,
        "driving": svc.driving(run_id, state),
        "driver_error": svc.runner.errors.get(run_id),
        "question": svc.files(run_id).read("question.md") if asking else None,
        "state": state.model_dump(mode="json"),
    }


def _events(svc: Service, run_id: RunId, after: int) -> list[dict[str, Any]]:
    with svc.journal() as opened:
        found = opened.events(run_id, after=after)
    return [
        {"seq": after + n, "kind": e.kind, "event": e.model_dump(mode="json")}
        for n, e in enumerate(found, start=1)
    ]


router = APIRouter(prefix="/api")


# ---------- reads


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/me")
def me(who: Viewer) -> dict[str, str]:
    return {"name": who.name, "role": who.role.name.lower()}


@router.get("/runs")
def runs(_: Viewer, svc: Svc) -> list[dict[str, Any]]:
    with svc.journal() as opened:
        return [
            {
                "run_id": r.run_id,
                "phase": r.phase,
                "outcome": r.outcome,
                "seq": r.head_seq,
                "created_at": r.created_at.isoformat(),
                "kind": r.environment.get("kind"),
                "driving": svc.driving(r.run_id) and r.outcome is None,
            }
            for r in opened.runs()
        ]


@router.get("/runs/{run_id}")
def run(run_id: RunPath, _: Viewer, svc: Svc) -> dict[str, Any]:
    with svc.journal() as opened:
        try:
            return summary(svc, RunId(run_id), opened)
        except UnknownRun as exc:
            raise HTTPException(404, str(exc)) from exc


@router.get("/runs/{run_id}/events")
def events(run_id: RunPath, _: Viewer, svc: Svc, after: int = 0) -> list[dict[str, Any]]:
    try:
        return _events(svc, RunId(run_id), max(after, 0))
    except UnknownRun as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/runs/{run_id}/timeline")
def run_timeline(run_id: RunPath, _: Viewer, svc: Svc) -> list[str]:
    with svc.journal() as opened:
        try:
            return timeline(opened.events(RunId(run_id)))
        except UnknownRun as exc:
            raise HTTPException(404, str(exc)) from exc


@router.get("/runs/{run_id}/stream")
async def stream(run_id: RunPath, _: Viewer, svc: Svc, after: int = 0) -> StreamingResponse:
    """Server-sent events: one per journaled event (``id`` is its sequence number, so a
    client reconnects with ``?after=``), then ``end`` once the run is over."""
    rid = RunId(run_id)
    with svc.journal() as opened:
        try:
            opened.load(rid)
        except UnknownRun as exc:
            raise HTTPException(404, str(exc)) from exc
    settings = svc.settings

    async def frames() -> AsyncIterator[str]:
        last, quiet = max(after, 0), 0.0
        while True:
            fresh = _events(svc, rid, last)
            for item in fresh:
                last = item["seq"]
                yield f"id: {last}\nevent: {item['kind']}\ndata: {json.dumps(item)}\n\n"
            with svc.journal() as opened:
                state = opened.load(rid)
            if state.phase.value in TERMINAL:
                yield f"event: end\ndata: {json.dumps({'phase': state.phase.value})}\n\n"
                return
            quiet = 0.0 if fresh else quiet + settings.poll_seconds
            if quiet >= settings.heartbeat_seconds:
                quiet = 0.0
                yield ": still waiting\n\n"
            await asyncio.sleep(settings.poll_seconds)

    return StreamingResponse(frames(), media_type="text/event-stream")


@router.get("/runs/{run_id}/files/{name}", response_class=PlainTextResponse)
def run_file(run_id: RunPath, name: str, _: Viewer, svc: Svc) -> str:
    if name not in FILES:
        raise HTTPException(404, f"no such run file: one of {', '.join(FILES)}")
    text = svc.files(run_id).read(name)
    if text is None:
        raise HTTPException(404, f"{name} has not been written for {run_id}")
    return text


@router.get("/runs/{run_id}/records/{name}")
def run_records(run_id: RunPath, name: str, _: Viewer, svc: Svc) -> list[dict[str, Any]]:
    if name not in RECORDS:
        raise HTTPException(404, f"no such records: one of {', '.join(RECORDS)}")
    return svc.files(run_id).records(f"{name}.jsonl")


@router.get("/workflow")
def workflow(_: Viewer) -> dict[str, Any]:
    """The transition table, for drawing the workflow graph."""
    return {
        "phases": [p.value for p in Phase],
        "terminal": sorted(TERMINAL),
        "rules": [
            {
                "source": rule.source.value,
                "event": rule.event.__name__,
                "targets": sorted(t.value for t in rule.targets),
            }
            for rule in TABLE
        ],
        "escapes": [
            {"event": event, "target": target.value, "why": why}
            for event, target, why in GLOBAL_ESCAPES
        ],
    }


@router.get("/skills")
def skills(_: Viewer, svc: Svc) -> list[dict[str, Any]]:
    try:
        found = registry.Registry.load(svc.settings.repo)
        return [found.latest(i).model_dump(mode="json") for i in found.ids()]
    except registry.SkillError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/policy")
def security_policy(_: Viewer, svc: Svc) -> dict[str, Any]:
    try:
        return policy.load(svc.settings.repo).model_dump(mode="json")
    except PreferenceError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/preferences")
def preferences(_: Viewer, svc: Svc) -> list[dict[str, Any]]:
    try:
        resolved = policy.preferences(svc.settings.repo)
    except PreferenceError as exc:
        raise HTTPException(409, str(exc)) from exc
    return [
        {**p.model_dump(mode="json"), "origin": resolved.origin(p.id)} for p in resolved.preferences
    ]


@router.get("/metrics")
def metrics(_: Viewer, svc: Svc) -> dict[str, Any]:
    """Aggregates over every run in the journal, from the rollups the journal keeps on its
    runs table with every append: one query, whatever the number of runs (ADR-023)."""
    with svc.journal() as opened:
        runs = opened.rollups()
    phases: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for run in runs:
        phases[run.phase] = phases.get(run.phase, 0) + 1
        if run.outcome_reason is not None:
            outcomes[run.outcome_reason] = outcomes.get(run.outcome_reason, 0) + 1
    completed = [r for r in runs if r.phase == Phase.COMPLETED.value]
    return {
        "runs": len(runs),
        "live": sum(r.outcome_reason is None for r in runs),
        "waiting_on_people": sum(r.waiting for r in runs),
        "by_phase": dict(sorted(phases.items())),
        "by_outcome": dict(sorted(outcomes.items())),
        "spent_usd_micros": sum(r.spent_usd_micros for r in runs),
        "paused_seconds": sum(r.paused_seconds for r in runs),
        "completed": len(completed),
        "mean_revisions_completed": (
            sum(r.revisions for r in completed) / len(completed) if completed else None
        ),
        "mean_working_seconds_completed": (
            sum(r.working_seconds for r in completed) / len(completed) if completed else None
        ),
    }


@router.get("/audit")
def audit_log(_: Admin, svc: Svc, limit: int = 500) -> list[dict[str, Any]]:
    return svc.audit.entries(limit=max(1, min(limit, 5_000)))


# ---------- changes
#
# These are ``async``: they run on the server's event loop, where the runner starts its
# drivers. Their journal work is short and synchronous (SQLite, compare-and-set).


@router.post("/runs")
async def start_run(body: StartRun, who: Operator, svc: Svc) -> dict[str, Any]:
    rid = RunId(body.run_id or f"run-{uuid.uuid4().hex[:8]}")
    settings = svc.settings

    def work() -> dict[str, Any]:
        if problem := secrets_in_task(body.task):
            raise ValueError(problem)
        overrides: dict[str, Any] = {}
        if body.budget_usd is not None:
            overrides["budget_usd_micros"] = round(body.budget_usd * 1_000_000)
        config = settings.config
        limits = (config.limits if config else LimitsConfig()).to_limits(**overrides)
        if body.simulate is not None:
            environment = SimulationConfig(
                seed=body.simulate.seed, profile=body.simulate.profile
            ).model_dump(mode="json")
        elif config is None:
            raise ValueError("live runs need msa.toml: model ids and prices live there")
        else:
            environment = live.LiveEnvironment(
                repo=str(settings.repo.resolve()),
                state_dir=str(settings.state_dir.resolve()),
                task=body.task,
                efforts=config.models.efforts(),
            ).model_dump(mode="json")
        with svc.journal() as opened:
            if any(r.run_id == rid for r in opened.runs()):
                raise ValueError(f"run {rid} already exists")
        digest = Digest(digest_of(body.task.encode("utf-8")))
        launch.update(task_digest=digest, limits=limits, environment=environment)
        if svc.bridge is None:
            svc.runner.start(rid, task_digest=digest, limits=limits, environment=environment)
        return {"run_id": rid, "kind": environment.get("kind")}

    launch: dict[str, Any] = {}
    result: dict[str, Any] = act(svc, who, "start", rid, work)
    if svc.bridge is not None:
        await svc.bridge.start(rid, **launch)  # msa worker drives it
        result["driver"] = "temporal"
    return result


async def change(
    svc: Service,
    who: Principal,
    action: str,
    run_id: str,
    do: Callable[[control.Ledger], control.Done | None],
    *,
    drive: bool,
) -> dict[str, Any]:
    """Validate and record one action through the core. When a Temporal workflow drives
    the run, the event goes to it as a signal (it journals it), and nothing here drives
    the run; otherwise it is journaled here and, if asked, driven in this process."""
    rid = RunId(run_id)
    bridge = svc.bridge
    by_workflow = bridge is not None and await bridge.driving(rid)
    held: list[control.Deferred] = []

    def work() -> dict[str, Any]:
        with svc.journal() as opened:
            ledger: control.Ledger = opened
            if by_workflow:
                held.append(deferred := control.Deferred(opened))
                ledger = deferred
            done = do(ledger)
            after = summary(svc, rid, opened)
        if drive and after["phase"] not in TERMINAL and not by_workflow and bridge is None:
            svc.runner.drive(rid)
            after["driving"] = True
        return {"event": done.event.model_dump(mode="json") if done else None, "run": after}

    result: dict[str, Any] = act(svc, who, action, run_id, work)
    if bridge is not None and by_workflow:
        for event in held[0].events:
            await bridge.send(rid, cast(AnyEvent, event))
        result["run"]["driving"] = True
        result["driver"] = "temporal"
    elif bridge is not None and drive and result["run"]["phase"] not in TERMINAL:
        with svc.journal() as opened:
            environment = opened.environment(rid)
        await bridge.adopt(rid, environment)  # nothing drove it: a workflow does now
        result["run"]["driving"] = True
        result["driver"] = "temporal"
    return result


@router.post("/runs/{run_id}/pause")
async def pause(run_id: RunPath, body: Reason, who: Operator, svc: Svc) -> dict[str, Any]:
    clock, reason = svc.settings.clock, body.reason or "paused from the API"
    return await change(
        svc,
        who,
        "pause",
        run_id,
        lambda j: control.pause(j, RunId(run_id), clock, by=who.name, reason=reason),
        drive=False,
    )


@router.post("/runs/{run_id}/resume")
async def resume(run_id: RunPath, who: Operator, svc: Svc) -> dict[str, Any]:
    clock = svc.settings.clock
    return await change(
        svc,
        who,
        "resume",
        run_id,
        lambda j: control.resume(j, RunId(run_id), clock, by=who.name),
        drive=True,
    )


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: RunPath, body: Reason, who: Operator, svc: Svc) -> dict[str, Any]:
    clock, reason = svc.settings.clock, body.reason or f"cancelled by {who.name}"
    return await change(
        svc,
        who,
        "cancel",
        run_id,
        lambda j: control.cancel(j, RunId(run_id), clock, reason=reason),
        drive=False,
    )


@router.post("/runs/{run_id}/unblock")
async def unblock(run_id: RunPath, body: Note, who: Operator, svc: Svc) -> dict[str, Any]:
    clock = svc.settings.clock
    return await change(
        svc,
        who,
        "unblock",
        run_id,
        lambda j: control.unblock(j, RunId(run_id), clock, by=who.name, note=body.note),
        drive=True,
    )


@router.post("/runs/{run_id}/approve")
async def approve(
    run_id: RunPath, who: Approver, svc: Svc, body: Approval | None = None
) -> dict[str, Any]:
    clock = svc.settings.clock
    config = svc.settings.config
    default = config.publish.mode if config is not None else PublishMode.LOCAL
    return await change(
        svc,
        who,
        "approve",
        run_id,
        lambda j: control.decide(
            j,
            RunId(run_id),
            clock,
            by=who.name,
            decision="approve",
            publish=body.publish if body is not None else None,
            default_publish=default,
        ),
        drive=True,
    )


@router.post("/runs/{run_id}/reject")
async def reject(run_id: RunPath, body: Feedback, who: Approver, svc: Svc) -> dict[str, Any]:
    clock = svc.settings.clock
    return await change(
        svc,
        who,
        "reject",
        run_id,
        lambda j: control.decide(
            j, RunId(run_id), clock, by=who.name, decision="reject", feedback=body.feedback
        ),
        drive=True,
    )


@router.post("/runs/{run_id}/abort")
async def abort(run_id: RunPath, body: Reason, who: Approver, svc: Svc) -> dict[str, Any]:
    clock, reason = svc.settings.clock, body.reason or "stopped from the API"
    return await change(
        svc,
        who,
        "abort",
        run_id,
        lambda j: control.abort(j, RunId(run_id), clock, by=who.name, reason=reason),
        drive=False,
    )


@router.post("/runs/{run_id}/rule")
async def rule(run_id: RunPath, body: Ruling, who: Approver, svc: Svc) -> dict[str, Any]:
    clock, verdicts = svc.settings.clock, dict(body.verdicts)
    return await change(
        svc,
        who,
        "rule",
        run_id,
        lambda j: control.rule(j, RunId(run_id), clock, by=who.name, verdicts=verdicts),
        drive=True,
    )


# ---------- the app


def default_factory(settings: Settings) -> Factory:
    """Simulated runs answer from the simulator; live runs use the real executors and
    model, configured by msa.toml, exactly as ``msa resume`` would."""

    @asynccontextmanager
    async def make(
        run_id: RunId, environment: dict[str, Any], journal: SqlJournal
    ) -> AsyncIterator[Interpreter]:
        snapshot_every = settings.config.journal.snapshot_every if settings.config else 50
        if environment.get("kind") == "simulation":
            yield Interpreter(
                journal,
                SimulatedEnvironment(SimulationConfig.model_validate(environment)),
                clock=LogicalClock(),
                observer=Traced(Observer()),
                snapshot_every=snapshot_every,
            )
            return
        if settings.config is None:
            raise RuntimeError("live runs need msa.toml: model ids and prices live there")
        client = AnthropicMessages.from_config(settings.config)
        with SqliteResponseStore(settings.state_dir / "responses.db") as responses:
            try:
                yield live.interpreter(
                    live.LiveEnvironment.model_validate(environment),
                    settings.config,
                    run_id=run_id,
                    journal=journal,
                    client=client,
                    store=responses,
                    clock=SystemClock(),
                    observer=Traced(Observer()),
                )
            finally:
                await client.aclose()

    return make


def create_app(settings: Settings, *, factory: Factory | None = None) -> FastAPI:
    runner = Runner(settings.open_journal, factory or default_factory(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await runner.close()

    app = FastAPI(title="AEGIS", version=__version__, lifespan=lifespan)
    temporal = settings.temporal
    snapshot_every = settings.config.journal.snapshot_every if settings.config else 50
    app.state.service = Service(
        settings=settings,
        tokens=TokenStore(settings.state_dir / "tokens.json"),
        audit=AuditLog(settings.state_dir / "audit.jsonl"),
        runner=runner,
        bridge=(
            TemporalBridge(temporal, snapshot_every=snapshot_every)
            if temporal is not None and temporal.address
            else None
        ),
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def traced(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        name = f"{request.method} {request.url.path}"
        with tracing.tracer().start_as_current_span(name) as span:
            span.set_attribute("http.request.method", request.method)
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
            return response

    app.include_router(router)
    return app

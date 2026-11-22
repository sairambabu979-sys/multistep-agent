"""A live run: real executors against a real repository and a real (or recorded) model.

``LiveEnvironment`` is what the journal stores for a live run, so ``msa resume`` rebuilds
the same executors. ``executors`` wires one executor per effect kind:

| effect | executor |
|---|---|
| prepare_workspace | intake (M5), then the run's worktree at the base tree |
| request_plan | the Planner (``msa.runtime.roles``) |
| implement_plan | the Coder, in the build worktree |
| author_acceptance_tests | the Tester, in its own worktree at the base tree (blind) |
| revise_changes | the Coder or the Tester, whichever owns the revision |
| integrate_build | the Coder's tree with the Tester's directories overlaid: the head |
| run_verification | ``msa.runtime.verify`` |
| run_test_suite | ``msa.runtime.testing`` |
| finalize | the patch from base to head, stored as an artifact |
| request_review | the Reviewer (``msa.runtime.review``), or the stand-in below |
| run_negotiation_round | the Coder as author, then the Reviewer replying |
| request_arbitration | the ladder: repro test, preference, style, then the Arbiter |
| await_plan_approval, | a person: the question is written to the run directory and |
| await_human_ruling | the run parks until ``msa approve`` / ``msa rule`` answers it |

With ``[review].enabled = false`` the Reviewer is replaced by a pass-through stand-in
(arm B of the ablation, PLAN §11: Planner + Coder + Tester + VERIFYING). It answers at
once with no findings, spends nothing, and the run report names it, so no reader mistakes
it for a review.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from msa.agents.schemas import Plan
from msa.config import MsaConfig, Role
from msa.core import RunState
from msa.core._frozen import Frozen
from msa.core.effects import (
    AwaitHumanRuling,
    AwaitPlanApproval,
    Effect,
    Finalize,
    IntegrateBuild,
    RequestReview,
)
from msa.core.events import (
    AnyEvent,
    BuildIntegrated,
    EventBase,
    Finalized,
    IntakeCompleted,
    ReviewProduced,
)
from msa.core.ids import Digest, RunId
from msa.core.values import Phase
from msa.llm.client import MessagesClient
from msa.llm.gateway import Gateway
from msa.llm.store import ResponseStore
from msa.observability import report
from msa.runtime import review
from msa.runtime.artifacts import PLAN_FILE, RunFiles, digest_of
from msa.runtime.clock import Clock
from msa.runtime.context import RunContext
from msa.runtime.executors import Router
from msa.runtime.intake import IntakeExecutor
from msa.runtime.interpreter import Executor, Interpreter, Observer, Parked
from msa.runtime.journal import SqliteJournal
from msa.runtime.roles import (
    CoderExecutor,
    PlannerExecutor,
    Revisions,
    TesterExecutor,
)
from msa.runtime.testing import TestSuiteExecutor
from msa.runtime.verify import VerifyExecutor
from msa.runtime.worktree import RunWorktree
from msa.skills.preferences import Resolved, store
from msa.tools.audit import Violation, log_violation
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox

STAND_INS: dict[str, str] = {
    "request_review": "Reviewer (M8): no review; no findings",
}


class LiveEnvironment(Frozen):
    """Everything needed to rebuild a live run's executors. Stored in the journal."""

    kind: Literal["live"] = "live"
    repo: str  # absolute path of the repository
    state_dir: str  # absolute path of the state directory (.msa)
    task: str
    overrides: tuple[str, ...] = ()  # --pref
    cassette: str | None = None
    cassette_mode: Literal["record", "replay"] | None = None

    @property
    def task_digest(self) -> Digest:
        return digest_of(self.task.encode("utf-8"))


# ---------- the executors that are not agents


class WorkspaceIntake:
    """INTAKE, then the run's build and Tester worktrees at the base tree, before the event
    is journaled: once intake has completed, every later executor (also after a resume)
    finds them, and the two BUILDING regions never race to create theirs."""

    def __init__(self, intake: IntakeExecutor, *worktrees: RunWorktree) -> None:
        self.intake = intake
        self.worktrees = worktrees

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        event = await self.intake.execute(effect, now=now)
        if isinstance(event, IntakeCompleted):
            for worktree in self.worktrees:
                await worktree.ensure(event.base_tree)
        return event


class Integrate:
    """Join the two BUILDING regions: the Tester's directories, as its worktree holds them,
    overlaid on the Coder's tree. Each role could write only its own paths, so the overlay
    cannot conflict, and running it again after a crash changes nothing."""

    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, IntegrateBuild):
            raise TypeError(f"integration cannot execute {command.kind}")
        ctx = self.context
        if ctx.tester_worktree.exists:
            tests = await ctx.tester_worktree.snapshot()
            owned = ctx.config.capabilities.owned_directories(Role.TESTER)
            await ctx.worktree.overlay(tests, command.base_tree, owned)
        head = await ctx.worktree.snapshot()
        return BuildIntegrated(at=now, effect_id=effect.id, head_tree=head)


class FinalizeExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, Finalize):
            raise TypeError(f"finalize cannot execute {command.kind}")
        ctx = self.context
        patch = await ctx.worktree.diff(await ctx.worktree.base_tree(), command.tree)
        artifact = ctx.files.put(patch)
        ctx.files.write("patch.diff", patch)
        return Finalized(at=now, effect_id=effect.id, tree=command.tree, artifact=artifact)


class HumanGate:
    """A plan gate or a dispute for a person: the question goes to ``question.md`` in the
    run directory, and the run parks. The clock is stopped until they answer."""

    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        ctx = self.context
        run = ctx.run_id
        match effect.command:
            case AwaitPlanApproval(plan=ref):
                plan = Plan.model_validate_json(ctx.files.get(ref.digest)).render()
                question = (
                    f"# Run {run}: approve the plan?\n\n{plan}\n"
                    f"Approve: `msa approve {run}`\n\n"
                    f'Send it back: `msa approve {run} --reject "what to change"`\n\n'
                    f'Stop the run: `msa approve {run} --abort "why"`\n'
                )
            case AwaitHumanRuling(findings=ids):
                disputes = "\n\n".join(review.dispute(ctx, f) for f in ids)
                example = " ".join(f"{f}=upheld" for f in ids)
                question = (
                    f"# Run {run}: rule on {len(ids)} disputed finding(s)\n\n"
                    "The reviewer and the author could not settle these, and no repro test, "
                    "preference or style rule decided them.\n\n"
                    f"{disputes}\n\n"
                    "Rule on every finding: upheld (the code must change), dismissed (it "
                    "stays), or deferred (a follow-up):\n\n"
                    f"`msa rule {run} {example}`\n\n"
                    f'Stop the run instead: `msa rule {run} --abort "why"`\n'
                )
            case other:
                raise TypeError(f"no person answers {other.kind}")
        path = ctx.files.write("question.md", question)
        raise Parked(f"waiting for a person: {path}")


class StandIns:
    """The pass-through answer described in the module docstring."""

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        await asyncio.sleep(0)  # answer like I/O would: after the other tasks get a turn
        match effect.command:
            case RequestReview(tree=tree):
                return ReviewProduced(at=now, effect_id=effect.id, tree=tree, findings=())
            case other:
                raise TypeError(f"no stand-in for {other.kind}")


class Unavailable:
    """An effect no executor handles. The interpreter turns the error into a failure
    event, so the run stops with this message instead of stalling."""

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        raise NotImplementedError(f"{effect.command.kind} has no executor")


# ---------- wiring


def executors(context: RunContext, intake: IntakeExecutor) -> Router:
    coder, tester = CoderExecutor(context), TesterExecutor(context)
    person = HumanGate(context)
    reviewing: Executor = (
        review.ReviewExecutor(context) if context.config.review.enabled else StandIns()
    )
    routes: dict[str, Executor] = {
        "prepare_workspace": WorkspaceIntake(intake, context.worktree, context.tester_worktree),
        "request_plan": PlannerExecutor(context),
        "implement_plan": coder,
        "author_acceptance_tests": tester,
        "revise_changes": Revisions(coder, tester),
        "integrate_build": Integrate(context),
        "run_verification": VerifyExecutor(context),
        "run_test_suite": TestSuiteExecutor(context),
        "finalize": FinalizeExecutor(context),
        "request_review": reviewing,
        "run_negotiation_round": review.NegotiationExecutor(context),
        "request_arbitration": review.ArbitrationExecutor(context),
        "await_plan_approval": person,
        "await_human_ruling": person,
    }
    return Router(routes, fallback=Unavailable())


def context(
    environment: LiveEnvironment,
    config: MsaConfig,
    gateway: Gateway,
    *,
    run_id: RunId,
    on_violation: Callable[[Violation], None] = log_violation,
    intake: IntakeExecutor,
) -> RunContext:
    repo, state_dir = Path(environment.repo), Path(environment.state_dir)

    def preferences() -> Resolved:
        # Resolved by INTAKE; after a resume that skipped INTAKE, resolved again from the
        # same layers (an edit to a layer file between crash and resume is picked up).
        return intake.resolved or store.load(repo, environment.overrides)

    return RunContext(
        run_id=run_id,
        task=environment.task,
        config=config,
        gateway=gateway,
        worktree=RunWorktree.for_run(repo, state_dir, run_id),
        tester_worktree=RunWorktree.for_run(repo, state_dir, run_id, "tests"),
        base_worktree=RunWorktree.for_run(repo, state_dir, run_id, "base"),
        repro_worktree=RunWorktree.for_run(repo, state_dir, run_id, "repro"),
        files=RunFiles.for_run(state_dir, run_id),
        preferences=preferences,
        on_violation=on_violation,
    )


# ---------- the report


def write_report(
    files: RunFiles, environment: LiveEnvironment, state: RunState, events: Sequence[EventBase]
) -> Path:
    stored = files.read(PLAN_FILE)
    tests = files.records("tests.jsonl")
    reviewed = bool(files.records(review.REVIEWS))
    text = report.render(
        state,
        events,
        task=environment.task,
        plan=Plan.model_validate_json(stored).render() if stored else None,
        invocations=files.records("invocations.jsonl"),
        verifications=files.records("verifications.jsonl"),
        summaries=files.records("summaries.jsonl"),
        stand_ins={} if reviewed else STAND_INS,
        patch=files.read("patch.diff") if state.phase is Phase.DONE else None,
        tests=tests,
        last_test_report=files.text(Digest(tests[-1]["report"])) if tests else None,
        review=report.Review(
            reviews=files.records(review.REVIEWS),
            findings=files.records(review.FINDINGS),
            rounds=files.records(review.ROUNDS),
            rulings=files.records(review.RULINGS),
        ),
    )
    return files.write("report.md", text)


def interpreter(
    environment: LiveEnvironment,
    config: MsaConfig,
    *,
    run_id: RunId,
    journal: SqliteJournal,
    client: MessagesClient,
    store: ResponseStore,
    clock: Clock,
    observer: Observer | None = None,
    on_violation: Callable[[Violation], None] = log_violation,
    on_report: Callable[[Path], None] | None = None,
) -> Interpreter:
    """The interpreter that drives (or resumes) a live run, writing its report at the end."""
    repo = Path(environment.repo)
    intake = IntakeExecutor(
        repo,
        LocalSandbox(Root.at(repo), config.sandbox, role="intake", on_violation=on_violation),
        overrides=environment.overrides,
    )
    run = context(
        environment,
        config,
        Gateway(client, config, store=store),
        run_id=run_id,
        on_violation=on_violation,
        intake=intake,
    )

    def finished(state: RunState) -> None:
        path = write_report(run.files, environment, state, journal.events(state.run_id))
        if on_report is not None:
            on_report(path)

    return Interpreter(
        journal,
        executors(run, intake),
        clock=clock,
        observer=observer,
        on_outcome=finished,
        on_park=finished,
        snapshot_every=config.journal.snapshot_every,
    )

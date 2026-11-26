"""A live run: real executors against a real repository and a real (or recorded) model.

``LiveEnvironment`` is what the journal stores for a live run, so ``msa resume`` rebuilds
the same executors. ``executors`` wires one executor per effect kind:

| effect | executor |
|---|---|
| admit_run | admission policy and the repository's live runs (``msa.runtime.intake``) |
| validate_request | the task, the preferences, the repository |
| load_context | the base commit and tree, the worktrees, the repository map |
| request_plan | the Planner (``msa.runtime.roles``) |
| implement_plan | the Coder, in the build worktree |
| author_acceptance_tests | the Tester, in its own worktree at the base tree (blind) |
| revise_changes | the Coder or the Tester, whichever owns the revision |
| integrate_build | the Coder's tree with the Tester's directories overlaid: the head |
| run_static_analysis | ``msa.runtime.verify`` |
| run_unit_tests | ``msa.runtime.testing`` |
| run_integration_tests | ``msa.runtime.integration`` |
| run_security_review | ``msa.runtime.security`` |
| request_review | the Reviewer (``msa.runtime.review``), or the stand-in below |
| run_negotiation_round | the Coder as author, then the Reviewer replying |
| request_arbitration | the ladder: repro test, preference, style, then the Arbiter |
| verify_release | the Release Verifier and the PR bundle (``msa.runtime.release``) |
| publish_release | the approved commit on a local branch; pushed, with a PR, if asked |
| await_* (plan, ruling, release, unblock, resume) | a person, from outside (below) |

A person's question is written to ``question.md`` in the run directory, and the run parks
until ``msa approve``, ``msa rule``, ``msa unblock`` or ``msa resume`` answers it.

With ``[review].enabled = false`` the Reviewer is replaced by a pass-through stand-in
(arm B of the ablation, PLAN §11). It answers at once with no findings, spends nothing,
and the run report names it, so no reader mistakes it for a review.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Literal

from msa.agents.schemas import Plan
from msa.config import MsaConfig, Role
from msa.core import RunState, Step, TransitionRejected
from msa.core._frozen import Frozen
from msa.core.effects import (
    AwaitHumanRuling,
    AwaitPlanApproval,
    AwaitReleaseApproval,
    AwaitResume,
    AwaitUnblock,
    Effect,
    IntegrateBuild,
    RequestReview,
)
from msa.core.events import (
    AnyEvent,
    BuildIntegrated,
    EventBase,
    ReviewProduced,
)
from msa.core.ids import Digest, RunId
from msa.core.values import Phase
from msa.llm.client import MessagesClient
from msa.llm.gateway import Gateway
from msa.llm.store import ResponseStore
from msa.observability import report
from msa.runtime import integration, review, security
from msa.runtime.artifacts import PLAN_FILE, SUMMARIES, RunFiles, digest_of
from msa.runtime.clock import Clock
from msa.runtime.context import RunContext
from msa.runtime.executors import Router
from msa.runtime.intake import AdmissionExecutor, ContextExecutor, ValidationExecutor
from msa.runtime.integration import IntegrationExecutor
from msa.runtime.interpreter import Executor, Interpreter, Observer, Parked
from msa.runtime.journal import SqlJournal
from msa.runtime.release import PR_FILE, PublishExecutor, ReleaseExecutor
from msa.runtime.roles import (
    CoderExecutor,
    PlannerExecutor,
    Revisions,
    TesterExecutor,
)
from msa.runtime.security import SecurityExecutor
from msa.runtime.testing import TestSuiteExecutor
from msa.runtime.verify import VerifyExecutor
from msa.runtime.worktree import RunWorktree
from msa.skills import policy
from msa.skills.policy import Policy
from msa.skills.preferences import Resolved
from msa.skills.registry import Registry
from msa.tools.audit import Violation, log_violation
from msa.tools.errors import CommandUnavailable
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox, container_problem

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


class Integrate:
    """Join the two IMPLEMENTING regions: the Tester's directories, as its worktree holds them,
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


class HumanGate:
    """A question for a person: it goes to ``question.md`` in the run directory, and the
    run parks. The clock is stopped until they answer (ADR-014)."""

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
            case AwaitReleaseApproval(tree=tree):
                pr = ctx.files.read(PR_FILE) or "(pr.md is missing)"
                question = (
                    f"# Run {run}: approve the release?\n\n"
                    "Every gate passed on the tree below. Read the description and the patch "
                    f"(`patch.diff` in this directory), then:\n\n"
                    f"Approve and publish to a local branch: `msa approve {run}`\n\n"
                    f'Send it back for changes: `msa approve {run} --reject "what to change"`'
                    f'\n\nStop the run: `msa approve {run} --abort "why"`\n\n---\n\n{pr}'
                )
                patch = await ctx.worktree.diff(await ctx.worktree.base_tree(), tree)
                ctx.files.write("patch.diff", patch)
            case AwaitUnblock(reason=reason):
                question = (
                    f"# Run {run} is blocked\n\n{reason}\n\n"
                    "No agent can fix this: it is the environment (a missing tool or service), "
                    "or a test that was broken before the run. Fix it, then rerun the stage:"
                    f'\n\n`msa unblock {run} --note "what you fixed"`\n\n'
                    f'Stop the run instead: `msa unblock {run} --abort "why"`\n'
                )
            case AwaitResume():
                question = (
                    f"# Run {run} is paused\n\nContinue it where it stopped: "
                    f"`msa resume {run}`\n\nStop it: `msa cancel {run}`\n"
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


def executors(
    context: RunContext,
    admission: AdmissionExecutor,
    validation: ValidationExecutor,
    loading: ContextExecutor,
) -> Router:
    coder, tester = CoderExecutor(context), TesterExecutor(context)
    person = HumanGate(context)
    reviewing: Executor = (
        review.ReviewExecutor(context) if context.config.review.enabled else StandIns()
    )
    routes: dict[str, Executor] = {
        "admit_run": admission,
        "validate_request": validation,
        "load_context": loading,
        "request_plan": PlannerExecutor(context),
        "implement_plan": coder,
        "author_acceptance_tests": tester,
        "revise_changes": Revisions(coder, tester),
        "integrate_build": Integrate(context),
        "run_static_analysis": VerifyExecutor(context),
        "run_unit_tests": TestSuiteExecutor(context),
        "run_integration_tests": IntegrationExecutor(context),
        "run_security_review": SecurityExecutor(context),
        "request_review": reviewing,
        "run_negotiation_round": review.NegotiationExecutor(context),
        "request_arbitration": review.ArbitrationExecutor(context),
        "verify_release": ReleaseExecutor(context),
        "publish_release": PublishExecutor(context),
        "await_plan_approval": person,
        "await_human_ruling": person,
        "await_release_approval": person,
        "await_unblock": person,
        "await_resume": person,
    }
    return Router(routes, fallback=Unavailable())


def context(
    environment: LiveEnvironment,
    config: MsaConfig,
    gateway: Gateway,
    *,
    run_id: RunId,
    on_violation: Callable[[Violation], None] = log_violation,
    validation: ValidationExecutor,
) -> RunContext:
    repo, state_dir = Path(environment.repo), Path(environment.state_dir)

    def preferences() -> Resolved:
        # Resolved by VALIDATING; after a resume that skipped it, resolved again from the
        # same layers (an edit to a layer file between crash and resume is picked up).
        return validation.resolved or policy.preferences(repo, environment.overrides)

    @cache
    def skills() -> Registry:
        return Registry.load(repo)

    @cache
    def security() -> Policy:
        return policy.load(repo)

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
        skills=skills,
        policy=security,
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
        summaries=files.records(SUMMARIES),
        stand_ins={} if reviewed else STAND_INS,
        patch=files.read("patch.diff") if state.phase is Phase.COMPLETED else None,
        tests=tests,
        last_test_report=files.text(Digest(tests[-1]["report"])) if tests else None,
        review=report.Review(
            reviews=files.records(review.REVIEWS),
            findings=files.records(review.FINDINGS),
            rounds=files.records(review.ROUNDS),
            rulings=files.records(review.RULINGS),
        ),
        gates=report.Gates(
            integrations=files.records(integration.RECORDS),
            security=files.records(security.RECORDS),
            releases=files.records("releases.jsonl"),
        ),
    )
    return files.write("report.md", text)


EFFECTS = "effects.jsonl"


class EffectLog(Observer):
    """Records every effect an executor starts, in the run's ``effects.jsonl``, durably and
    before the executor runs. It is the audit of what ran. After a crash it shows what ran
    again: nothing the journal had answered, and the effects in flight at least once
    (ADR-007). ``invocations.jsonl`` records only agent invocations, so it cannot show that
    for the stages that call no model."""

    def __init__(self, files: RunFiles, inner: Observer | None = None) -> None:
        self.files = files
        self.inner = inner or Observer()

    def effect_started(self, effect: Effect) -> None:
        self.files.record(EFFECTS, {"effect": effect.id, "kind": effect.command.kind})
        self.inner.effect_started(effect)

    def before_commit(self, event: AnyEvent, step: Step) -> None:
        self.inner.before_commit(event, step)

    def after_commit(self, event: AnyEvent, step: Step) -> None:
        self.inner.after_commit(event, step)

    def rejected(self, event: AnyEvent, error: TransitionRejected) -> None:
        self.inner.rejected(event, error)


def interpreter(
    environment: LiveEnvironment,
    config: MsaConfig,
    *,
    run_id: RunId,
    journal: SqlJournal,
    client: MessagesClient,
    store: ResponseStore,
    clock: Clock,
    observer: Observer | None = None,
    on_violation: Callable[[Violation], None] = log_violation,
    on_report: Callable[[Path], None] | None = None,
) -> Interpreter:
    """The interpreter that drives (or resumes) a live run, writing its report at the end.
    With the container backend and no usable runtime, it refuses: nothing would run."""
    if (problem := container_problem(config.sandbox)) is not None:
        raise CommandUnavailable(problem)
    repo = Path(environment.repo)
    sandbox = LocalSandbox(Root.at(repo), config.sandbox, role="intake", on_violation=on_violation)
    validation = ValidationExecutor(
        repo, sandbox, task=environment.task, overrides=environment.overrides, config=config
    )
    run = context(
        environment,
        config,
        Gateway(client, config, store=store),
        run_id=run_id,
        on_violation=on_violation,
        validation=validation,
    )
    admission = AdmissionExecutor(
        config.admission,
        limits=lambda: journal.load(run_id).limits,
        live_runs=lambda: live_runs_of(journal, environment.repo, besides=run_id),
    )
    loading = ContextExecutor(
        sandbox,
        run.files,
        run_id,
        (run.worktree, run.tester_worktree),
    )

    def finished(state: RunState) -> None:
        path = write_report(run.files, environment, state, journal.events(state.run_id))
        if on_report is not None:
            on_report(path)

    return Interpreter(
        journal,
        executors(run, admission, validation, loading),
        clock=clock,
        observer=EffectLog(run.files, observer),
        on_outcome=finished,
        on_park=finished,
        snapshot_every=config.journal.snapshot_every,
    )


def live_runs_of(journal: SqlJournal, repo: str, *, besides: RunId) -> int:
    """Live runs of ``repo`` other than ``besides``, parked ones included."""
    return sum(
        1
        for run in journal.runs()
        if run.run_id != besides
        and run.outcome is None
        and run.environment.get("kind") == "live"
        and run.environment.get("repo") == repo
    )

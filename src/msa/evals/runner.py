"""Running the corpus through the system, one arm at a time (``msa eval run``).

Each (task, arm) pair is one live run, driven in-process by the same interpreter ``msa run``
uses, on a fresh copy of the task's base repository. Nobody is there to rule on a dispute,
so the run escalates to the Arbiter (``escalation = "arbiter"``); and nobody approves the
release, so a run that reaches HUMAN_APPROVAL has delivered: its head tree is what gets
scored, with the task's hidden tests, in the sandbox.

Arms (PLAN §11). Only B and D exist so far:

- **B**: Planner, Coder, Tester and every gate; the Reviewer is the pass-through stand-in.
- **D**: B plus the Reviewer, negotiation and arbitration: the full system.
- A (no Tester) and C (the author must accept every finding) need runtime changes first.

Results are appended to ``results.jsonl`` one line per run as each finishes, so an eval that
stops part-way (a crash, a budget cap) resumes where it stopped and never pays twice for a
finished run.

A run that raises (the harness, the model client or the scoring broke) is recorded with an
``error`` and is not evidence; it is tried once more (``ATTEMPTS``), so one broken run does
not leave an arm short of the results the decision rule needs. The errored attempt stays in
``results.jsonl`` with what its model calls recorded spending, which counts against
``max_total_usd``.
"""

from __future__ import annotations

import json
import shutil
import time
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from msa.agents.schemas import Plan
from msa.config import MsaConfig
from msa.core.ids import RunId
from msa.core.state import RunState
from msa.core.values import Escalation, Phase
from msa.evals.corpus import Task, export_tree, materialize
from msa.evals.scoring import hidden_tests
from msa.llm.client import MessagesClient
from msa.llm.store import SqliteResponseStore
from msa.runtime import live
from msa.runtime.artifacts import PLAN_FILE, RunFiles
from msa.runtime.clock import SystemClock
from msa.runtime.journal import SqliteJournal
from msa.runtime.review import FINDINGS, ROUNDS, RULINGS

Arm = Literal["B", "D"]
ARMS: dict[str, str] = {
    "B": "Planner, Coder, Tester and every gate, no Reviewer",
    "D": "B plus the Reviewer, negotiation and arbitration (the full system)",
}
UNBUILT = {
    "A": "arm A (no Tester) needs UNIT_TESTING to accept a run without acceptance tests",
    "C": "arm C (the author accepts every finding) needs a negotiation that cannot dispute",
}
RESULTS = "results.jsonl"
ATTEMPTS = 2  # per (task, arm, repeat): one more after an attempt that errored
INVOCATIONS = "invocations.jsonl"
DELIVERED = frozenset({Phase.HUMAN_APPROVAL, Phase.COMPLETED})

ClientFactory = Callable[[MsaConfig, live.LiveEnvironment], MessagesClient]


class ArmUnavailable(ValueError):
    """An arm the harness cannot run yet."""


class FindingRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    severity: str  # as admitted (a blocker on the Tester's tests is reported as minor)
    kind: str
    admitted: bool
    claim: str
    verdict: str | None  # None: advisory, or never settled
    rung: str | None  # what settled it: negotiation, repro, preference, style, arbiter


class Result(BaseModel):
    """One (task, arm) run: what happened, what it cost, and whether it resolved."""

    model_config = ConfigDict(frozen=True)

    task: str
    kind: str
    expect: str
    arm: str
    repeat: int
    run_id: str
    phase: str
    outcome: str | None  # why it stopped, if it stopped short of delivering
    delivered: bool
    resolved: bool | None  # None for a trap: a person judges it from plan_risks
    hidden_passed: int = 0
    hidden_failed: list[str] = []
    hidden_error: str | None = None
    hidden_isolated: bool = False
    spend_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    wall_seconds: float = 0.0
    revisions: int = 0
    negotiation_rounds: int = 0
    findings: list[FindingRow] = []
    plan_risks: list[str] = []
    error: str | None = None  # the harness itself failed; the run is not evidence
    efforts: dict[str, str] = {}  # each role's effort (msa.toml with any --effort)


def arm_config(settings: MsaConfig, arm: str, budget_usd: float) -> MsaConfig:
    if arm in UNBUILT:
        raise ArmUnavailable(UNBUILT[arm])
    if arm not in ARMS:
        raise ArmUnavailable(f"no arm {arm!r}: the arms are A, B, C and D")
    limits = settings.limits.model_copy(
        update={"escalation": Escalation.ARBITER, "budget_usd": budget_usd}
    )
    review = settings.review.model_copy(update={"enabled": arm == "D"})
    return settings.model_copy(update={"limits": limits, "review": review})


async def run_one(
    task: Task,
    arm: str,
    settings: MsaConfig,
    workdir: Path,
    client_factory: ClientFactory,
    *,
    budget_usd: float,
    repeat: int = 0,
) -> Result:
    """One live run of ``task`` under ``arm`` in ``workdir`` (replaced if it exists)."""
    config = arm_config(settings, arm, budget_usd)
    run_id = RunId(f"eval-{task.id}-{arm}-{repeat}")
    if workdir.exists():  # a run that stopped before recording its result starts over
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    repo = materialize(task, workdir / "repo")
    state_dir = workdir / "state"
    environment = live.LiveEnvironment(
        repo=str(repo), state_dir=str(state_dir), task=task.text, efforts=config.models.efforts()
    )
    record: dict[str, object] = {
        "task": task.id,
        "kind": task.kind,
        "expect": task.expect,
        "arm": arm,
        "repeat": repeat,
        "run_id": run_id,
        "efforts": {role.value: effort.value for role, effort in environment.efforts.items()},
    }
    files = RunFiles.for_run(state_dir, run_id)
    started = time.monotonic()
    try:
        state = await _drive(environment, config, run_id, client_factory)
        record["wall_seconds"] = time.monotonic() - started
        delivered = state.phase in DELIVERED and state.head_tree is not None
        record |= await _score(task, state, repo, workdir, config)
    except Exception as exc:  # the eval goes on; this attempt is not evidence
        return Result.model_validate(
            record
            | _recorded_spend(files)
            | {
                "phase": "none",
                "outcome": None,
                "delivered": False,
                "resolved": None if task.expect == "flag" else False,
                "wall_seconds": time.monotonic() - started,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    spent = state.ledger.spent
    invocations = files.records(INVOCATIONS)
    return Result.model_validate(
        record
        | {
            "phase": state.phase.value,
            "outcome": None if delivered else _outcome(state),
            "delivered": delivered,
            "spend_usd": spent.usd_micros / 1_000_000,
            "input_tokens": spent.input_tokens,
            "output_tokens": spent.output_tokens,
            "cache_read_tokens": sum(int(r.get("cache_read_tokens", 0)) for r in invocations),
            "revisions": state.revisions,
            "negotiation_rounds": len({r["effect"] for r in files.records(ROUNDS)}),
            "findings": findings(files),
            "plan_risks": _risks(files),
        }
    )


async def _score(
    task: Task, state: RunState, repo: Path, workdir: Path, config: MsaConfig
) -> dict[str, object]:
    """A delivered tree's hidden-test score; a trap, or a run that delivered nothing, is not
    scored by tests."""
    if task.expect != "resolve" or state.phase not in DELIVERED or state.head_tree is None:
        return {"resolved": None if task.expect == "flag" else False}
    tree = export_tree(repo, state.head_tree, workdir / "delivered")
    score = await hidden_tests(task, tree, config.sandbox, workdir / "junit" / "hidden.xml")
    return {
        "resolved": score.ok,
        "hidden_passed": score.passed,
        "hidden_failed": list(score.failed),
        "hidden_error": score.error,
        "hidden_isolated": score.isolated,
    }


def _recorded_spend(files: RunFiles) -> dict[str, object]:
    """What an attempt that raised had spent, from its model calls' records. A floor: a call
    cut off mid-flight records nothing."""
    calls = files.records(INVOCATIONS)
    return {
        "spend_usd": sum(int(c.get("usd_micros", 0)) for c in calls) / 1_000_000,
        "input_tokens": sum(int(c.get("input_tokens", 0)) for c in calls),
        "output_tokens": sum(int(c.get("output_tokens", 0)) for c in calls),
        "cache_read_tokens": sum(int(c.get("cache_read_tokens", 0)) for c in calls),
    }


async def _drive(
    environment: live.LiveEnvironment,
    config: MsaConfig,
    run_id: RunId,
    client_factory: ClientFactory,
) -> RunState:
    state_dir = Path(environment.state_dir)
    client = client_factory(config, environment)
    with (
        SqliteJournal(state_dir / "runs.db") as journal,
        SqliteResponseStore(state_dir / "responses.db") as responses,
    ):
        interpreter = live.interpreter(
            environment,
            config,
            run_id=run_id,
            journal=journal,
            client=client,
            store=responses,
            clock=SystemClock(),
        )
        try:
            return await interpreter.start(
                run_id=run_id,
                task_digest=environment.task_digest,
                limits=config.limits.to_limits(),
                environment=environment.model_dump(mode="json"),
            )
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()


def findings(files: RunFiles) -> list[FindingRow]:
    """Every finding the Reviewer raised, with what finally settled it."""
    negotiated = {r["finding"]: r["verdict"] for r in files.records(ROUNDS) if r.get("verdict")}
    ruled = {r["finding"]: r for r in files.records(RULINGS) if r.get("verdict")}
    rows = []
    for record in files.records(FINDINGS):
        fid = str(record["id"])
        finding = record.get("finding") or {}
        if fid in ruled:
            verdict, rung = str(ruled[fid]["verdict"]), str(ruled[fid].get("rung"))
        elif fid in negotiated:
            verdict, rung = str(negotiated[fid]), "negotiation"
        else:
            verdict, rung = None, None
        rows.append(
            FindingRow(
                id=fid,
                severity=str(record.get("severity")),
                kind=str(finding.get("kind")),
                admitted=bool(record.get("admitted")),
                claim=str(finding.get("claim", "")),
                verdict=verdict,
                rung=rung,
            )
        )
    return rows


def _risks(files: RunFiles) -> list[str]:
    stored = files.read(PLAN_FILE)
    return list(Plan.model_validate_json(stored).risks) if stored else []


def _outcome(state: RunState) -> str:
    if state.outcome is not None:
        return f"{state.outcome.reason.value}: {state.outcome.detail}"
    return f"stopped in {state.phase.value}"


def read_results(path: Path) -> list[Result]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Result.model_validate_json(line) for line in lines if line.strip()]


async def run_all(
    tasks: Iterable[Task],
    arms: Iterable[str],
    settings: MsaConfig,
    out: Path,
    client_factory: ClientFactory,
    *,
    budget_usd: float,
    max_total_usd: float | None = None,
    repeats: int = 1,
    on_result: Callable[[Result], None] | None = None,
) -> list[Result]:
    """Every (task, arm, repeat) without a result in ``out/results.jsonl``, arms interleaved
    per task so a partial eval still compares the arms on the same tasks. One whose attempt
    errored is tried again, up to ``ATTEMPTS`` in all, counting attempts recorded before.
    Stops starting runs once ``max_total_usd`` has been spent, counting runs recorded
    before."""
    tasks, arms = list(tasks), list(arms)
    for arm in arms:
        arm_config(settings, arm, budget_usd)  # refuse an unbuilt arm before spending
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / RESULTS
    earlier = read_results(results_path)
    attempts = Counter((r.task, r.arm, r.repeat) for r in earlier)
    done = {(r.task, r.arm, r.repeat) for r in earlier if r.error is None}
    spent = sum(r.spend_usd for r in earlier)
    new: list[Result] = []
    for repeat in range(repeats):
        for task in tasks:
            for arm in arms:
                key = (task.id, arm, repeat)
                while key not in done and attempts[key] < ATTEMPTS:
                    if max_total_usd is not None and spent >= max_total_usd:
                        return new
                    name = f"{task.id}-{arm}-{repeat}"
                    if attempts[key]:  # a fresh directory; the errored attempt's stays
                        name += f"-attempt{attempts[key] + 1}"
                    result = await run_one(
                        task,
                        arm,
                        settings,
                        out / "runs" / name,
                        client_factory,
                        budget_usd=budget_usd,
                        repeat=repeat,
                    )
                    with results_path.open("a", encoding="utf-8") as sink:
                        sink.write(json.dumps(result.model_dump(mode="json")) + "\n")
                    attempts[key] += 1
                    spent += result.spend_usd
                    if result.error is None:
                        done.add(key)
                    new.append(result)
                    if on_result is not None:
                        on_result(result)
    return new

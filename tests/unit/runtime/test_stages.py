"""The AEGIS stages that need no model (ADR-015), on a real worktree of the greeter fixture:
admission, validation, context, integration tests, the security review, the release
verifier and publishing."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from msa.config import MsaConfig, Role
from msa.core import start
from msa.core.effects import (
    AdmitRun,
    Effect,
    LoadContext,
    PublishRelease,
    RunIntegrationTests,
    RunSecurityReview,
    ValidateRequest,
    VerifyRelease,
)
from msa.core.events import (
    ContextLoaded,
    IntegrationTestsFailed,
    IntegrationTestsPassed,
    ReleasePublished,
    ReleaseVerificationFailed,
    ReleaseVerified,
    RequestRejected,
    RunAdmitted,
    RunDenied,
    SecurityReviewFailed,
    SecurityReviewPassed,
)
from msa.core.ids import RunId, TreeHash
from msa.core.values import FailureKind, Owner
from msa.llm import fake
from msa.llm.gateway import Gateway
from msa.llm.store import MemoryResponseStore
from msa.runtime import integration, live, release, security
from msa.runtime.context import RunContext
from msa.runtime.intake import (
    AdmissionExecutor,
    ContextExecutor,
    ValidationExecutor,
    admission_problems,
    base_commit,
    secrets_in_task,
)
from msa.runtime.integration import IntegrationExecutor
from msa.runtime.journal import SqliteJournal
from msa.runtime.release import PublishExecutor, ReleaseExecutor, out_of_scope, title_of
from msa.runtime.security import (
    SecurityExecutor,
    added_lines,
    code_issues,
    is_manifest,
    owner_of,
)
from msa.tools.paths import Root
from msa.tools.sandbox import LocalSandbox
from tests.support.factory import T0, limits, run_started
from tests.support.llm import config as base_config
from tests.support.repos import git, make_repo

KEY = "AKIA" + "Q3EGX7PLMR2TBN5W"  # built at runtime: this file holds no key


def effect(command: object) -> Effect:
    return Effect.model_validate({"id": "run-1:9:0", "budget_usd_micros": None, "command": command})


# ---------- admission and validation


def test_admission_checks_the_ceilings_and_the_live_runs() -> None:
    policy = base_config().admission
    assert admission_problems(limits(), policy, live_runs=0) == []
    big = limits(budget_usd_micros=round(policy.max_budget_usd * 1_000_000) + 1)
    long = limits(wall_clock=timedelta(minutes=policy.max_wall_clock_minutes + 1))
    (budget,) = admission_problems(big, policy, live_runs=0)
    assert "over the admission limit" in budget
    assert "max_budget_usd" in budget
    (clock,) = admission_problems(long, policy, live_runs=0)
    assert "max_wall_clock_minutes" in clock
    (busy,) = admission_problems(limits(), policy, live_runs=policy.max_live_runs)
    assert f"{policy.max_live_runs} other run(s) of this repository are live" in busy


def test_a_denied_run_says_why_and_an_admitted_one_goes_on() -> None:
    policy = base_config().admission
    busy = AdmissionExecutor(policy, limits=limits, live_runs=lambda: policy.max_live_runs)
    denied = asyncio.run(busy.execute(effect(AdmitRun()), now=T0))
    assert isinstance(denied, RunDenied)
    assert "max_live_runs" in denied.reason
    free = AdmissionExecutor(policy, limits=limits, live_runs=lambda: 0)
    assert isinstance(asyncio.run(free.execute(effect(AdmitRun()), now=T0)), RunAdmitted)


def test_a_task_holding_a_secret_is_refused_without_repeating_it(tmp_path: Path) -> None:
    problem = secrets_in_task(f"use {KEY} to deploy")
    assert problem is not None
    assert KEY not in problem
    assert secrets_in_task("use the DEPLOY_KEY secret from the vault") is None
    repo = make_repo("greeter", tmp_path / "repo")
    sandbox = LocalSandbox(Root.at(repo), base_config().sandbox)
    validation = ValidationExecutor(repo, sandbox, task=f"deploy with {KEY}")
    answer = asyncio.run(validation.execute(effect(ValidateRequest()), now=T0))
    assert isinstance(answer, RequestRejected)
    assert KEY not in answer.reason
    empty = ValidationExecutor(repo, sandbox, task="  \n")
    answer = asyncio.run(empty.execute(effect(ValidateRequest()), now=T0))
    assert isinstance(answer, RequestRejected)
    assert answer.reason == "the task is empty"


def test_context_records_the_base_commit_and_the_repository_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    run = _context(tmp_path)
    sandbox = LocalSandbox(Root.at(run.worktree.repo), run.config.sandbox)
    loader = ContextExecutor(sandbox, run.files, run.run_id, (run.worktree,))
    answer = asyncio.run(loader.execute(effect(LoadContext()), now=T0))
    assert isinstance(answer, ContextLoaded)
    repo = run.worktree.repo
    assert answer.base_tree == git(repo, "rev-parse", "HEAD^{tree}")
    assert base_commit(run.files) == git(repo, "rev-parse", "HEAD")
    stored = json.loads(run.files.text(answer.context))
    assert "src/greeter.py" in stored["files"]
    assert run.worktree.exists


def test_live_runs_are_counted_per_repository(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "runs.db") as journal:
        for run_id, repo in (("a", "/r1"), ("b", "/r1"), ("c", "/r2")):
            first = run_started(run_id=run_id)
            environment = {"kind": "live", "repo": repo, "state_dir": "/s", "task": "t"}
            journal.create(environment, first, start(first).state)
        assert live.live_runs_of(journal, "/r1", besides=RunId("a")) == 1
        assert live.live_runs_of(journal, "/r2", besides=RunId("a")) == 1
        assert live.live_runs_of(journal, "/r3", besides=RunId("a")) == 0


# ---------- the security review's rules


def test_added_lines_carry_their_new_side_numbers() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1,2 +1,3 @@\n keep\n-old\n+new one\n+new two\n keep\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
        "diff --git a/y.py b/y.py\n--- /dev/null\n+++ b/y.py\n@@ -0,0 +7,1 @@\n+late\n"
    )
    assert added_lines(diff) == {"x.py": [(2, "new one"), (3, "new two")], "y.py": [(7, "late")]}


SOURCE = """import subprocess as sp
import os, pickle, yaml, hashlib, requests, tempfile
from yaml import SafeLoader

eval("1")
os.system("ls")
sp.run("ls", shell=True)
sp.run(["ls"])
pickle.loads(b"")
yaml.load("a")
yaml.load("a", Loader=SafeLoader)
requests.get("https://x", verify=False)
hashlib.md5(b"x")
hashlib.md5(b"x", usedforsecurity=False)
tempfile.mktemp()
os.chmod("f", 0o777)
"""


def test_insecure_calls_are_found_in_the_syntax_tree_on_added_lines_only() -> None:
    every = range(1, 20)
    found = {(i.line, i.rule, i.severity) for i in code_issues("a.py", SOURCE, every)}
    assert found == {
        (5, "code execution", "high"),
        (6, "shell command", "high"),
        (7, "shell command", "high"),  # through the `sp` alias
        (9, "unsafe deserialization", "high"),
        (10, "unsafe deserialization", "high"),
        (12, "TLS verification off", "high"),
        (13, "weak hash", "medium"),
        (15, "temp file race", "medium"),
        (16, "world-writable", "medium"),
    }
    assert code_issues("a.py", SOURCE, [8, 11, 14]) == []  # the safe forms
    assert [i.line for i in code_issues("a.py", SOURCE, [5])] == [5]
    assert code_issues("a.py", "def broken(:\n", [1]) == []  # syntax is not this gate's


def test_findings_go_to_whoever_can_fix_all_of_them() -> None:
    tester = ["tests/acceptance", "tests/repro"]
    mine = security.Issue("tests/acceptance/t.py", 1, "critical", "secret", "x")
    theirs = security.Issue("src/a.py", 1, "high", "code execution", "x")
    advisory = security.Issue("src/b.py", 1, "medium", "weak hash", "x")
    assert owner_of([mine], tester) is Owner.TESTER
    assert owner_of([mine, advisory], tester) is Owner.TESTER  # advisories need no fix
    assert owner_of([mine, theirs], tester) is Owner.CODER
    assert owner_of([], tester) is Owner.CODER


def test_dependency_manifests_are_recognized_by_name() -> None:
    for path in ("pyproject.toml", "sub/requirements-dev.txt", "uv.lock", "web/package.json"):
        assert is_manifest(path), path
    assert not is_manifest("src/requirements.py")


# ---------- the stages on a real worktree


def _context(tmp_path: Path, **config: Any) -> RunContext:
    repo = make_repo("greeter", tmp_path / "repo")
    base = base_config()
    settings: MsaConfig = base.model_copy(
        update={
            "sandbox": base.sandbox.model_copy(update={"launcher": (sys.executable, "-m")}),
            **config,
        }
    )
    environment = live.LiveEnvironment(repo=str(repo), state_dir=str(tmp_path / "s"), task="t")
    validation = ValidationExecutor(repo, LocalSandbox(Root.at(repo), settings.sandbox), task="t")
    context = live.context(
        environment,
        settings,
        Gateway(fake.ScriptedClient(), settings, store=MemoryResponseStore()),
        run_id=RunId("run-1"),
        validation=validation,
    )
    context.task = "Add farewell(name) to the greeter.\n\nIt says goodbye."
    asyncio.run(context.worktree.ensure(TreeHash(git(repo, "rev-parse", "HEAD^{tree}"))))
    return context


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunContext:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    return _context(tmp_path)


def change(run: RunContext, files: dict[str, str]) -> TreeHash:
    for path, text in files.items():
        target = run.worktree.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return asyncio.run(run.worktree.snapshot())


def test_a_clean_change_passes_the_security_review(run: RunContext) -> None:
    tree = change(run, {"src/greeter.py": "def farewell(name: str) -> str:\n    return name\n"})
    answer = asyncio.run(
        SecurityExecutor(run).execute(effect(RunSecurityReview(tree=tree)), now=T0)
    )
    assert isinstance(answer, SecurityReviewPassed)
    report = run.files.text(answer.report)
    assert "No secrets and no insecure calls in the added lines." in report


def test_a_secret_and_an_eval_fail_the_security_review_without_leaking(run: RunContext) -> None:
    tree = change(
        run,
        {"src/settings.py": f'API_KEY = "{KEY}"\n', "src/run.py": "def go(s):\n    eval(s)\n"},
    )
    answer = asyncio.run(
        SecurityExecutor(run).execute(effect(RunSecurityReview(tree=tree)), now=T0)
    )
    assert isinstance(answer, SecurityReviewFailed)
    assert answer.owner is Owner.CODER
    report = run.files.text(answer.report)
    assert KEY not in report
    assert "`src/settings.py:1` secret: aws access key id" in report
    assert "`src/run.py:2` code execution" in report
    (record,) = run.files.records(security.RECORDS)
    assert (record["verdict"], record["blocking"], record["owner"]) == ("failed", 2, "coder")


def test_a_secret_in_the_testers_files_goes_back_to_the_tester(run: RunContext) -> None:
    tree = change(run, {"tests/acceptance/test_x.py": f'TOKEN = "{KEY}"\n'})
    answer = asyncio.run(
        SecurityExecutor(run).execute(effect(RunSecurityReview(tree=tree)), now=T0)
    )
    assert isinstance(answer, SecurityReviewFailed)
    assert answer.owner is Owner.TESTER


def test_dependency_changes_are_listed_and_can_be_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    strict = base_config().security.model_copy(update={"forbid_new_dependencies": True})
    for policy, expected in ((None, SecurityReviewPassed), (strict, SecurityReviewFailed)):
        ctx = _context(
            tmp_path / str(expected.__name__), **({"security": policy} if policy else {})
        )
        pyproject = (ctx.worktree.path / "pyproject.toml").read_text(encoding="utf-8")
        tree = change(ctx, {"pyproject.toml": pyproject + "\n# a new dependency\n"})
        answer = asyncio.run(
            SecurityExecutor(ctx).execute(effect(RunSecurityReview(tree=tree)), now=T0)
        )
        assert isinstance(answer, expected)
        report = ctx.files.text(answer.report)
        assert "## Dependency manifests changed" in report
        assert "- `pyproject.toml`" in report
        assert "No dependency audit is configured" in report


INTEGRATION_TEST = (
    "from greeter import greet\n\n\ndef test_greet() -> None:\n    assert greet('a')\n"
)


def _integrate(run: RunContext, tree: TreeHash) -> object:
    command = RunIntegrationTests(tree=tree)
    return asyncio.run(IntegrationExecutor(run).execute(effect(command), now=T0))


def test_no_integration_suite_passes_with_nothing_run_and_says_so(run: RunContext) -> None:
    tree = change(run, {"src/extra.py": "X = 1\n"})
    answer = _integrate(run, tree)
    assert isinstance(answer, IntegrationTestsPassed)
    (record,) = run.files.records(integration.RECORDS)
    assert (record["verdict"], record["ran"]) == ("passed", False)
    assert "Nothing ran, so this stage is no evidence either way." in run.files.text(
        record["report"]
    )


def test_a_passing_suite_runs_without_network_where_the_kernel_allows(run: RunContext) -> None:
    tree = change(run, {"tests/integration/test_flow.py": INTEGRATION_TEST})
    answer = _integrate(run, tree)
    assert isinstance(answer, IntegrationTestsPassed)
    (record,) = run.files.records(integration.RECORDS)
    sandbox = run.sandbox(Role.CODER)
    expected = asyncio.run(integration.isolation(sandbox)) is not None
    assert (record["ran"], record["isolated"]) == (True, expected)


@pytest.mark.skipif(sys.platform != "linux", reason="network namespaces are Linux's")
def test_an_isolated_suite_cannot_reach_the_network(run: RunContext) -> None:
    if asyncio.run(integration.isolation(run.sandbox(Role.CODER))) is None:
        pytest.skip("this kernel does not let an unprivileged user create a network namespace")
    probe = (
        "import socket\n\n\ndef test_reaches_out() -> None:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=2).close()\n"
    )
    tree = change(run, {"tests/integration/test_net.py": probe})
    answer = _integrate(run, tree)
    assert isinstance(answer, IntegrationTestsFailed)
    assert "Network is unreachable" in run.files.text(answer.report)


def test_a_suite_the_change_broke_goes_to_the_coder(run: RunContext) -> None:
    broken = INTEGRATION_TEST.replace("assert greet('a')", "assert greet('a') == 'nope'")
    tree = change(run, {"tests/integration/test_flow.py": broken})
    answer = _integrate(run, tree)
    assert isinstance(answer, IntegrationTestsFailed)
    assert (answer.triage.kind, answer.triage.owner) == (FailureKind.IMPL_BUG, Owner.CODER)
    assert "Fail on the head tree only: the change broke them" in run.files.text(answer.report)


def test_a_suite_broken_before_the_run_blocks_for_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    repo = make_repo("greeter", tmp_path / "pre" / "repo")
    broken = INTEGRATION_TEST.replace("assert greet('a')", "assert False")
    (repo / "tests" / "integration").mkdir(parents=True)
    (repo / "tests" / "integration" / "test_flow.py").write_text(broken, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "a broken suite")
    ctx = _context(tmp_path / "unused")
    ctx.worktree = ctx.worktree.for_run(repo, tmp_path / "pre" / "s", "run-1")
    ctx.base_worktree = ctx.worktree.for_run(repo, tmp_path / "pre" / "s", "run-1", "base")
    asyncio.run(ctx.worktree.ensure(TreeHash(git(repo, "rev-parse", "HEAD^{tree}"))))
    tree = change(ctx, {"src/extra.py": "X = 1\n"})
    answer = _integrate(ctx, tree)
    assert isinstance(answer, IntegrationTestsFailed)
    assert (answer.triage.kind, answer.triage.owner) == (FailureKind.ENVIRONMENT, None)
    assert "Fail on the base tree too: broken before this run" in ctx.files.text(answer.report)


def test_isolation_required_but_unavailable_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    always = base_config().integration.model_copy(update={"isolate_network": "always"})
    ctx = _context(tmp_path, integration=always)

    async def unavailable(_: LocalSandbox) -> None:
        return None

    monkeypatch.setattr(integration, "isolation", unavailable)
    tree = change(ctx, {"tests/integration/test_flow.py": INTEGRATION_TEST})
    answer = _integrate(ctx, tree)
    assert isinstance(answer, IntegrationTestsFailed)
    assert answer.triage.owner is None
    assert "isolate_network = always" in ctx.files.text(answer.report)


# ---------- the release verifier and publishing


def test_titles_come_from_the_first_line_of_the_task() -> None:
    assert title_of("# Add farewell\n\nmore") == "Add farewell"
    assert title_of("\n\n  ") == "Change"
    long = title_of("x" * 100)
    assert len(long) == release.TITLE_LIMIT
    assert long.endswith("…")


def test_scope_allows_what_the_coder_or_tester_may_write() -> None:
    capabilities = base_config().capabilities
    assert out_of_scope(["src/a.py", "tests/acceptance/t.py"], capabilities) == []
    assert out_of_scope([".msa/runs.db", ".git/config"], capabilities) == [
        ".msa/runs.db",
        ".git/config",
    ]


def _passed_gates(run: RunContext, tree: TreeHash, **integration_record: object) -> None:
    for records, entry in (
        ("verifications.jsonl", {"passed": True}),
        ("tests.jsonl", {"verdict": "passed"}),
        (integration.RECORDS, {"verdict": "passed", "ran": True, **integration_record}),
        (security.RECORDS, {"verdict": "passed", "advisory": 0}),
    ):
        run.files.record(records, {"tree": tree, "report": "sha256:" + "0" * 64, **entry})


def _with_context(run: RunContext) -> None:
    sandbox = LocalSandbox(Root.at(run.worktree.repo), run.config.sandbox)
    loader = ContextExecutor(sandbox, run.files, run.run_id, ())
    asyncio.run(loader.execute(effect(LoadContext()), now=T0))


def _verify(run: RunContext, tree: TreeHash) -> object:
    return asyncio.run(ReleaseExecutor(run).execute(effect(VerifyRelease(tree=tree)), now=T0))


def test_an_empty_change_and_missing_evidence_are_not_released(run: RunContext) -> None:
    _with_context(run)
    tree = asyncio.run(run.worktree.snapshot())
    answer = _verify(run, tree)
    assert isinstance(answer, ReleaseVerificationFailed)
    report = run.files.text(answer.report)
    assert "The change is empty" in report
    for gate in ("Static analysis", "Unit tests", "Integration tests", "Security review"):
        assert f"Missing evidence. {gate}: no passing record for tree {tree}." in report


def test_a_secret_in_the_patch_is_never_released(run: RunContext) -> None:
    _with_context(run)
    tree = change(run, {"src/settings.py": f'KEY = "{KEY}"\n'})
    _passed_gates(run, tree)
    answer = _verify(run, tree)
    assert isinstance(answer, ReleaseVerificationFailed)
    report = run.files.text(answer.report)
    assert "The patch holds secrets" in report
    assert KEY not in report


def test_a_verified_release_writes_the_pr_and_publishes_once_approved(run: RunContext) -> None:
    _with_context(run)
    tree = change(run, {"src/farewell.py": "def farewell(n: str) -> str:\n    return n\n"})
    _passed_gates(run, tree, isolated=True)
    answer = _verify(run, tree)
    assert isinstance(answer, ReleaseVerified)
    bundle = json.loads(run.files.get(answer.bundle))
    assert (bundle["title"], bundle["tree"], bundle["changed"]) == (
        "Add farewell(name) to the greeter.",
        tree,
        ["src/farewell.py"],
    )
    pr = (run.files.directory / release.PR_FILE).read_text(encoding="utf-8")
    assert pr.startswith("# Add farewell(name) to the greeter.\n")
    assert "> It says goodbye." in pr
    assert "| Integration tests | passed, without network |" in pr
    assert f"`msa approve {run.run_id}`" in pr

    publish = effect(PublishRelease(tree=tree, bundle=answer.bundle, approved_by="grace"))
    first = asyncio.run(PublishExecutor(run).execute(publish, now=T0))
    again = asyncio.run(PublishExecutor(run).execute(publish, now=T0))  # after a crash
    assert isinstance(first, ReleasePublished)
    assert isinstance(again, ReleasePublished)
    assert first.reference == again.reference  # the same commit, reused
    repo = run.worktree.repo
    commit = git(repo, "rev-parse", "refs/heads/aegis/run-1")
    assert first.reference == f"aegis/run-1 at {commit}"
    assert git(repo, "show", "-s", "--format=%T %P", commit).split() == [
        tree,
        git(repo, "rev-parse", "HEAD"),
    ]
    message = git(repo, "show", "-s", "--format=%B%n%an", commit)
    assert "Approved-by: grace" in message
    assert message.rstrip().endswith("AEGIS")
    assert git(repo, "remote") == ""  # nothing to push to, and nothing pushed


def test_publishing_never_moves_a_branch_that_holds_something_else(run: RunContext) -> None:
    _with_context(run)
    tree = change(run, {"src/farewell.py": "X = 1\n"})
    _passed_gates(run, tree)
    answer = _verify(run, tree)
    assert isinstance(answer, ReleaseVerified)
    repo = run.worktree.repo
    git(repo, "branch", "aegis/run-1", "HEAD")  # someone else's work under the same name
    publish = effect(PublishRelease(tree=tree, bundle=answer.bundle, approved_by="grace"))
    with pytest.raises(RuntimeError, match="already exists and holds another change"):
        asyncio.run(PublishExecutor(run).execute(publish, now=T0))
    assert git(repo, "rev-parse", "aegis/run-1") == git(repo, "rev-parse", "HEAD")


# ---------- paths that block or refuse, rather than pass


FLAKY = """from pathlib import Path


def test_sometimes() -> None:
    marker = Path(__file__).with_name("ran-once")
    if not marker.exists():
        marker.write_text("x")
        raise AssertionError("first run fails")
"""


def test_a_flaky_integration_test_blocks_and_is_never_blamed_on_the_change(
    run: RunContext,
) -> None:
    (run.worktree.path / ".gitignore").write_text("ran-once\n")
    tree = change(run, {"tests/integration/test_flaky.py": FLAKY})
    answer = _integrate(run, tree)
    assert isinstance(answer, IntegrationTestsFailed)
    assert (answer.triage.kind, answer.triage.owner) == (FailureKind.ENVIRONMENT, None)
    report = run.files.text(answer.report)
    assert "## Flaky: mixed results when rerun alone" in report
    assert "tests/integration/test_flaky.py::test_sometimes" in report


def test_an_integration_suite_that_cannot_run_blocks(run: RunContext) -> None:
    broken = "raise RuntimeError('the fixture database is gone')\n"
    tree = change(
        run,
        {"tests/integration/conftest.py": broken, "tests/integration/test_a.py": INTEGRATION_TEST},
    )
    answer = _integrate(run, tree)
    assert isinstance(answer, IntegrationTestsFailed)
    assert answer.triage.owner is None
    assert "cannot run" in run.files.text(answer.report)


@pytest.mark.parametrize(("command", "expected"), [("true", True), ("false", False)])
def test_a_dependency_audit_gates_a_manifest_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, expected: bool
) -> None:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    audited = base_config().security.model_copy(update={"audit_command": (command,)})
    ctx = _context(tmp_path, security=audited)
    pyproject = (ctx.worktree.path / "pyproject.toml").read_text(encoding="utf-8")
    tree = change(ctx, {"pyproject.toml": pyproject + "\n# a new dependency\n"})
    answer = asyncio.run(
        SecurityExecutor(ctx).execute(effect(RunSecurityReview(tree=tree)), now=T0)
    )
    assert isinstance(answer, SecurityReviewPassed if expected else SecurityReviewFailed)
    report = ctx.files.text(answer.report)
    assert "## Dependency audit" in report
    if not expected:
        assert "dependency audit: `false` failed" in report


def test_a_path_no_role_may_write_is_never_released(run: RunContext) -> None:
    _with_context(run)
    tree = change(run, {".msa/notes.txt": "who put this here\n", "src/ok.py": "X = 1\n"})
    _passed_gates(run, tree)
    answer = _verify(run, tree)
    assert isinstance(answer, ReleaseVerificationFailed)
    assert "Paths no role may write were changed: `.msa/notes.txt`." in run.files.text(
        answer.report
    )

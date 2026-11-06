"""Sandboxed commands: allowlist, scrubbed environment, limits, search and git."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from msa.tools.errors import CommandRejected, CommandUnavailable, PathRejected
from msa.tools.paths import Root
from msa.tools.sandbox import DEAD_PROXY, Completed, LocalSandbox, SearchResult, _rg_match
from tests.support.tools import Tree, build_tree, sandbox_config

PY = sys.executable


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return build_tree(tmp_path)


def sandbox(tree: Tree, **overrides: Any) -> LocalSandbox:
    overrides.setdefault("launcher", (PY, "-m"))
    return LocalSandbox(Root.at(tree.repo), sandbox_config(**overrides))


def python(box: LocalSandbox, code: str, **kwargs: Any) -> Completed:
    return asyncio.run(box.execute([PY, "-c", code], shown="python", **kwargs))


# ---------- execution


def test_the_environment_is_scrubbed_to_the_passthrough_list(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
    monkeypatch.setenv("LANG", "C.UTF-8")
    done = python(sandbox(tree), "import json, os; print(json.dumps(dict(os.environ)))")
    env = json.loads(done.stdout)
    assert "ANTHROPIC_API_KEY" not in env
    assert env["LANG"] == "C.UTF-8"
    assert env["HTTPS_PROXY"] == env["http_proxy"] == DEAD_PROXY
    assert env["UV_OFFLINE"] == "1"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    allowed = set(sandbox_config().env_passthrough)
    fixed = {"PYTHONDONTWRITEBYTECODE", "NO_COLOR", "UV_OFFLINE", "PIP_NO_INDEX", "NO_PROXY"}
    git = {k for k in env if k.startswith("GIT_")}
    proxies = {k for k in env if k.lower().endswith("_proxy")}
    assert set(env) - allowed - fixed - git - proxies - {"no_proxy", "LC_CTYPE"} == set()


def test_a_command_that_needs_the_network_keeps_the_real_proxies(tree: Tree) -> None:
    done = python(sandbox(tree), "import os; print(os.environ.get('HTTPS_PROXY'))", network=True)
    assert DEAD_PROXY not in done.stdout


def test_a_timeout_kills_the_whole_process_group(tree: Tree) -> None:
    pidfile = tree.base / "grandchild.pid"
    code = (
        "import subprocess, time\n"
        f"p = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    done = python(sandbox(tree, timeout_seconds=1.0), code)
    assert done.timed_out
    assert done.render().startswith("$ python\ntimed out after 1s and was killed")
    grandchild = int(pidfile.read_text())
    assert not _running(grandchild)


def test_output_keeps_head_and_tail_and_counts_what_it_drops(tree: Tree) -> None:
    done = python(sandbox(tree, output_limit_bytes=1000), "print('A' * 100000 + 'TAIL')")
    assert done.stdout.startswith("A" * 500)
    assert done.stdout.rstrip().endswith("TAIL")
    assert done.elided_bytes == 100005 - 1000
    assert f"[{done.elided_bytes} bytes of output elided from the middle]" in done.render()


def test_stdout_and_stderr_can_be_kept_apart(tree: Tree) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    done = python(sandbox(tree), code, merge=False)
    assert (done.exit_code, done.stdout, done.stderr) == (3, "out\n", "err\n")
    assert done.render() == f"$ python\nexit 3 after {done.seconds:.1f}s\nout\nerr"


def test_the_file_size_limit_applies(tree: Tree) -> None:
    code = "with open('big.bin', 'wb') as f:\n    f.write(b'x' * 5000)"
    done = python(sandbox(tree, max_file_bytes=1000), code)
    assert done.exit_code == 1
    assert "File too large" in done.stdout  # Python ignores SIGXFSZ, so the write fails
    assert (tree.repo / "big.bin").stat().st_size <= 1000


def test_the_cpu_limit_kills_a_spinning_command(tree: Tree) -> None:
    done = python(sandbox(tree, cpu_seconds=1, timeout_seconds=30), "while True: pass")
    assert done.exit_code is not None
    assert done.exit_code < 0  # SIGXCPU, then SIGKILL at the hard limit
    assert done.render().splitlines()[1].startswith("killed by signal")


def test_a_program_missing_from_path_is_reported(tree: Tree) -> None:
    with pytest.raises(CommandUnavailable, match="no-such-tool is not installed"):
        asyncio.run(sandbox(tree).execute(["no-such-tool"], shown="x"))


# ---------- run


def test_only_allowlisted_commands_run(tree: Tree) -> None:
    box = sandbox(tree)
    with pytest.raises(CommandRejected, match="'rm' is not an allowed command"):
        asyncio.run(box.run("rm", ["-rf", "."]))  # type: ignore[arg-type]
    with pytest.raises(CommandRejected, match="keyword applies to pytest only"):
        asyncio.run(box.run("mypy", [], keyword="x"))
    with pytest.raises(CommandRejected, match="keyword must be 1-1000 characters"):
        asyncio.run(box.run("pytest", [], keyword=""))
    with pytest.raises(PathRejected, match="resolves outside"):
        asyncio.run(box.run("pytest", ["../outside"]))


def test_pytest_runs_through_the_launcher_with_confined_paths(tree: Tree) -> None:
    tests = tree.repo / "tests" / "unit"
    tests.mkdir()
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (tests / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    (tree.repo / "-k").write_text("")  # a file named like an option stays a path
    box = sandbox(tree)
    passed = asyncio.run(box.run("pytest", ["tests/unit/test_ok.py"]))
    assert passed.exit_code == 0, passed.render()
    assert passed.command == "pytest -q ./tests/unit/test_ok.py"
    failed = asyncio.run(box.run("pytest", ["tests/unit"], keyword="bad"))
    assert failed.exit_code == 1
    assert failed.command == "pytest -q -kbad ./tests/unit"
    assert "1 failed" in failed.stdout
    assert asyncio.run(box.run("pytest", ["."], keyword="nothing")).command.endswith(" .")


# ---------- search


needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")


@needs_rg
def test_search_finds_matches_without_following_symlinks_out(tree: Tree) -> None:
    box = sandbox(tree)
    found = asyncio.run(box.search(r"def \w+"))
    assert [(m.path, m.line, m.text) for m in found.matches] == [
        ("src/app.py", 1, "def greet(name):"),
        ("tests/acceptance/test_login.py", 1, "def test_login():"),
    ]
    secret = asyncio.run(box.search("secret outside"))
    assert secret.matches == ()  # escape/ and secret_link point outside: never searched
    assert secret.render() == "no matches"
    only_src = asyncio.run(box.search("def", glob="src/**"))
    assert [m.path for m in only_src.matches] == ["src/app.py"]


@needs_rg
def test_search_stops_at_the_match_limit(tree: Tree) -> None:
    (tree.repo / "many.txt").write_text("hit\n" * 50)
    found = asyncio.run(sandbox(tree, search_limit_matches=5).search("hit"))
    assert len(found.matches) == 5
    assert found.more
    assert found.render().endswith("[stopped after 5 matches; narrow the search]")


@needs_rg
def test_a_bad_pattern_is_a_tool_error(tree: Tree) -> None:
    with pytest.raises(CommandRejected, match=r"search failed: .*regex parse error"):
        asyncio.run(sandbox(tree).search("(unclosed"))
    with pytest.raises(CommandRejected, match="pattern must be"):
        asyncio.run(sandbox(tree).search(""))


def test_search_without_ripgrep_says_so(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tree.base / "empty-bin"))
    with pytest.raises(CommandUnavailable, match="rg is not installed"):
        asyncio.run(sandbox(tree).search("x"))


def test_unusable_ripgrep_lines_are_skipped() -> None:
    assert _rg_match('{"type": "match", "data": {"path"') is None  # cut by the output cap
    assert _rg_match('{"type": "begin", "data": {}}') is None
    undecodable = {"type": "match", "data": {"path": {"bytes": "gA=="}, "lines": {"text": "x"}}}
    assert _rg_match(json.dumps(undecodable)) is None
    assert SearchResult((), more=False).render() == "no matches"


# ---------- git_diff


def git(repo: Path, *args: str) -> None:
    env = {"PATH": os.environ["PATH"], "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tree: Tree) -> Tree:
    git(tree.repo, "init", "-q", "-b", "main")
    git(tree.repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "src", "tests")
    git(tree.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return tree


def test_git_diff_shows_changes_and_untracked_files(repo: Tree) -> None:
    (repo.repo / "src" / "app.py").write_text("changed\n")
    (repo.repo / "src" / "new.py").write_text("new\n")
    done = asyncio.run(sandbox(repo).git_diff())
    assert done.exit_code == 0
    assert "-def greet(name):" in done.stdout
    assert "+changed" in done.stdout
    assert done.stdout.endswith(
        "untracked files:\ndocs\nescape\nowned_link\nsecret_link\nsrc/new.py"
    )


def test_git_diff_with_no_changes_says_so(repo: Tree) -> None:
    for link in ("docs", "escape", "owned_link", "secret_link"):
        (repo.repo / link).unlink()
    assert asyncio.run(sandbox(repo).git_diff("HEAD")).stdout == "no changes to tracked files"


def test_repository_config_cannot_run_programs_during_a_diff(repo: Tree) -> None:
    pwned = repo.base / "pwned"
    hostile = f"touch {pwned}"
    git(repo.repo, "config", "core.fsmonitor", hostile)
    git(repo.repo, "config", "diff.external", hostile)
    git(repo.repo, "config", "diff.evil.textconv", hostile)
    (repo.repo / ".gitattributes").write_text("*.py diff=evil\n")
    (repo.repo / "src" / "app.py").write_text("changed\n")
    done = asyncio.run(sandbox(repo).git_diff())
    assert "+changed" in done.stdout
    assert not pwned.exists()


@pytest.mark.parametrize("base", ["--output=/tmp/x", "HEAD..main", "a b", "", "x" * 300])
def test_git_diff_rejects_revisions_that_could_be_options(repo: Tree, base: str) -> None:
    with pytest.raises(CommandRejected, match="is not a revision this tool accepts"):
        asyncio.run(sandbox(repo).git_diff(base))


def test_a_failing_git_diff_is_a_tool_error(tree: Tree) -> None:
    with pytest.raises(CommandRejected, match="git diff failed"):
        asyncio.run(sandbox(tree).git_diff("HEAD"))  # not a git repository


def _running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0]
    except FileNotFoundError:
        return False
    return state != "Z"

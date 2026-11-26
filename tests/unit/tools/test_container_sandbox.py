"""The container sandbox (ADR-019): repository commands in a locked-down container.

The first tests need no container runtime: they check the exact arguments the runtime is
given, and drive the sandbox with a fake ``docker`` that records how it was called. The
last ones run real containers, and skip cleanly where Docker cannot run them (CI sets
``MSA_REQUIRE_CONTAINER=1``, so there they must run).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from msa.config import ContainerConfig, SandboxConfig
from msa.runtime import live
from msa.runtime.integration import isolation
from msa.tools.errors import CommandUnavailable
from msa.tools.paths import Root
from msa.tools.sandbox import ContainerSandbox, LocalSandbox, container_problem, make_sandbox
from tests.support.containers import IMAGE, needs_containers
from tests.support.llm import config as msa_config


def config(**container: object) -> SandboxConfig:
    settings = ContainerConfig.model_validate({"image": IMAGE, **container})
    return SandboxConfig(backend="container", container=settings)


@pytest.fixture
def root(tmp_path: Path) -> Root:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return Root.at(worktree)


# ---------- configuration and the factory


def test_the_local_backend_is_the_default_and_container_needs_an_image() -> None:
    assert SandboxConfig().backend == "local"
    with pytest.raises(ValidationError, match=r"needs \[sandbox\.container\]\.image"):
        SandboxConfig(backend="container")


def test_the_factory_builds_the_backend_the_config_names(root: Root) -> None:
    assert type(make_sandbox(root, SandboxConfig())) is LocalSandbox
    box = make_sandbox(root, config(launcher=("python3", "-m")))
    assert isinstance(box, ContainerSandbox)
    assert box.config.launcher == ("python3", "-m")  # callers build commands for the image


def test_a_container_needs_no_network_wrapper_it_has_none(root: Root) -> None:
    box = ContainerSandbox(root, config())
    assert asyncio.run(isolation(box)) == ()  # isolated, and nothing to prefix


# ---------- the arguments the runtime is given


def values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == flag]


def test_a_command_runs_locked_down(root: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(os, "getgid", lambda: 1000)
    box = ContainerSandbox(root, config(cpus=1.5, memory="2g", pids_limit=64, tmp_size="64m"))
    args = box.container_args(["python", "-m", "pytest", "-q"], "msa-test")
    where = str(root.path)
    assert args[:4] == ["run", "--rm", "--name", "msa-test"]
    for flag, value in [
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--user", "1000:1000"),
        ("--cpus", "1.5"),
        ("--memory", "2g"),
        ("--memory-swap", "2g"),
        ("--pids-limit", "64"),
        ("--tmpfs", "/tmp:rw,nosuid,nodev,size=64m"),
        ("--mount", f"type=bind,src={where},dst={where}"),
        ("--workdir", where),
        ("--env", "HOME=/tmp"),
    ]:
        assert value in values(args, flag), (flag, value, values(args, flag))
    for flag in ("--network", "--user", "--cpus", "--memory", "--workdir"):
        assert len(values(args, flag)) == 1, flag  # set once, so nothing overrides it
    assert "--read-only" in args
    assert not any(a.startswith("PATH=") for a in args)  # the image's PATH, not this machine's
    assert args[-5:] == [IMAGE, "python", "-m", "pytest", "-q"]


def test_as_root_the_command_runs_as_nobody(root: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0)
    args = ContainerSandbox(root, config()).container_args(["true"], "n")
    assert args[args.index("--user") + 1] == "65534:65534"


def test_a_configured_user_is_used_and_podman_keeps_your_id(root: Root) -> None:
    args = ContainerSandbox(root, config(user="2000:2000")).container_args(["true"], "n")
    assert args[args.index("--user") + 1] == "2000:2000"
    assert "--userns" not in args
    podman = ContainerSandbox(root, config(runtime="podman")).container_args(["true"], "n")
    assert podman[podman.index("--userns") + 1] == "keep-id"


def test_only_the_harness_output_directories_outside_the_worktree_are_mounted(
    root: Root, tmp_path: Path
) -> None:
    junit = tmp_path / "run" / "junit" / "head.xml"
    inside = root.path / "reports" / "x.xml"
    box = ContainerSandbox(root, config())
    argv = ["pytest", f"--junitxml={junit}", str(inside), "./tests", "--", "tests/a.py"]
    mounts = [a for a in box.container_args(argv, "n") if a.startswith("type=bind")]
    assert mounts == [
        f"type=bind,src={root.path},dst={root.path}",
        f"type=bind,src={junit.parent},dst={junit.parent}",
    ]


def test_a_path_that_cannot_be_mounted_is_refused(tmp_path: Path) -> None:
    odd = tmp_path / "a,b"
    odd.mkdir()
    with pytest.raises(CommandUnavailable, match="cannot be mounted"):
        ContainerSandbox(Root.at(odd), config()).container_args(["true"], "n")


# ---------- failing closed


def fake_runtime(directory: Path, *, info: int = 0, run_seconds: float = 0) -> Path:
    """A ``docker`` that records each call, answers ``info`` with exit ``info``, and
    'runs' a container by sleeping ``run_seconds``."""
    directory.mkdir(exist_ok=True)
    log = directory / "calls.jsonl"
    script = directory / "docker"
    script.write_text(
        f"""#!{sys.executable}
import json, sys, time
with open({str(log)!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "info":
    print("Cannot connect to the Docker daemon", file=sys.stderr)
    sys.exit({info})
if sys.argv[1] == "run":
    time.sleep({run_seconds})
    print("ran in the container")
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_the_local_backend_never_needs_a_runtime() -> None:
    assert container_problem(SandboxConfig()) is None


def test_no_runtime_installed_is_a_clear_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    problem = container_problem(config())
    assert problem == (
        '[sandbox].backend = "container", but docker is not installed: install it, or set '
        'backend = "local" for a repository you trust'
    )


def test_a_runtime_that_cannot_run_containers_is_a_clear_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_runtime(tmp_path / "bin", info=1)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    problem = container_problem(config())
    assert problem is not None
    assert problem.startswith(
        "docker is installed but cannot run containers (Cannot connect to the Docker daemon)"
    )


def test_a_command_with_no_runtime_does_not_run_at_all(
    root: Root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(CommandUnavailable, match="docker is not installed"):
        asyncio.run(ContainerSandbox(root, config()).run("pytest"))


def test_a_live_run_refuses_to_start_without_a_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    settings = msa_config().model_copy(update={"sandbox": config()})
    environment = live.LiveEnvironment(repo=str(tmp_path), state_dir=str(tmp_path), task="x")
    with pytest.raises(CommandUnavailable, match="docker is not installed"):
        live.interpreter(
            environment,
            settings,
            run_id="run-1",  # type: ignore[arg-type]
            journal=None,  # type: ignore[arg-type]
            client=None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
            clock=None,  # type: ignore[arg-type]
        )


# ---------- running, with a fake runtime


def test_a_command_runs_through_the_runtime_and_its_container_is_removed(
    root: Root, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = fake_runtime(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    done = asyncio.run(ContainerSandbox(root, config()).run("pytest", ["tests"]))
    assert done.exit_code == 0
    assert done.stdout.strip() == "ran in the container"
    assert done.command == "pytest -q ./tests"  # no host network wrapper: it has none
    started, removed = calls(log)
    assert started[0] == "run"
    assert started[-6:] == [IMAGE, "python", "-m", "pytest", "-q", "./tests"]
    name = started[started.index("--name") + 1]
    assert removed == ["rm", "--force", name]


def test_a_command_that_times_out_has_its_container_removed(
    root: Root, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = fake_runtime(tmp_path / "bin", run_seconds=30)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    box = ContainerSandbox(root, config().model_copy(update={"timeout_seconds": 1.0}))
    done = asyncio.run(box.execute(["python", "-m", "pytest"], shown="pytest"))
    assert done.timed_out
    started, removed = calls(log)
    assert removed == ["rm", "--force", started[started.index("--name") + 1]]


def test_a_harness_tool_that_needs_the_network_runs_on_this_machine(
    root: Root, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = fake_runtime(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    done = asyncio.run(
        ContainerSandbox(root, config()).execute(["echo", "audit"], shown="echo", network=True)
    )
    assert done.stdout.strip() == "audit"
    assert not log.exists()  # the runtime was never called


# ---------- running, in real containers


def in_container(root: Root, code: str, **settings: object) -> str:
    box = ContainerSandbox(root, config(**settings))
    done = asyncio.run(box.execute(["python", "-c", code], shown="python -c"))
    return done.render()


def test_a_container_can_write_the_worktree_and_nothing_else(root: Root) -> None:
    needs_containers()
    out = in_container(
        root,
        "import os\n"
        "open('made-inside.txt', 'w').write('x')\n"
        "for path in ('/etc/x', '/usr/x', '/x'):\n"
        "    try:\n"
        "        open(path, 'w')\n"
        "        print('WROTE', path)\n"
        "    except OSError as e:\n"
        "        print('refused', path, e.errno)\n"
        "print('uid', os.getuid())\n",
    )
    assert "WROTE" not in out, out
    assert out.count("refused") == 3, out
    made = root.path / "made-inside.txt"
    assert made.read_text() == "x"
    if os.getuid() != 0:
        assert made.stat().st_uid == os.getuid()  # yours, not root's
    assert "uid 0" not in out


def test_a_container_has_no_network(root: Root) -> None:
    needs_containers()
    out = in_container(
        root,
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('no network', type(e).__name__)\n",
    )
    assert "CONNECTED" not in out, out
    assert "no network" in out, out


def test_a_harness_report_outside_the_worktree_can_be_written(root: Root, tmp_path: Path) -> None:
    needs_containers()
    report = tmp_path / "run" / "junit" / "head.xml"
    report.parent.mkdir(parents=True)
    box = ContainerSandbox(root, config())
    code = "import sys; open(sys.argv[1].split('=', 1)[1], 'w').write('<testsuite/>')"
    done = asyncio.run(
        box.execute(["python", "-c", code, f"--junitxml={report}"], shown="python -c")
    )
    assert done.exit_code == 0, done.render()
    assert report.read_text() == "<testsuite/>"


def test_a_container_past_its_memory_limit_is_killed(root: Root) -> None:
    needs_containers()
    out = in_container(root, "x = bytearray(512 * 1024 * 1024)\nprint('ALLOCATED')", memory="64m")
    assert "ALLOCATED" not in out, out

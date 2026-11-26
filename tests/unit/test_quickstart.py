"""scripts/quickstart_check.py: which README commands the clean-machine quickstart check runs.

If the README's "Try it" section changes shape, this says so here, instead of the CI job
quietly checking fewer commands, or none."""

from __future__ import annotations

import shlex
from pathlib import Path

from ruamel.yaml import YAML
from typer.testing import CliRunner

from msa import cli
from scripts.quickstart_check import commands
from tests.support.scripts import ROOT

README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_check_runs_every_keyless_quickstart_command_as_written() -> None:
    assert commands(README) == [
        "uv sync",
        "uv run msa simulate --seed 21 --run-id demo",
        "uv run msa inspect demo --at 9",
        "uv run msa replay demo",
    ]


def test_the_quickstart_shows_a_run_that_completes(tmp_path: Path) -> None:
    """Exit codes alone are not enough: the README once showed a seed whose run was rejected
    after three events, and an ``inspect --at`` past its end, and every command still exited 0.
    A simulated run's outcome depends on its seed and its run id, so this runs them as written."""
    runner, db = CliRunner(), str(tmp_path / "runs.db")
    simulate, inspect, replay = (
        shlex.split(command)[3:]
        for command in commands(README)
        if command.startswith("uv run msa ")
    )
    ran = runner.invoke(cli.app, [*simulate, "--db", db])
    assert ran.exit_code == 0, ran.output
    assert ": COMPLETED: completed (aegis/" in ran.output.splitlines()[-1]
    events = sum(" -> " in line for line in ran.output.splitlines())
    at = int(inspect[inspect.index("--at") + 1])
    assert at < events  # a past state, not the end
    shown = runner.invoke(cli.app, [*inspect, "--db", db])
    assert shown.exit_code == 0, shown.output
    assert f"(live, seq {at})" in shown.output
    replayed = runner.invoke(cli.app, [*replay, "--db", db])
    assert replayed.exit_code == 0, replayed.output
    assert f"ok: {events} events replay deterministically" in replayed.output


def test_it_stops_before_the_first_command_that_needs_a_key() -> None:
    readme = (
        "## Try it\n\n```bash\nuv sync\n```\n\nIt needs `ANTHROPIC_API_KEY`:\n\n"
        "```bash\nuv run msa run --repo .\n```\n"
    )
    assert commands(readme) == ["uv sync"]
    assert commands("# no quickstart here\n") == []


def test_ci_runs_it_on_a_clean_runner() -> None:
    workflow = YAML(typ="safe").load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["quickstart"]
    uv = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("astral-sh/setup-uv"))
    assert uv["with"]["enable-cache"] is False  # nothing carried over from another run
    runs = [s.get("run", "") for s in job["steps"]]
    assert "python3 scripts/quickstart_check.py" in runs
    assert not any("ripgrep" in r or "apt-get" in r for r in runs)  # the README says it needs none

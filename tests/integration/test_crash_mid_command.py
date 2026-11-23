"""A SIGKILL from outside the run, while a sandboxed command is running.

The definition-of-done run (``test_demo.py``) kills itself from inside the scripted model,
so nothing else is running at the instant of the kill. The live run's first crash also
landed between commands, with msa alone. This crashes the happy-path greeter run from
outside, with ``scripts/live_run_crash.py``'s own kill. It waits until the unit-test
stage's pytest is running in the sandbox, in a session of its own, then stops and kills the
whole tree at once. The run is then resumed from disk, as ``msa resume`` would resume it.
It must complete: the killed stage runs again, nothing journaled runs again, and the
killed pytest left nothing behind that breaks the run.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from msa.core.events import Completion
from msa.core.values import Phase
from msa.runtime.journal import SqliteJournal
from tests.support.runs import (
    GOOD_GREETER,
    RUN,
    Harness,
    RoleScripts,
    Started,
    author_script,
    coder_script,
    make_harness,
    planner_script,
)
from tests.support.scripts import load

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "linux", reason="the kill reads /proc"),
]

crash = load("live_run_crash")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    harness = make_harness(tmp_path)
    harness.observer = Started(tmp_path / "started.log")
    return harness


def _run(harness: Harness) -> None:
    client = RoleScripts(
        planner=planner_script(), coder=coder_script(GOOD_GREETER), tester=author_script()
    )
    harness.run(client, approve_release=False)


def phase(db: Path) -> str | None:
    try:
        with closing(sqlite3.connect(f"file:{db}?mode=rw", uri=True)) as conn:
            row = conn.execute("SELECT phase FROM runs WHERE run_id = ?", (RUN,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def running_pytest(root: int) -> list[int]:
    found = []
    for pid in crash.tree(root)[1:]:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"pytest" in argv:
            found.append(pid)
    return found


def gone(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    return stat.rpartition(")")[2].split()[0] == "Z"


def test_a_kill_while_the_unit_tests_run_resumes_to_completion(harness: Harness) -> None:
    started = harness.observer
    assert isinstance(started, Started)
    db = harness.state_dir / "runs.db"
    child = multiprocessing.get_context("fork").Process(target=_run, args=(harness,))
    child.start()
    assert child.pid is not None

    deadline = time.monotonic() + 300
    pytests: list[int] = []
    while not pytests:
        assert child.is_alive(), f"the run ended (exit {child.exitcode}) before its unit tests"
        assert time.monotonic() < deadline, "the unit tests never started"
        if phase(db) == Phase.UNIT_TESTING:
            pytests = running_pytest(child.pid)
        time.sleep(0.005)
    killed = crash.crash(child.pid)
    child.join(timeout=30)
    assert child.exitcode == -signal.SIGKILL
    assert all(gone(pid) for pid in pytests)  # in a session of its own, killed all the same
    assert killed >= 2  # the run and its sandboxed pytest, at least

    with SqliteJournal(db) as journal:
        crashed = journal.load(RUN)
        completed = {e.effect_id for e in journal.events(RUN) if isinstance(e, Completion)}
    assert crashed.phase is Phase.UNIT_TESTING
    (in_flight,) = crashed.pending
    assert in_flight.command.kind == "run_unit_tests"
    before_resume = len(started.lines())

    # Nothing after the build asks the model, so the resumed run needs no scripts at all.
    final, journal = harness.resume(RoleScripts(planner=[], coder=[], tester=[]))
    with journal:
        assert final.phase is Phase.COMPLETED, final.outcome
        assert journal.verify(RUN).ok
    rerun = started.lines()[before_resume:]
    assert rerun[0] == (in_flight.id, "run_unit_tests")  # the killed stage, first, again
    assert not {effect for effect, _ in rerun} & completed  # nothing journaled ran again
    assert os.path.exists(harness.run_dir / "patch.diff")

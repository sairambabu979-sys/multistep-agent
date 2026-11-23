"""scripts/live_run_crash.py: the live run's crash, which must be a real one.

A stand-in for ``msa run`` journals the scripted run one event at a time and starts a
grandchild in a session of its own, as the sandbox starts the agents' commands. The script
must SIGKILL the whole tree once the journal reaches the seq it was given, and record what
the journal held then.
"""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest

from tests.support.driver import Driver
from tests.support.factory import limits
from tests.support.scripts import ROOT, load

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="it reads /proc")

RUN = "run-1"

# The stand-in run: seq 1 to 6 quickly, then a long wait, as a model call would be, with
# the acceptance tests still pending. It never gets past the wait unless nothing kills it.
STAND_IN = textwrap.dedent(
    """
    import json, subprocess, sys, time
    from datetime import timedelta
    from pathlib import Path

    sys.path.insert(0, {root!r})
    from msa.runtime.journal import SqliteJournal
    from msa.runtime.simulation import Profile, SimulationConfig
    from tests.support.driver import Driver
    from tests.support.factory import limits

    db, pids = Path(sys.argv[1]), Path(sys.argv[2])
    d = Driver(limits(wall_clock=timedelta(days=3650)))
    d.to_review(); d.review(); d.verify_release()
    environment = SimulationConfig(seed=0, profile=Profile.HAPPY).model_dump(mode="json")
    records = db.parent / "runs" / "run-1" / "invocations.jsonl"
    records.parent.mkdir(parents=True)
    with SqliteJournal(db) as journal:
        journal.create(environment, d.events[0], d.steps[0].state)
        grandchild = subprocess.Popen(["sleep", "600"], start_new_session=True)
        pids.write_text(str(grandchild.pid))
        for event, step in zip(d.events[1:], d.steps[1:]):
            if step.state.seq == 6:
                records.write_text(json.dumps({{"effect": "run-1:5:0"}}) + "\\n")
            journal.append(event, step.state, snapshot=False)
            time.sleep(60 if step.state.seq == 6 else 0.02)
    """
)


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"  # a zombie is dead, merely unreaped


def test_the_whole_tree_is_killed_at_the_seq_and_the_journal_is_recorded(tmp_path: Path) -> None:
    stand_in = tmp_path / "stand_in.py"
    stand_in.write_text(STAND_IN.format(root=str(ROOT)), encoding="utf-8")
    db, pids, evidence = tmp_path / "runs.db", tmp_path / "pids", tmp_path / "crash.json"

    argv = ["--db", str(db), "--run", RUN, "--after-seq", "6", "--evidence", str(evidence)]
    command = [sys.executable, str(stand_in), str(db), str(pids)]
    assert load("live_run_crash").main([*argv, "--", *command]) == 0

    grandchild = int(pids.read_text())
    assert not alive(grandchild)  # in a session of its own, and killed all the same
    found = json.loads(evidence.read_text())
    d = Driver(limits(wall_clock=timedelta(days=3650)))
    d.to_review()
    assert found == {
        "run": RUN,
        "after_seq": 6,
        "seq": 6,
        "event": "ImplementationCompleted",
        "phase": d.steps[5].state.phase.value,
        "pending": [{"effect": "run-1:5:1", "kind": "author_acceptance_tests"}],
        "processes": found["processes"],
        "invocations": 1,
    }
    assert found["processes"] >= 2  # the stand-in and its grandchild, at least


def test_a_command_that_ends_before_the_seq_crashed_nothing(tmp_path: Path) -> None:
    evidence = tmp_path / "crash.json"
    argv = ["--db", str(tmp_path / "runs.db"), "--run", RUN, "--after-seq", "3"]
    code = load("live_run_crash").main(
        [*argv, "--evidence", str(evidence), "--", sys.executable, "-c", "pass"]
    )
    assert code == 1
    assert not evidence.exists()

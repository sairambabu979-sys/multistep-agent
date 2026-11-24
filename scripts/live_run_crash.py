"""SIGKILL a run partway through, as a machine crash would, for ``msa resume`` to continue.

Used by the `live run` workflow (.github/workflows/live-run.yml):

    uv run python scripts/live_run_crash.py --db DB --run RUN --after-seq 6 \\
        --evidence crash.json -- .venv/bin/msa --config msa.toml run --run-id RUN --db DB ...

It starts the command in a session of its own and watches the run's journal. Once the run
has journaled ``--after-seq`` events, it stops every process the command started, then kills
them all with SIGKILL: no cleanup, no flush, no rollback. The agents' sandboxed commands run
in sessions of their own, so a signal to the command's process group would miss them; the
whole process tree is stopped first, so nothing in it can fork past the kill.

It writes what the journal holds at the crash to ``--evidence``, for
``scripts/live_run_summary.py --crash`` to check the resumed run against. That includes how
many effect starts the run had logged (``effects.jsonl``), so the summary can tell which
effects started again after the resume. The exit status is 0 once the run has been
crashed. It is 1 when the command was not killed: it ended before the journal reached
``--after-seq``, or it exited by itself before the kill landed. Then nothing was crashed,
the test did not happen, and no evidence is written.

Linux only: it reads the process tree from /proc.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from msa.core.ids import RunId
from msa.runtime.journal import open_journal

POLL_SECONDS = 0.1


def head_seq(db: Path, run_id: str) -> int:
    """The run's journaled sequence, read without writing anything; 0 until the run exists.

    Opened read-write but only read: a WAL journal's readers need its shared-memory file,
    and ``mode=rw`` never creates the database, so this cannot race the run creating it."""
    if not db.exists():
        return 0
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=rw", uri=True)) as conn:
            row = conn.execute("SELECT head_seq FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    except sqlite3.OperationalError:  # not migrated yet, or busy: look again
        return 0
    return int(row[0]) if row else 0


def _children() -> dict[int, list[int]]:
    found: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:  # it exited while we looked
            continue
        ppid = int(stat.rpartition(")")[2].split()[1])  # the name may hold spaces and ")"
        found.setdefault(ppid, []).append(int(entry.name))
    return found


def tree(root: int) -> list[int]:
    """``root`` and every process descended from it, whatever session each is in."""
    children = _children()
    found, todo = [], [root]
    while todo:
        pid = todo.pop()
        found.append(pid)
        todo.extend(children.get(pid, ()))
    return found


def crash(root: int) -> int:
    """SIGSTOP the whole tree until no new process appears, then SIGKILL all of it.
    Returns how many processes were killed."""
    stopped: set[int] = set()
    while new := [pid for pid in tree(root) if pid not in stopped]:
        for pid in new:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGSTOP)
            stopped.add(pid)
    for pid in stopped:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    return len(stopped)


def evidence(db: Path, run_id: str, after_seq: int, processes: int) -> dict[str, Any]:
    """What the journal and the run's records hold once every writer is dead."""
    rid = RunId(run_id)
    with open_journal(db) as journal:
        state = journal.load(rid)
        last = journal.events(rid, after=state.seq - 1)[-1]
    run_dir = db.parent / "runs" / run_id
    return {
        "run": run_id,
        "after_seq": after_seq,
        "seq": state.seq,
        "event": type(last).__name__,
        "phase": state.phase.value,
        "pending": [{"effect": e.id, "kind": e.command.kind} for e in state.pending],
        "processes": processes,
        "effects": lines(run_dir / "effects.jsonl"),
        "invocations": lines(run_dir / "invocations.jsonl"),
    }


def lines(path: Path) -> int:
    """Complete records in a run's .jsonl file (each is appended whole, then fsynced)."""
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--after-seq", type=int, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the run command")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the command to run after --")
    if args.after_seq < 1:
        parser.error("--after-seq must be at least 1")

    process = subprocess.Popen(command, start_new_session=True)  # our own argv, no shell
    while (seq := head_seq(args.db, args.run)) < args.after_seq:
        if process.poll() is not None:
            sys.stderr.write(
                f"the command ended (exit {process.returncode}) with the journal at seq "
                f"{seq}, before seq {args.after_seq}: nothing was crashed\n"
            )
            return 1
        time.sleep(POLL_SECONDS)
    processes = crash(process.pid)
    process.wait()
    if process.returncode != -signal.SIGKILL:
        sys.stderr.write(
            f"the command exited by itself (exit {process.returncode}) with the journal at "
            f"seq {seq}, before the kill landed: nothing was crashed\n"
        )
        return 1
    found = evidence(args.db, args.run, args.after_seq, processes)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(
        f"SIGKILLed {processes} process(es) with the journal at seq {found['seq']} "
        f"({found['event']}), {len(found['pending'])} effect(s) pending\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

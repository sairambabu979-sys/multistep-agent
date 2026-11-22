"""A symlink swapped in while tools run: the race path confinement is built to win.

A background thread atomically exchanges ``repo/swap`` (a real directory) with
``repo/.link`` (a symlink to the outside world) as fast as it can, while the tools keep
creating and reading files under ``swap``. Resolving and opening happen at different
instants, so many calls straddle an exchange. Each call may succeed inside the tree or be
refused; none may touch the outside.

The exchange is ``renameat2(RENAME_EXCHANGE)``, so ``swap`` is never missing: a flip that
left a gap would let the tools recreate ``swap`` as a plain directory and quietly end the
race. Linux only.

The flipper is a separate process, as an attacker would be. A thread would share the GIL
with the tools and could only flip between their calls: how often it ran, and whether any
flip landed mid-call at all, would then depend on how the interpreter and the OS share out
time, not on the code under test. (On a loaded CI runner it once flipped 252 times in
3,000 calls; pinned to one busy CPU, no flip ever landed inside a call.)
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from msa.config import Role
from msa.tools.errors import PathChanged, PathRejected, ToolError
from tests.support.tools import SECRET, build_tree

ROUNDS = 1500
RENAME_EXCHANGE = 2
AT_FDCWD = -100

# Exchanges its two arguments as fast as it can until SIGTERM, then prints how many times.
FLIPPER = f"""
import ctypes, os, signal, sys
exchange = ctypes.CDLL(None, use_errno=True).renameat2
a, b = (os.fsencode(p) for p in sys.argv[1:3])
flips = 0
def done(*_):
    print(flips, flush=True)
    os._exit(0)
signal.signal(signal.SIGTERM, done)
print("ready", flush=True)
while True:
    if exchange({AT_FDCWD}, a, {AT_FDCWD}, b, {RENAME_EXCHANGE}) != 0:
        print("errno", ctypes.get_errno(), flush=True)
        os._exit(1)
    flips += 1
"""

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="renameat2 is Linux-only")


def exchanger() -> ctypes._NamedFuncPointer:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        return libc.renameat2
    except AttributeError:  # pragma: no cover - glibc older than 2.28
        pytest.skip("renameat2 is unavailable")


def test_tools_never_cross_a_symlink_swapped_in_mid_call(tmp_path: Path) -> None:
    exchanger()  # skips where renameat2 does not exist
    tree = build_tree(tmp_path)
    swap, link = tree.repo / "swap", tree.repo / ".link"
    swap.mkdir()
    (swap / "secret.txt").write_text("decoy inside the tree\n")
    os.symlink(tree.outside, link)

    flipper = subprocess.Popen(
        [sys.executable, "-c", FLIPPER, str(swap), str(link)], stdout=subprocess.PIPE, text=True
    )
    assert flipper.stdout is not None
    assert flipper.stdout.readline() == "ready\n"
    coder = tree.workspace(Role.CODER)
    outcomes: dict[str, int] = {}
    try:
        for i in range(ROUNDS):
            for tool, args in (
                (coder.create_file, (f"swap/planted-{i}.txt", "planted\n")),
                (coder.read_file, ("swap/secret.txt",)),
            ):
                try:
                    result = tool(*args)
                except ToolError as exc:
                    outcomes[type(exc).__name__] = outcomes.get(type(exc).__name__, 0) + 1
                else:
                    assert SECRET.strip() not in getattr(result, "text", "")
                    outcomes["ok"] = outcomes.get("ok", 0) + 1
    finally:
        flipper.send_signal(signal.SIGTERM)
        reported, _ = flipper.communicate(timeout=30)
    assert flipper.returncode == 0, f"the flipper failed: {reported}"
    flips = int(reported)
    assert tree.outside_untouched(), outcomes
    assert flips > ROUNDS, f"the flipper barely ran ({flips} flips): the race was not exercised"
    # Every side of the race happened: calls that resolved a symlink were refused, calls that
    # resolved a real directory succeeded inside the tree, and calls that resolved a real
    # directory but met the symlink when opening it were refused. Those last ones (hundreds
    # per run) are the calls a check-then-open implementation would have let out.
    assert outcomes.get("ok", 0) > 0, outcomes
    assert outcomes.get(PathRejected.__name__, 0) > 0, outcomes
    assert outcomes.get(PathChanged.__name__, 0) > 0, outcomes
    assert set(outcomes) <= {"ok", PathRejected.__name__, PathChanged.__name__, "FileMissing"}

"""A symlink swapped in while tools run: the race path confinement is built to win.

A background thread atomically exchanges ``repo/swap`` (a real directory) with
``repo/.link`` (a symlink to the outside world) as fast as it can, while the tools keep
creating and reading files under ``swap``. Resolving and opening happen at different
instants, so many calls straddle an exchange. Each call may succeed inside the tree or be
refused; none may touch the outside.

The exchange is ``renameat2(RENAME_EXCHANGE)``, so ``swap`` is never missing: a flip that
left a gap would let the tools recreate ``swap`` as a plain directory and quietly end the
race. Linux only.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path

import pytest

from msa.config import Role
from msa.tools.errors import PathChanged, PathRejected, ToolError
from tests.support.tools import SECRET, build_tree

ROUNDS = 1500
RENAME_EXCHANGE = 2
AT_FDCWD = -100

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="renameat2 is Linux-only")


def exchanger() -> ctypes._NamedFuncPointer:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        return libc.renameat2
    except AttributeError:  # pragma: no cover - glibc older than 2.28
        pytest.skip("renameat2 is unavailable")


def test_tools_never_cross_a_symlink_swapped_in_mid_call(tmp_path: Path) -> None:
    renameat2 = exchanger()
    tree = build_tree(tmp_path)
    swap, link = tree.repo / "swap", tree.repo / ".link"
    swap.mkdir()
    (swap / "secret.txt").write_text("decoy inside the tree\n")
    os.symlink(tree.outside, link)
    stop = threading.Event()
    flips = 0

    def flip() -> None:
        nonlocal flips
        a, b = os.fsencode(swap), os.fsencode(link)
        while not stop.is_set():
            if renameat2(AT_FDCWD, a, AT_FDCWD, b, RENAME_EXCHANGE) != 0:
                raise OSError(ctypes.get_errno(), "renameat2 failed")
            flips += 1

    flipper = threading.Thread(target=flip, daemon=True)
    coder = tree.workspace(Role.CODER)
    outcomes: dict[str, int] = {}
    flipper.start()
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
        stop.set()
        flipper.join()
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

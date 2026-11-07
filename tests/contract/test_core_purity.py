"""The functional core performs no I/O and reads no clock or randomness.

This uses an allowlist, not a blocklist: a new import in ``msa.core`` fails this test until
someone deliberately decides it is pure and adds it here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "src" / "msa" / "core"

ALLOWED_IMPORTS = {
    "__future__",
    "collections",
    "collections.abc",
    "dataclasses",
    "datetime",  # for types and arithmetic only; clock reads are banned below
    "enum",
    "functools",
    "itertools",
    "typing",
    "pydantic",
}
BANNED_CALLS = {"now", "utcnow", "today", "time", "monotonic", "perf_counter", "random", "uuid4"}


def _modules() -> list[Path]:
    return sorted(CORE.rglob("*.py"))


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_core_modules_are_discovered() -> None:
    assert len(_modules()) >= 10


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_core_imports_only_pure_modules(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    impure = {
        name
        for name in _imports(tree)
        if not name.startswith("msa.core") and name not in ALLOWED_IMPORTS
    }
    assert impure == set(), f"{path.name} imports {sorted(impure)}"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_core_never_reads_clocks_or_randomness(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert calls & BANNED_CALLS == set()

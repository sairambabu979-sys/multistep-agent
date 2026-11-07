"""The weakened-test guard (PLAN §6.1): a Tester revision may not lower the bar.

When the Tester revises its own tests, every test function that existed before must still
exist, with at least as many checks. A check is an ``assert`` statement, or a
``pytest.raises`` / ``pytest.warns`` / ``pytest.approx`` call (these assert, too). Removing
a test, or a check from one, needs a Reviewer finding that approves it (M8); until then the
revision is refused.

This is a structural count, not a proof: rewriting ``assert x == 1`` into ``assert x``
keeps the count. It closes the cheap ways to turn a red test green, and the Reviewer reads
the rest.
"""

from __future__ import annotations

import ast
from collections.abc import Collection, Mapping

CHECK_CALLS = frozenset({"raises", "warns", "approx", "fail"})


def _is_check(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name in CHECK_CALLS
    return False


def checks(source: str) -> dict[str, int]:
    """Checks per test function (``test_*``, qualified by class), or {} if it does not parse:
    a file that does not parse fails its own tests anyway."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    found: dict[str, int] = {}

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}::")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                found[f"{prefix}{node.name}"] = sum(_is_check(n) for n in ast.walk(node))

    visit(tree.body, "")
    return found


def weakened(
    before: Mapping[str, str],
    after: Mapping[str, str | None],
    exempt: Collection[str] = (),
) -> list[str]:
    """What a revision weakened, given each changed file's source before and after (None:
    deleted). Files that are new in ``after`` cannot weaken anything. ``exempt`` names
    tests (``path::name``) that TESTING rejected, such as a test that passed before the
    change: replacing one is the fix, not a weakening."""
    problems = []
    for path, old in sorted(before.items()):
        new = after.get(path)
        old_checks = checks(old)
        new_checks = checks(new) if new is not None else {}
        for test, count in sorted(old_checks.items()):
            if f"{path}::{test}" in exempt:
                continue
            if test not in new_checks:
                problems.append(f"{path}::{test} was removed")
            elif new_checks[test] < count:
                problems.append(f"{path}::{test} went from {count} check(s) to {new_checks[test]}")
    return problems

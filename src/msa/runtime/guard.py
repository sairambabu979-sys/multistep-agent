"""Guards on the Tester's evidence (PLAN §6.1, ADR-012, ADR-013).

Acceptance tests are evidence only while they mean what the Tester wrote. Two kinds of
change could empty them of meaning without making anything fail, and each is refused:

- **A Tester revision that lowers the bar** (``weakened``). Every test that existed before
  must still exist, with at least as many *live* checks, and without a new skip or xfail
  mark. A check is an ``assert`` that can fail, or a ``pytest.raises`` / ``warns`` /
  ``approx`` / ``fail`` call. Checks after an unconditional ``return``, ``raise``,
  ``pytest.skip()`` (and the like) are dead and do not count, nor do those under
  ``if False:``, nor asserts that cannot fail (``assert True``, ``assert (x, "msg")``,
  ``assert x == x``). Weakening needs a Reviewer finding that approves it (M8); until then
  the revision is refused.
- **A Coder change to how those tests run** (``reconfigured``). pytest loads the conftest.py
  of every directory above a test, and reads its configuration from ini files and
  pyproject.toml found there. Whoever changes those can skip, deselect or ignore the tests
  below them, or load a plugin that rewrites their outcomes, without touching a test file.
  VERIFYING refuses such a change: in a run, the Tester's tests run under the base tree's
  pytest configuration.

Both are structural, not proofs: ``assert x == 1`` → ``assert x`` keeps the count, and code
under test can still detect pytest. They close the cheap ways to turn red into green; the
Reviewer reads the rest.

Tests are found the way pytest finds them by default: ``test*`` functions at module level,
and ``test*`` methods of ``Test*`` classes (or ``unittest.TestCase`` subclasses), qualified
by class (``TestGroup::test_x``).
"""

from __future__ import annotations

import ast
import configparser
import tomllib
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from pathlib import PurePosixPath

type _Function = ast.FunctionDef | ast.AsyncFunctionDef

CHECK_CALLS = frozenset({"raises", "warns", "approx", "fail"})
# pytest.skip() and friends raise: nothing after them in the same block runs.
RAISING_CALLS = frozenset({"skip", "xfail", "fail", "exit"})
# Marks (pytest's and unittest's) that make a test not run, or not count when it fails.
SKIP_MARKS = frozenset({"skip", "skipif", "xfail", "skipIf", "skipUnless", "expectedFailure"})
# Files that configure pytest for the directory they are in and every directory below it.
CONTROL_FILES = (
    "conftest.py",
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)


# ---------- finding tests


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_test_class(node: ast.ClassDef) -> bool:
    return node.name.startswith("Test") or any(
        (_name(base) or "").endswith("TestCase") for base in node.bases
    )


def _assigns_pytestmark(node: ast.stmt) -> bool:
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
    return isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "pytestmark"


def _pytestmark(body: Iterable[ast.stmt]) -> list[ast.expr]:
    """The values assigned to ``pytestmark`` in a module or class body."""
    return [
        node.value
        for node in body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        and node.value is not None
        and _assigns_pytestmark(node)
    ]


def _marks(nodes: Iterable[ast.AST]) -> frozenset[str]:
    return frozenset(
        name for node in nodes for sub in ast.walk(node) if (name := _name(sub)) in SKIP_MARKS
    )


def _tests(module: ast.Module) -> Iterator[tuple[str, _Function, frozenset[str]]]:
    """Each test pytest would collect: its qualified name, its node, and the skip/xfail
    marks on it, its classes or its module."""

    def visit(
        body: list[ast.stmt], prefix: str, inherited: frozenset[str]
    ) -> Iterator[tuple[str, _Function, frozenset[str]]]:
        for node in body:
            if isinstance(node, ast.ClassDef) and _is_test_class(node):
                marks = inherited | _marks([*node.decorator_list, *_pytestmark(node.body)])
                yield from visit(node.body, f"{prefix}{node.name}::", marks)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                yield f"{prefix}{node.name}", node, inherited | _marks(node.decorator_list)

    yield from visit(module.body, "", _marks(_pytestmark(module.body)))


def _parse(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def find_tests(source: str) -> list[str]:
    """The tests in a module, in source order, or [] if it does not parse."""
    module = _parse(source)
    return [name for name, _, _ in _tests(module)] if module else []


def marks(source: str) -> dict[str, frozenset[str]]:
    """Skip and xfail marks per test (only tests that have one)."""
    module = _parse(source)
    return {name: found for name, _, found in _tests(module) if found} if module else {}


# ---------- counting live checks


def _truth(test: ast.expr) -> bool | None:
    """The value of a constant condition, or None when it is not constant."""
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _truth(test.operand)
        return None if inner is None else not inner
    return None


def _vacuous(test: ast.expr) -> bool:
    """An assert condition that cannot be false."""
    if _truth(test) is True:
        return True
    if isinstance(test, ast.Tuple | ast.List | ast.Set) and test.elts:
        return True  # assert (x, "message"): a non-empty literal is always true
    if isinstance(test, ast.Dict) and test.keys:
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        same = ast.dump(test.left) == ast.dump(test.comparators[0])
        return same and isinstance(test.ops[0], ast.Eq | ast.Is | ast.LtE | ast.GtE)
    return False


def _is_check(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return not _vacuous(node.test)
    if isinstance(node, ast.Call):
        return _name(node.func) in CHECK_CALLS
    return False


def _count(node: ast.AST) -> int:
    return sum(_is_check(n) for n in ast.walk(node))


def _ends(stmt: ast.stmt) -> bool:
    """Nothing after ``stmt`` in its block runs. Conservative: unsure means no, so a
    check is only ever called dead when it plainly is."""
    if isinstance(stmt, ast.Return | ast.Raise | ast.Continue | ast.Break):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return _name(stmt.value.func) in RAISING_CALLS
    if isinstance(stmt, ast.If):
        truth = _truth(stmt.test)
        if truth is True:
            return _block_ends(stmt.body)
        if truth is False:
            return _block_ends(stmt.orelse)
        return _block_ends(stmt.body) and _block_ends(stmt.orelse)
    return False


def _block_ends(body: Sequence[ast.stmt]) -> bool:
    return any(_ends(s) for s in body)


def _live(body: Sequence[ast.stmt]) -> int:
    """The checks in a block that can run: those before the first statement that ends it."""
    total = 0
    for stmt in body:
        total += _live_in(stmt)
        if _ends(stmt):
            break
    return total


def _live_in(stmt: ast.stmt) -> int:
    match stmt:
        case ast.If(test=test, body=body, orelse=orelse):
            truth = _truth(test)
            return (
                _count(test)
                + (_live(body) if truth is not False else 0)
                + (_live(orelse) if truth is not True else 0)
            )
        case ast.While(test=test, body=body, orelse=orelse):
            return _count(test) + (_live(body) if _truth(test) is not False else 0) + _live(orelse)
        case (
            ast.For(iter=iterable, body=body, orelse=orelse)
            | ast.AsyncFor(iter=iterable, body=body, orelse=orelse)
        ):
            return _count(iterable) + _live(body) + _live(orelse)
        case ast.With(items=items, body=body) | ast.AsyncWith(items=items, body=body):
            return sum(_count(item.context_expr) for item in items) + _live(body)
        case (
            ast.Try(body=body, handlers=handlers, orelse=orelse, finalbody=final)
            | ast.TryStar(body=body, handlers=handlers, orelse=orelse, finalbody=final)
        ):
            caught = sum(_live(h.body) for h in handlers)
            return _live(body) + caught + _live(orelse) + _live(final)
        case ast.Match(subject=subject, cases=cases):
            return _count(subject) + sum(_live(case.body) for case in cases)
        case _:
            # Simple statements, and nested functions and classes: a helper's checks count
            # (moving asserts into a helper is not weakening).
            return _count(stmt)


def checks(source: str) -> dict[str, int]:
    """Live checks per test, or {} if the module does not parse: a file that does not
    parse fails its own tests anyway."""
    module = _parse(source)
    return {name: _live(node.body) for name, node, _ in _tests(module)} if module else {}


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
        old_checks, new_checks = checks(old), checks(new) if new is not None else {}
        old_marks, new_marks = marks(old), marks(new) if new is not None else {}
        for test, count in sorted(old_checks.items()):
            if f"{path}::{test}" in exempt:
                continue
            if test not in new_checks:
                problems.append(f"{path}::{test} was removed")
                continue
            if new_checks[test] < count:
                problems.append(f"{path}::{test} went from {count} check(s) to {new_checks[test]}")
            if added := new_marks.get(test, frozenset()) - old_marks.get(test, frozenset()):
                problems.append(f"{path}::{test} is now marked {', '.join(sorted(added))}")
    return problems


# ---------- what decides how pytest runs the Tester's tests


def pytest_controls(directories: Sequence[str]) -> list[str]:
    """The files that configure pytest for ``directories`` from outside them: the
    conftest.py and configuration files of every directory above each one, up to the
    repository root. Files inside them belong to whoever owns the directory."""
    above = {
        "/".join(PurePosixPath(d).parts[:depth])
        for d in directories
        for depth in range(len(PurePosixPath(d).parts))
    }
    return sorted(f"{a}/{name}" if a else name for a in above for name in CONTROL_FILES)


def _table(data: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _settings(path: str, text: str | None) -> object:
    """What in ``text`` pytest reads, so that unrelated edits (a dependency in
    pyproject.toml, say) do not count as reconfiguring it."""
    if text is None:
        return None
    match PurePosixPath(path).name:
        case "pyproject.toml":
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                return text
            pytest = _table(data, "tool", "pytest")
            plugins = _table(data, "project", "entry-points", "pytest11")
            return None if pytest is None and plugins is None else (pytest, plugins)
        case "tox.ini" | "setup.cfg" as name:
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(text)
            except configparser.Error:
                return text
            section = "pytest" if name == "tox.ini" else "tool:pytest"
            return dict(parser.items(section)) if parser.has_section(section) else None
        case _:  # conftest.py and pytest.ini: every byte matters
            return text


def reconfigured(path: str, before: str | None, after: str | None) -> str | None:
    """Why changing ``path`` from ``before`` to ``after`` (None: absent) changes how pytest
    runs the tests below it, or None if it does not."""
    old, new = _settings(path, before), _settings(path, after)
    if old == new:
        return None
    change = "created" if old is None else "deleted" if new is None else "changed"
    if PurePosixPath(path).name == "conftest.py":
        return f"`{path}` {change}: a conftest.py above the tests runs inside their session"
    return f"`{path}`: its pytest configuration was {change}"

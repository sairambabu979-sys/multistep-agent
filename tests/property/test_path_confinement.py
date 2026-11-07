"""M4's exit criterion: fuzzed path confinement (PLAN §13).

Paths are built from a hostile vocabulary: parent steps, in-tree and escaping symlinks,
absolute host paths, empty and dot segments, newlines, look-alike Unicode and encodings
that mean nothing to a filesystem. Whatever the path:

1. resolution agrees with a reference model (the OS's realpath, contained or refused);
2. a read never returns anything from outside the tree;
3. a write never creates or changes anything outside the tree;
4. the coder never changes a tester-owned file, whatever route it takes.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from msa.config import Role
from msa.tools.errors import ToolError
from msa.tools.paths import Root
from tests.support.tools import SECRET, Tree, build_tree

SEGMENTS = [
    "..",
    ".",
    "",
    "src",
    "docs",
    "escape",
    "secret_link",
    "owned_link",
    "tests",
    "acceptance",
    "test_login.py",
    "app.py",
    "secret.txt",
    "outside",
    "new",
    "a",
    "~",
    "%2e%2e",
    "...",
    "..\\..",
    " ",
    "a\nb",
    "\uff53\uff52\uff43",
    "\u2024\u2024",
]


@st.composite
def requested_paths(draw: st.DrawFn) -> str:
    segments = draw(st.lists(st.sampled_from(SEGMENTS), min_size=1, max_size=7))
    path = "/".join(segments)
    prefix = draw(st.sampled_from(["", "", "", "/", "//", "./"]))
    return prefix + path


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Tree:
    return build_tree(tmp_path_factory.mktemp("confinement"))


def reference(root: Root, requested: str) -> str | None:
    """What a correct resolver returns: the contained real path, or None for a refusal."""
    if not requested or "\x00" in requested or requested.startswith("/"):
        return None
    real = os.path.realpath(os.path.join(root.path, requested))
    inside = real == str(root.path) or real.startswith(str(root.path) + os.sep)
    return os.path.relpath(real, root.path) if inside else None


SETTINGS = settings(suppress_health_check=[HealthCheck.function_scoped_fixture])

# Every known route out, always tried: random generation alone finds them only by luck.
ESCAPES = [
    "../outside/secret.txt",
    "escape/secret.txt",
    "secret_link",
    "src/../../outside/secret.txt",
    "docs/../../outside/secret.txt",
    "escape/new",
    "../outside/new",
    "owned_link",
    "tests/acceptance/test_login.py",
    "docs/../tests/acceptance/test_login.py",
]


def with_escapes(**fixed: object) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def decorate(test: Callable[..., None]) -> Callable[..., None]:
        for path in ESCAPES:
            test = example(requested=path, **fixed)(test)
        return test

    return decorate


@SETTINGS
@with_escapes()
@given(requested=requested_paths())
def test_resolution_agrees_with_the_reference_model(tree: Tree, requested: str) -> None:
    root = Root.at(tree.repo)
    expected = reference(root, requested)
    try:
        resolved = root.resolve(requested).as_posix()
    except ToolError:
        assert expected is None, f"{requested!r} should resolve to {expected!r}"
    else:
        assert resolved == expected


@SETTINGS
@with_escapes()
@given(requested=requested_paths())
def test_a_read_never_returns_anything_from_outside(tree: Tree, requested: str) -> None:
    try:
        view = tree.workspace(Role.CODER).read_file(requested)
    except ToolError:
        return
    assert SECRET.strip() not in view.text
    assert (tree.repo / view.path).resolve().is_relative_to(tree.repo.resolve())


@SETTINGS
@with_escapes(role=Role.CODER)
@given(requested=requested_paths(), role=st.sampled_from(list(Role)))
def test_a_write_never_reaches_outside_the_tree(tree: Tree, requested: str, role: Role) -> None:
    workspace = tree.workspace(role)
    try:
        done = workspace.create_file(requested, "planted\n")
    except ToolError:
        pass
    else:
        written = (tree.repo / done.path).resolve()
        assert written.is_relative_to(tree.repo.resolve())
        assert workspace.policy.allows(done.path)
        written.unlink()  # keep the module's tree as it was
    assert tree.outside_untouched()


@SETTINGS
@with_escapes(old="pass")
@given(requested=requested_paths(), old=st.sampled_from(["pass", "def", "test_login"]))
def test_the_coder_never_changes_a_tester_file(tree: Tree, requested: str, old: str) -> None:
    owned = tree.repo / "tests" / "acceptance" / "test_login.py"
    before = owned.read_bytes()
    coder = tree.workspace(Role.CODER)
    try:
        expected = coder.read_file(requested).content_hash
    except ToolError:
        expected = "sha256:unknown"
    try:
        coder.edit(requested, old, "assert False  # sabotaged", expected)
    except ToolError:
        landed = False
    else:
        landed = True
    changed = owned.read_bytes() != before  # judged before anything is put back
    if landed:  # restore whatever file it hit, so later examples start clean
        target = Path(os.path.realpath(tree.repo / requested))
        target.write_bytes(target.read_bytes().replace(b"assert False  # sabotaged", old.encode()))
    assert not changed, f"{requested!r} let the coder change a tester-owned file"

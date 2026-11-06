"""File tools: reads with hashes, capability-checked atomic writes, violations recorded."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from msa.config import Role
from msa.tools.audit import Violation, log_violation
from msa.tools.errors import (
    CapabilityDenied,
    EditMismatch,
    FileExists,
    FileMissing,
    NotText,
    PathRejected,
    StaleRead,
    TooLarge,
    ToolError,
)
from msa.tools.workspace import Workspace, content_hash
from tests.support.tools import APP, Tree, build_tree


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return build_tree(tmp_path)


@pytest.fixture
def coder(tree: Tree) -> Workspace:
    return tree.workspace(Role.CODER)


# ---------- read_file


def test_a_read_returns_numbered_lines_and_the_whole_files_hash(coder: Workspace) -> None:
    view = coder.read_file("src/app.py")
    assert (view.path, view.first_line, view.last_line, view.total_lines) == ("src/app.py", 1, 2, 2)
    assert view.text == APP
    assert view.content_hash == content_hash(APP.encode())
    assert view.render() == (
        f"src/app.py (lines 1-2 of 2) {view.content_hash}\n"
        '     1\tdef greet(name):\n     2\t    return f"hello {name}"'
    )


def test_a_range_selects_lines_but_hashes_the_whole_file(coder: Workspace, tree: Tree) -> None:
    (tree.repo / "five.txt").write_text("a\nb\nc\nd\ne\n")
    view = coder.read_file("five.txt", start_line=2, end_line=3)
    assert (view.text, view.first_line, view.last_line) == ("b\nc\n", 2, 3)
    assert view.content_hash == content_hash(b"a\nb\nc\nd\ne\n")
    assert coder.read_file("five.txt", start_line=4, end_line=99).text == "d\ne\n"
    with pytest.raises(ToolError, match="has 5 lines; lines 9-5 select nothing"):
        coder.read_file("five.txt", start_line=9)


def test_an_empty_file_reads_as_empty(coder: Workspace, tree: Tree) -> None:
    (tree.repo / "empty.txt").write_text("")
    view = coder.read_file("empty.txt")
    assert (view.text, view.total_lines) == ("", 0)
    assert view.render() == f"empty.txt (empty) {content_hash(b'')}"


def test_reading_through_an_in_tree_symlink_reports_the_real_path(coder: Workspace) -> None:
    assert coder.read_file("docs/app.py").path == "src/app.py"


def test_reads_are_refused_for_binary_undecodable_and_oversized_files(
    tree: Tree, coder: Workspace
) -> None:
    (tree.repo / "blob.bin").write_bytes(b"\x89PNG\x00\x01")
    (tree.repo / "latin1.txt").write_bytes("café".encode("latin-1"))
    (tree.repo / "big.txt").write_text("x" * 5000)
    (tree.repo / "wide.txt").write_text("y\n" * 200)
    with pytest.raises(NotText, match=re.escape("blob.bin is binary")):
        coder.read_file("blob.bin")
    with pytest.raises(NotText, match=re.escape("latin1.txt is not UTF-8 text")):
        coder.read_file("latin1.txt")
    small = tree.workspace(Role.CODER, read_limit_bytes=1000, view_limit_chars=100)
    with pytest.raises(
        TooLarge, match=re.escape("big.txt is 5000 bytes, over the 1000-byte read limit")
    ):
        small.read_file("big.txt")
    with pytest.raises(TooLarge, match="it has 200 lines: read it in ranges"):
        small.read_file("wide.txt")
    assert small.read_file("wide.txt", start_line=1, end_line=10).last_line == 10


def test_a_file_that_grows_past_the_limit_while_read_is_refused(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = tree.workspace(Role.CODER, read_limit_bytes=10)
    (tree.repo / "grows.txt").write_text("12345")
    real_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: real_read(fd, n) * 3)  # appears to grow
    with pytest.raises(TooLarge, match=re.escape("grows.txt is over the 10-byte read limit")):
        small.read_file("grows.txt")


# ---------- create_file


def test_create_makes_the_file_and_its_directories_atomically(coder: Workspace, tree: Tree) -> None:
    done = coder.create_file("src/pkg/new.py", "x = 1\n")
    assert done.render() == f"created src/pkg/new.py {content_hash(b'x = 1' + bytes([10]))}"
    assert (tree.repo / "src" / "pkg" / "new.py").read_text() == "x = 1\n"
    assert stat.S_IMODE((tree.repo / "src" / "pkg" / "new.py").stat().st_mode) == 0o644
    assert not [p for p in (tree.repo / "src" / "pkg").iterdir() if p.name.startswith(".msa-")]


def test_create_never_replaces_a_file(coder: Workspace, tree: Tree) -> None:
    with pytest.raises(
        FileExists, match=re.escape("src/app.py already exists; read it and edit it")
    ):
        coder.create_file("src/app.py", "clobbered")
    assert (tree.repo / "src" / "app.py").read_text() == APP
    assert not [p for p in (tree.repo / "src").iterdir() if p.name.startswith(".msa-")]


def test_create_is_refused_at_the_root_and_outside_it(coder: Workspace, tree: Tree) -> None:
    with pytest.raises(PathRejected, match="is the repository root"):
        coder.create_file(".", "x")
    with pytest.raises(PathRejected, match="resolves outside"):
        coder.create_file("escape/new.txt", "x")
    assert tree.outside_untouched()


# ---------- edit


def read_hash(workspace: Workspace, path: str) -> str:
    return workspace.read_file(path).content_hash


def test_an_edit_replaces_one_occurrence_and_returns_the_new_hash(
    coder: Workspace, tree: Tree
) -> None:
    (tree.repo / "src" / "app.py").chmod(0o751)
    done = coder.edit("src/app.py", "hello", "hi", read_hash(coder, "src/app.py"))
    edited = APP.replace("hello", "hi")
    assert (tree.repo / "src" / "app.py").read_text() == edited
    assert done.content_hash == content_hash(edited.encode())
    assert done.render() == f"edited src/app.py {done.content_hash}"
    assert stat.S_IMODE((tree.repo / "src" / "app.py").stat().st_mode) == 0o751


def test_an_edit_of_a_file_changed_since_its_read_is_refused(coder: Workspace, tree: Tree) -> None:
    seen = read_hash(coder, "src/app.py")
    (tree.repo / "src" / "app.py").write_text(APP + "# someone else\n")
    with pytest.raises(StaleRead, match="changed since it was read; read it again"):
        coder.edit("src/app.py", "hello", "hi", seen)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("absent", "x", "old does not occur in src/app.py"),
        ("name", "who", "old occurs 2 times in src/app.py"),
        ("", "x", "old does not occur"),
        ("hello", "hello", "old and new are identical"),
    ],
)
def test_an_edit_must_match_exactly_once(
    coder: Workspace, old: str, new: str, message: str
) -> None:
    with pytest.raises(EditMismatch, match=message):
        coder.edit("src/app.py", old, new, read_hash(coder, "src/app.py"))


def test_an_edit_breaks_a_hard_link_instead_of_writing_through_it(
    coder: Workspace, tree: Tree
) -> None:
    os.link(tree.outside / "secret.txt", tree.repo / "src" / "linked.txt")
    coder.edit("src/linked.txt", "secret", "public", read_hash(coder, "src/linked.txt"))
    assert tree.outside_untouched()
    assert (tree.repo / "src" / "linked.txt").read_text() != (
        tree.outside / "secret.txt"
    ).read_text()


def test_an_edit_of_a_missing_file_is_refused(coder: Workspace) -> None:
    with pytest.raises(FileMissing, match=re.escape("src/nope.py does not exist")):
        coder.edit("src/nope.py", "a", "b", "sha256:0")


# ---------- capabilities and violations


def test_capability_is_checked_on_the_resolved_path(tree: Tree, coder: Workspace) -> None:
    """owned_link is an in-tree symlink into tester paths: it cannot launder a write."""
    before = (tree.repo / "tests" / "acceptance" / "test_login.py").read_text()
    with pytest.raises(
        CapabilityDenied, match=re.escape("'tests/acceptance/test_login.py': it belongs to tester")
    ):
        coder.edit("owned_link", "pass", "assert False", "sha256:irrelevant")
    assert (tree.repo / "tests" / "acceptance" / "test_login.py").read_text() == before


def test_a_denied_edit_reveals_nothing_about_the_file(tree: Tree, coder: Workspace) -> None:
    """The capability check runs before the hash check, so a wrong hash is not reported."""
    with pytest.raises(CapabilityDenied):
        coder.edit("tests/acceptance/test_login.py", "pass", "x", "sha256:wrong")


def test_security_refusals_are_recorded_as_violations(tree: Tree) -> None:
    coder = tree.workspace(Role.CODER)
    reviewer = tree.workspace(Role.REVIEWER)
    for attempt in (
        lambda: coder.read_file("../outside/secret.txt"),
        lambda: coder.create_file("tests/acceptance/test_new.py", "x"),
        lambda: reviewer.create_file("src/new.py", "x"),
    ):
        with pytest.raises(ToolError):
            attempt()
    with pytest.raises(FileMissing):  # an honest mistake, not a violation
        coder.read_file("src/nope.py")
    assert [(v.kind, v.role, v.tool, v.path) for v in tree.violations] == [
        ("path_rejected", "coder", "read_file", "../outside/secret.txt"),
        ("capability_denied", "coder", "create_file", "tests/acceptance/test_new.py"),
        ("capability_denied", "reviewer", "create_file", "src/new.py"),
    ]
    assert "resolves outside the repository" in tree.violations[0].detail
    assert tree.outside_untouched()


def test_violations_are_logged_by_default(caplog: pytest.LogCaptureFixture) -> None:
    violation = Violation("path_rejected", "coder", "read_file", "../x", "resolves outside")
    with caplog.at_level("WARNING", logger="msa.security"):
        log_violation(violation)
    assert caplog.messages == [
        "read_file refused path_rejected for coder on '../x': resolves outside"
    ]


@pytest.mark.parametrize(
    ("error", "kind", "message"),
    [
        (
            FileNotFoundError(2, "gone"),
            "PathChanged",
            "src/new.py changed while it was being written",
        ),
        (
            OSError(28, "No space left on device"),
            "ToolError",
            "src/new.py cannot be written: No space left on device",
        ),
    ],
)
def test_os_failures_while_writing_become_tool_errors(
    coder: Workspace, monkeypatch: pytest.MonkeyPatch, error: OSError, kind: str, message: str
) -> None:
    def fail(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(ToolError, match=re.escape(message)) as info:
        coder.create_file("src/new.py", "x")
    assert type(info.value).__name__ == kind

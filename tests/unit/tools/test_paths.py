"""Path confinement: resolve once, then open without following symlinks."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

import pytest

from msa.tools.errors import FileMissing, NotAFile, PathChanged, PathRejected, ToolError
from msa.tools.paths import MAX_PATH_CHARS, Root
from tests.support.tools import Tree, build_tree


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return build_tree(tmp_path)


@pytest.fixture
def root(tree: Tree) -> Root:
    return Root.at(tree.repo)


@pytest.mark.parametrize(
    ("requested", "resolved"),
    [
        ("src/app.py", "src/app.py"),
        ("./src//app.py", "src/app.py"),
        ("src/../src/app.py", "src/app.py"),
        ("docs/app.py", "src/app.py"),  # through an in-tree symlink, to the real file
        ("owned_link", "tests/acceptance/test_login.py"),
        (".", "."),
        ("src/new/file.py", "src/new/file.py"),  # need not exist yet
        ("~/x", "~/x"),  # no tilde expansion: a literal name
    ],
)
def test_paths_inside_resolve_to_their_real_location(
    root: Root, requested: str, resolved: str
) -> None:
    assert root.resolve(requested) == PurePosixPath(resolved)


@pytest.mark.parametrize(
    ("requested", "reason"),
    [
        ("../outside/secret.txt", "resolves outside"),
        ("src/../../outside", "resolves outside"),
        ("escape/secret.txt", "resolves outside"),
        ("secret_link", "resolves outside"),
        ("docs/../../outside", "resolves outside"),
        ("..", "resolves outside"),
        ("/etc/passwd", "is absolute"),
        ("", "not a valid path"),
        ("src/\x00app.py", "not a valid path"),
        ("a" * (MAX_PATH_CHARS + 1), "not a valid path"),
    ],
)
def test_paths_that_leave_or_abuse_the_tree_are_rejected(
    root: Root, requested: str, reason: str
) -> None:
    with pytest.raises(PathRejected, match=reason):
        root.resolve(requested)


def test_a_long_requested_path_is_shortened_in_the_message(root: Root) -> None:
    with pytest.raises(PathRejected) as info:
        root.resolve("../" + "x" * 500)
    assert len(str(info.value)) < 300


def test_the_root_may_itself_be_reached_through_a_symlink(tree: Tree) -> None:
    os.symlink(tree.repo, tree.base / "repo_link")
    assert Root.at(tree.base / "repo_link").path == tree.repo.resolve()


def test_a_root_must_be_a_directory(tree: Tree) -> None:
    with pytest.raises(ValueError, match="is not a directory"):
        Root.at(tree.repo / "src" / "app.py")


def test_files_open_by_descriptor_and_must_be_regular(root: Root, tree: Tree) -> None:
    with root.open_file(PurePosixPath("src/app.py")) as (fd, status):
        assert os.read(fd, 3) == b"def"
        assert status.st_size == (tree.repo / "src" / "app.py").stat().st_size
    os.mkfifo(tree.repo / "src" / "pipe")
    with pytest.raises(NotAFile, match="not a regular file"):  # and it does not block
        root.open_file(PurePosixPath("src/pipe")).__enter__()
    for directory in ("src", "."):
        with pytest.raises(NotAFile, match="directory"):
            root.open_file(PurePosixPath(directory)).__enter__()
    with pytest.raises(FileMissing, match=re.escape("src/nope.py does not exist")):
        root.open_file(PurePosixPath("src/nope.py")).__enter__()
    with pytest.raises(FileMissing, match=re.escape("nope/x.py does not exist")):
        root.open_file(PurePosixPath("nope/x.py")).__enter__()


def test_a_symlink_that_appears_after_resolving_is_refused(root: Root, tree: Tree) -> None:
    """The race the descriptor walk exists for: resolved clean, swapped before the open."""
    resolved = root.resolve("src/app.py")
    (tree.repo / "src" / "app.py").unlink()
    os.symlink(tree.outside / "secret.txt", tree.repo / "src" / "app.py")
    with pytest.raises(PathChanged, match="changed while it was being opened"):
        root.open_file(resolved).__enter__()
    (tree.repo / "src").rename(tree.repo / "src_moved")  # resolved.parent was a real dir
    os.symlink(tree.outside, tree.repo / "src")
    with pytest.raises(PathChanged):
        root.directory(resolved.parent).__enter__()


def test_missing_directories_are_created_only_when_asked(root: Root, tree: Tree) -> None:
    with pytest.raises(FileMissing):
        root.directory(PurePosixPath("a/b")).__enter__()
    with root.directory(PurePosixPath("a/b"), create=True) as fd:
        assert os.listdir(fd) == []
    assert (tree.repo / "a" / "b").is_dir()


def test_other_open_failures_name_the_relative_path_only(root: Root, tree: Tree) -> None:
    locked = tree.repo / "locked"
    locked.mkdir()
    (locked / "f").write_text("x")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("permission checks do not apply to root")
        with pytest.raises(ToolError, match=r"^locked/f cannot be opened: Permission denied$"):
            root.open_file(PurePosixPath("locked/f")).__enter__()
    finally:
        locked.chmod(0o755)


def test_keys_are_nfc_and_case_folded_only_on_case_insensitive_disks(tree: Tree) -> None:
    decomposed = PurePosixPath("Tests/Acceptance/café.py")
    sensitive = Root(tree.repo, case_insensitive=False)
    insensitive = Root(tree.repo, case_insensitive=True)
    assert sensitive.key(decomposed) == "Tests/Acceptance/café.py"
    assert insensitive.key(decomposed) == "tests/acceptance/café.py"


def test_case_sensitivity_is_probed_on_the_root(tmp_path: Path) -> None:
    lower = tmp_path / "repo"
    lower.mkdir()
    probed = Root.at(lower).case_insensitive
    assert probed == os.path.exists(tmp_path / "REPO")
    digits = tmp_path / "1234"
    digits.mkdir()
    assert Root.at(digits).case_insensitive == (os.uname().sysname == "Darwin")


def test_an_over_long_name_is_a_tool_error_not_a_crash(root: Root) -> None:
    long_name = PurePosixPath("n" * 300)
    with pytest.raises(ToolError, match="cannot be opened: File name too long"):
        root.directory(long_name, create=True).__enter__()


@pytest.mark.parametrize("unresolved", ["../outside", "/etc", "src/../.."])
def test_the_walk_refuses_parent_steps_on_its_own(root: Root, unresolved: str) -> None:
    """Defence in depth: even a path that skipped resolve() cannot climb out by '..'."""
    with pytest.raises(PathRejected, match="is not a resolved path inside the repository"):
        root.directory(PurePosixPath(unresolved)).__enter__()

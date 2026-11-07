"""Every path a tool touches stays inside the worktree, without a race (ADR-009).

Checking a path and then opening it by name is a time-of-check/time-of-use race: a symlink
swapped in between redirects the open anywhere. So a path is used in two steps:

1. **Resolve once.** The requested path is joined to the root and resolved through
   symlinks (``realpath``). The result must lie inside the root; in-tree symlinks are fine,
   anything that leaves the tree is refused. Capability checks use this resolved path,
   so an in-tree symlink cannot launder a write into another role's files.
2. **Open without following.** The resolved path holds no symlinks, so it is opened one
   component at a time from a descriptor of the root, each with ``O_NOFOLLOW``. A symlink
   that appears after step 1 makes the open fail, and the tool refuses the call.
   Everything after step 1 is anchored to descriptors, so nothing can be redirected. The
   walk also refuses ``..`` on its own, so each step holds even if the other had a bug.

Writes go through a temporary file in the same directory and an atomic rename. That also
breaks hard links: a write never reaches a file shared with a path outside the tree.

POSIX only, like the rest of the runtime.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from msa.tools.errors import FileMissing, NotAFile, PathChanged, PathRejected, ToolError

MAX_PATH_CHARS = 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True, slots=True)
class Root:
    """A worktree root: the real directory every tool path is confined to."""

    path: Path
    case_insensitive: bool

    @classmethod
    def at(cls, path: Path | str) -> Root:
        real = Path(os.path.realpath(path))
        if not real.is_dir():
            raise ValueError(f"{path} is not a directory")
        return cls(real, _case_insensitive(real))

    def resolve(self, requested: str) -> PurePosixPath:
        """The real path, relative to the root, that ``requested`` names. ``.`` is the root."""
        if not requested or "\x00" in requested or len(requested) > MAX_PATH_CHARS:
            raise PathRejected(f"{_shown(requested)} is not a valid path")
        if requested.startswith("/"):
            raise PathRejected(
                f"{_shown(requested)} is absolute; give a path relative to the repository root"
            )
        try:
            real = os.path.realpath(os.path.join(self.path, requested))
        except OSError:
            # Python 3.12's realpath does not guard readlink: a symlink replaced mid-resolve
            # raises EINVAL or ENOENT, with the host path in the message. It changed under us.
            raise PathChanged(f"{_shown(requested)} changed while it was being resolved") from None
        root = str(self.path)
        if real != root and not real.startswith(root + os.sep):
            raise PathRejected(f"{_shown(requested)} resolves outside the repository")
        return PurePosixPath(os.path.relpath(real, root))

    def key(self, relative: PurePosixPath) -> str:
        """The form capability globs match: NFC, and case-folded on case-insensitive disks,
        where ``Tests/`` and ``tests/`` are the same directory."""
        text = unicodedata.normalize("NFC", relative.as_posix())
        return text.casefold() if self.case_insensitive else text

    # ---------- descriptor-anchored access

    @contextlib.contextmanager
    def directory(
        self, relative: PurePosixPath, *, create: bool = False, shown: PurePosixPath | None = None
    ) -> Iterator[int]:
        """A descriptor for the directory ``relative`` (already resolved), opened component
        by component without following symlinks. With ``create``, missing directories are
        made on the way. Errors name ``shown`` (default: ``relative``)."""
        fd = os.open(self.path, _DIR_FLAGS)
        try:
            for part in _parts(relative):
                child = _child(fd, part, shown or relative, create=create)  # fd stays open
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)  # exactly once: each descriptor has one owner at a time

    @contextlib.contextmanager
    def open_file(self, relative: PurePosixPath) -> Iterator[tuple[int, os.stat_result]]:
        """A read-only descriptor of the regular file ``relative`` and its status."""
        if not _parts(relative):
            raise NotAFile(". is a directory")
        with self.directory(relative.parent, shown=relative) as parent:
            try:
                fd = os.open(
                    relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                )
            except FileNotFoundError:
                raise FileMissing(f"{relative} does not exist") from None
            except OSError as exc:
                raise _open_error(exc, relative) from None
            try:
                status = os.fstat(fd)
                if stat.S_ISDIR(status.st_mode):
                    raise NotAFile(f"{relative} is a directory")
                if not stat.S_ISREG(status.st_mode):
                    raise NotAFile(f"{relative} is not a regular file")
                yield fd, status
            finally:
                os.close(fd)


def _parts(relative: PurePosixPath) -> tuple[str, ...]:
    """The components to walk. A resolved path never holds ``..`` or a root, and ``..`` is
    not a symlink, so ``O_NOFOLLOW`` would let it climb out: refuse it here too, so this
    layer holds even if resolution had a bug."""
    if relative.is_absolute() or ".." in relative.parts:
        raise PathRejected(f"{relative.as_posix()!r} is not a resolved path inside the repository")
    return tuple(part for part in relative.parts if part != ".")


def _child(fd: int, part: str, relative: PurePosixPath, *, create: bool) -> int:
    """Open the directory ``part`` inside ``fd``, refusing symlinks; ``fd`` stays open."""
    try:
        try:
            return os.open(part, _DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            if not create:
                raise FileMissing(f"{relative} does not exist") from None
            with contextlib.suppress(FileExistsError):  # a concurrent creator is fine
                os.mkdir(part, 0o755, dir_fd=fd)
            return os.open(part, _DIR_FLAGS, dir_fd=fd)
    except OSError as exc:
        raise _open_error(exc, relative) from None


def _open_error(exc: OSError, relative: PurePosixPath) -> ToolError:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        # The resolved path held no symlink a moment ago; one has appeared since.
        return PathChanged(f"{relative} changed while it was being opened")
    return ToolError(f"{relative} cannot be opened: {exc.strerror}")


def _case_insensitive(real: Path) -> bool:
    """Whether names on this filesystem match regardless of case, probed on the root."""
    flipped = real.parent / real.name.swapcase()
    if flipped == real:  # no letters in the name to flip: fall back to the platform norm
        return sys.platform == "darwin"
    try:
        return os.path.samestat(os.stat(real), os.stat(flipped))
    except OSError:
        return False


def _shown(requested: str) -> str:
    """A requested path for an error message: quoted, bounded, control characters escaped."""
    text = requested if len(requested) <= 200 else requested[:200] + "…"
    return repr(text)

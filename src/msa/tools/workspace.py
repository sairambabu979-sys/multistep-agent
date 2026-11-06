"""File tools on one worktree, for one role: read, create, edit (PLAN §3.2, ADR-009).

Every call confines its path (``msa.tools.paths``), and every write is checked against the
role's capability on the *resolved* path before anything is read or written. Refusals that
matter for security (a path escape, a symlink race, a capability denial) are reported as
``Violation`` records as well as tool errors: the model sees the error and carries on, and
the run's operator sees the record (``msa.tools.audit``).

Reads return a ``content_hash``; an edit must quote it, so the model can never overwrite a
file it has not seen in its current form. Writes are atomic: a temporary file in the same
directory, fsynced, then renamed over the target (or linked into place for a new file, which
cannot clobber one that appeared meanwhile).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

from msa.config import SandboxConfig
from msa.tools.audit import Violation, log_violation, reporting
from msa.tools.capabilities import WritePolicy
from msa.tools.errors import (
    EditMismatch,
    FileExists,
    NotText,
    PathChanged,
    PathRejected,
    StaleRead,
    TooLarge,
    ToolError,
)
from msa.tools.paths import Root


def content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class FileView:
    path: str
    text: str  # the selected lines, verbatim
    first_line: int
    last_line: int
    total_lines: int
    content_hash: str  # of the whole file, whatever the range

    def render(self) -> str:
        """Numbered lines for the model; the numbers are not part of the file."""
        span = (
            f"lines {self.first_line}-{self.last_line} of {self.total_lines}"
            if self.total_lines
            else "empty"
        )
        lines = self.text.splitlines()
        body = "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(lines, self.first_line))
        return f"{self.path} ({span}) {self.content_hash}\n{body}".rstrip("\n")


@dataclass(frozen=True, slots=True)
class Written:
    path: str
    content_hash: str
    created: bool

    def render(self) -> str:
        verb = "created" if self.created else "edited"
        return f"{verb} {self.path} {self.content_hash}"


class Workspace:
    def __init__(
        self,
        root: Root,
        policy: WritePolicy,
        limits: SandboxConfig,
        *,
        on_violation: Callable[[Violation], None] = log_violation,
    ) -> None:
        self.root = root
        self.policy = policy
        self.limits = limits
        self._on_violation = on_violation

    # ---------- tools

    def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> FileView:
        with self._reporting("read_file", path):
            relative = self.root.resolve(path)
            data = self._read(relative)
            text = _decode(data, relative)
            lines = text.splitlines(keepends=True)
            first = max(1, start_line or 1)
            last = min(len(lines), end_line or len(lines))
            if lines and first > last:
                raise ToolError(
                    f"{relative} has {len(lines)} lines; lines {first}-{last} select nothing"
                )
            selected = "".join(lines[first - 1 : last])
            if len(selected) > self.limits.view_limit_chars:
                raise TooLarge(
                    f"{relative} lines {first}-{last} are {len(selected)} characters, over the "
                    f"{self.limits.view_limit_chars} limit; it has {len(lines)} lines: read it "
                    "in ranges with start_line and end_line"
                )
            return FileView(
                path=relative.as_posix(),
                text=selected,
                first_line=first if lines else 0,
                last_line=last,
                total_lines=len(lines),
                content_hash=content_hash(data),
            )

    def create_file(self, path: str, content: str) -> Written:
        with self._reporting("create_file", path):
            relative = self._writable(path)
            data = content.encode("utf-8")
            with _writing(relative), self.root.directory(relative.parent, create=True) as parent:
                with _temporary(parent, data, mode=0o644) as temp:
                    try:  # link, unlike rename, refuses to replace a file that appeared
                        os.link(temp, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
                    except FileExistsError:
                        raise FileExists(
                            f"{relative} already exists; read it and edit it"
                        ) from None
                _fsync_dir(parent)
            return Written(relative.as_posix(), content_hash(data), created=True)

    def edit(self, path: str, old: str, new: str, expected_hash: str) -> Written:
        with self._reporting("edit", path):
            relative = self._writable(path)  # capability first: a refusal reveals nothing
            with self.root.open_file(relative) as (fd, status):
                current = _read_all(fd, status, relative, self.limits.read_limit_bytes)
            if content_hash(current) != expected_hash:
                raise StaleRead(f"{relative} changed since it was read; read it again")
            text = _decode(current, relative)
            if old == new:
                raise EditMismatch("old and new are identical; nothing to change")
            count = text.count(old) if old else 0
            if count != 1:
                where = "does not occur" if count == 0 else f"occurs {count} times"
                raise EditMismatch(
                    f"old {where} in {relative}; quote exactly one occurrence, "
                    "with enough surrounding lines to make it unique"
                )
            data = text.replace(old, new, 1).encode("utf-8")
            with _writing(relative), self.root.directory(relative.parent) as parent:
                with _temporary(parent, data, mode=stat.S_IMODE(status.st_mode)) as temp:
                    os.replace(temp, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
                _fsync_dir(parent)
            return Written(relative.as_posix(), content_hash(data), created=False)

    # ---------- shared steps

    def _writable(self, path: str) -> PurePosixPath:
        relative = self.root.resolve(path)
        if relative == PurePosixPath("."):
            raise PathRejected(f"{path!r} is the repository root, not a file")
        self.policy.check(self.root.key(relative))
        return relative

    def _read(self, relative: PurePosixPath) -> bytes:
        with self.root.open_file(relative) as (fd, status):
            return _read_all(fd, status, relative, self.limits.read_limit_bytes)

    def _reporting(self, tool: str, path: str) -> contextlib.AbstractContextManager[None]:
        return reporting(self._on_violation, self.policy.role.value, tool, path)


@contextlib.contextmanager
def _writing(relative: PurePosixPath) -> Iterator[None]:
    """OS failures while writing, as tool errors that name only the relative path."""
    try:
        yield
    except ToolError:
        raise
    except FileNotFoundError:
        # The directory held by descriptor was removed or replaced mid-write.
        raise PathChanged(f"{relative} changed while it was being written") from None
    except OSError as exc:
        raise ToolError(f"{relative} cannot be written: {exc.strerror}") from None


def _read_all(fd: int, status: os.stat_result, relative: PurePosixPath, limit: int) -> bytes:
    if status.st_size > limit:
        raise TooLarge(f"{relative} is {status.st_size} bytes, over the {limit}-byte read limit")
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(fd, 1 << 16):
        size += len(chunk)
        if size > limit:  # it grew while being read
            raise TooLarge(f"{relative} is over the {limit}-byte read limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(data: bytes, relative: PurePosixPath) -> str:
    if b"\x00" in data:
        raise NotText(f"{relative} is binary")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise NotText(f"{relative} is not UTF-8 text") from None


@contextlib.contextmanager
def _temporary(parent: int, data: bytes, *, mode: int) -> Iterator[str]:
    """A durable temporary file in ``parent`` holding ``data``; removed afterwards unless
    the caller renamed it away."""
    name = f".msa-{secrets.token_hex(8)}.tmp"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
    try:
        try:
            os.fchmod(fd, mode)  # the umask must not change an edited file's mode
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        yield name
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent)


def _fsync_dir(fd: int) -> None:
    os.fsync(fd)

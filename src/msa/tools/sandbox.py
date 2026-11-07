"""Commands in the local worktree sandbox: allowlisted, typed, bounded (PLAN §8, ADR-009).

The model never sends a command line. It names an allowlisted command and passes typed
arguments, and each path argument is confined like any tool path. A command runs:

- with a scrubbed environment: only ``[sandbox].env_passthrough``, which can never name a
  credential-like variable, plus fixed settings below;
- in its own process group, killed whole on timeout or cancellation;
- under ``RLIMIT_CPU``, ``RLIMIT_FSIZE`` and no core dumps. The limits are set by a fixed
  trampoline that then ``exec``s the command; ``preexec_fn`` is unsafe once threads exist;
- with network traffic steered to a dead proxy, and package tools set offline, unless the
  command needs the network. That stops well-behaved clients only. **This sandbox is not a
  network or syscall boundary**: repository code runs with the user's privileges.
  Untrusted repositories need ``ContainerSandbox`` (M10).

Output keeps its head and tail within ``output_limit_bytes``; the middle is elided and
counted, so a failing test's summary at the end is never lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from msa.config import SandboxConfig
from msa.tools.audit import Report, log_violation, reporting
from msa.tools.errors import CommandRejected, CommandUnavailable
from msa.tools.paths import Root

type CommandName = Literal["pytest", "ruff_check", "ruff_format_check", "mypy"]
COMMANDS: dict[CommandName, tuple[str, ...]] = {
    "pytest": ("pytest", "-q"),
    "ruff_check": ("ruff", "check", "--no-fix"),
    "ruff_format_check": ("ruff", "format", "--check", "--diff"),
    "mypy": ("mypy",),
}
DEAD_PROXY = "http://127.0.0.1:9"  # the discard port: connections fail fast
_REVISION = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_./@{}~^:-]{0,199}\Z")
# Runs as `python -I -S -c _LIMITS <cpu> <fsize> <program> <args...>`: lowers the limits
# (never above an inherited hard limit, which would fail) and becomes the program.
_LIMITS = """
import os, resource, sys
def cap(kind, value):
    hard = resource.getrlimit(kind)[1]
    value = value if hard == resource.RLIM_INFINITY else min(value, hard)
    resource.setrlimit(kind, (value, value))
cap(resource.RLIMIT_CPU, int(sys.argv[1]))
cap(resource.RLIMIT_FSIZE, int(sys.argv[2]))
cap(resource.RLIMIT_CORE, 0)
os.execv(sys.argv[3], sys.argv[3:])
"""


@dataclass(frozen=True, slots=True)
class Completed:
    command: str  # for display: the argv as run, minus the launcher and trampoline
    exit_code: int | None  # None when killed for the timeout
    seconds: float
    stdout: str
    stderr: str
    elided_bytes: int

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None

    def render(self) -> str:
        if self.timed_out:
            status = f"timed out after {self.seconds:.0f}s and was killed"
        elif self.exit_code is not None and self.exit_code < 0:
            status = f"killed by signal {-self.exit_code} after {self.seconds:.1f}s"
        else:
            status = f"exit {self.exit_code} after {self.seconds:.1f}s"
        parts = [f"$ {self.command}", status]
        if self.elided_bytes:
            parts.append(f"[{self.elided_bytes} bytes of output elided from the middle]")
        parts.extend(text.rstrip("\n") for text in (self.stdout, self.stderr) if text.strip())
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class Match:
    path: str
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    matches: tuple[Match, ...]
    more: bool  # stopped at the match limit

    def render(self) -> str:
        if not self.matches:
            return "no matches"
        lines = [f"{m.path}:{m.line}: {m.text}" for m in self.matches]
        if self.more:
            lines.append(f"[stopped after {len(self.matches)} matches; narrow the search]")
        return "\n".join(lines)


class LocalSandbox:
    def __init__(
        self,
        root: Root,
        config: SandboxConfig,
        *,
        role: str = "unknown",
        on_violation: Report = log_violation,
    ) -> None:
        self.root = root
        self.config = config
        self.role = role
        self._on_violation = on_violation

    # ---------- tools

    async def run(
        self, command: CommandName, paths: Sequence[str] = (), keyword: str | None = None
    ) -> Completed:
        if command not in COMMANDS:
            raise CommandRejected(f"{command!r} is not an allowed command: {sorted(COMMANDS)}")
        argv = [*COMMANDS[command]]
        if keyword is not None:
            if command != "pytest":
                raise CommandRejected("keyword applies to pytest only")
            argv.append(f"-k{_bounded(keyword, 'keyword')}")
        argv.extend(self._path_args(paths))  # "./x" can never parse as an option
        return await self.execute([*self.config.launcher, *argv], shown=" ".join(argv))

    async def search(self, pattern: str, glob: str | None = None) -> SearchResult:
        """ripgrep: linear-time regexes, so a hostile pattern cannot hang the run, and it
        honours .gitignore and skips binary files. It never follows symlinks. Results are
        sorted: a tool result feeds the next request, whose digest must be reproducible for
        a resumed invocation to replay its paid turns (ADR-008)."""
        argv = ["rg", "--json", "--no-config", "--sort=path", "--max-filesize", "1M"]
        argv += ["--max-columns", "300"]
        argv += ["--max-columns-preview", "--regexp", _bounded(pattern, "pattern")]
        if glob is not None:
            argv += ["--glob", _bounded(glob, "glob")]
        done = await self.execute([*argv, "--", "."], shown="rg", merge=False)
        if done.exit_code not in {0, 1}:  # 1: no match
            raise CommandRejected(f"search failed: {done.stderr.strip() or done.render()}")
        limit = self.config.search_limit_matches
        found: list[Match] = []
        for line in done.stdout.splitlines():
            match = _rg_match(line)
            if match is None:
                continue
            if len(found) == limit:
                return SearchResult(tuple(found), more=True)
            found.append(match)
        return SearchResult(tuple(found), more=bool(done.elided_bytes))

    async def git_diff(self, base: str | None = None) -> Completed:
        """Changes against ``base`` (default HEAD), plus untracked files. Repository config
        cannot run programs here: external diff drivers, textconv and fsmonitor are off."""
        revision = "HEAD" if base is None else base
        if not _REVISION.match(revision) or ".." in revision:
            raise CommandRejected(f"{revision!r} is not a revision this tool accepts")
        git = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
        diff = await self.execute(
            [
                *git,
                "--no-pager",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                revision,
                "--",
            ],
            shown=f"git diff {revision}",
            merge=False,
        )
        if diff.exit_code != 0:
            raise CommandRejected(f"git diff failed: {diff.stderr.strip() or diff.render()}")
        untracked = await self.execute(
            [*git, "ls-files", "--others", "--exclude-standard"], shown="git ls-files", merge=False
        )
        new = untracked.stdout.strip()
        body = diff.stdout.rstrip("\n") or "no changes to tracked files"
        if new:
            body += "\n\nuntracked files:\n" + new
        return Completed(diff.command, 0, diff.seconds, body, "", diff.elided_bytes)

    # ---------- execution

    async def execute(
        self, argv: Sequence[str], *, shown: str, merge: bool = True, network: bool = False
    ) -> Completed:
        env = self._environment(network=network)
        program = shutil.which(argv[0], path=env.get("PATH", ""))
        if program is None:
            raise CommandUnavailable(f"{argv[0]} is not installed on the sandbox PATH")
        limits = [str(self.config.cpu_seconds), str(self.config.max_file_bytes)]
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            "-c",
            _LIMITS,
            *limits,
            program,
            *argv[1:],
            cwd=self.root.path,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge else asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, so a kill reaches every child
        )
        half = self.config.output_limit_bytes // 2
        out, err = _Capped(half), _Capped(half)
        readers = [_drain(process.stdout, out)]
        if not merge:
            readers.append(_drain(process.stderr, err))
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                await asyncio.gather(*readers)
                exit_code: int | None = await process.wait()
        except TimeoutError:
            exit_code = None
        finally:
            _kill_group(process.pid)  # also whatever it left running in the background
            if process.returncode is None:
                await process.wait()
        return Completed(
            command=shown,
            exit_code=exit_code,
            seconds=time.monotonic() - started,
            stdout=out.text(),
            stderr=err.text(),
            elided_bytes=out.elided + err.elided,
        )

    def _environment(self, *, network: bool) -> dict[str, str]:
        env = {k: os.environ[k] for k in self.config.env_passthrough if k in os.environ}
        env |= {
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "NO_COLOR": "1",
        }
        if not network:
            proxies = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
            env |= dict.fromkeys(proxies, DEAD_PROXY)
            env |= {name.lower(): DEAD_PROXY for name in proxies}
            local = "localhost,127.0.0.1,::1"  # tests that serve on localhost still work
            env |= {"NO_PROXY": local, "no_proxy": local, "UV_OFFLINE": "1", "PIP_NO_INDEX": "1"}
        return env

    def _path_args(self, paths: Sequence[str]) -> list[str]:
        resolved = []
        for path in paths:
            with reporting(self._on_violation, self.role, "run", path):
                resolved.append(self.root.resolve(path))
        return ["./" + r.as_posix() if r.as_posix() != "." else "." for r in resolved]


class _Capped:
    """The first and last ``half`` bytes of a stream, counting what falls between."""

    def __init__(self, half: int) -> None:
        self.half = half
        self.head = bytearray()
        self.tail = bytearray()
        self.elided = 0

    def add(self, chunk: bytes) -> None:
        room = self.half - len(self.head)
        self.head += chunk[:room]
        self.tail += chunk[room:] if room > 0 else chunk
        if len(self.tail) > self.half:
            self.elided += len(self.tail) - self.half
            del self.tail[: len(self.tail) - self.half]

    def text(self) -> str:
        joiner = b"\n...\n" if self.elided else b""
        return (bytes(self.head) + joiner + bytes(self.tail)).decode("utf-8", "replace")


async def _drain(stream: asyncio.StreamReader | None, sink: _Capped) -> None:
    if stream is None:  # pragma: no cover - always a pipe here
        return
    while chunk := await stream.read(1 << 16):
        sink.add(chunk)


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _bounded(value: str, name: str) -> str:
    if not value or "\x00" in value or len(value) > 1000:
        raise CommandRejected(f"{name} must be 1-1000 characters without NUL")
    return value


def _rg_match(line: str) -> Match | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:  # a line cut by the output cap
        return None
    if event.get("type") != "match":
        return None
    data = event["data"]
    path, text = data["path"].get("text"), data["lines"].get("text")
    if path is None or text is None:  # not UTF-8: rg sends base64 bytes instead
        return None
    return Match(path.removeprefix("./"), int(data["line_number"]), text.rstrip("\n"))

"""Pushing an approved release, and opening its pull request (ADR-018).

Only after a person approved, and only when the approval asked for it (``[publish].mode``,
or ``msa approve --publish``). The release is already committed to its local branch by
then, so nothing here can lose it. A push or a pull request that fails is recorded on the
publication with its error, never raised: the run completes with the release committed,
and the journal says what did not happen.

- **The push** is plain ``git push <remote> <commit>:refs/heads/<branch>``, with the user's
  own git configuration, so their credential helper or SSH key does the authentication.
  It is never forced: a remote branch holding something else stays as it is, and the
  push fails. Hooks are off, and git never prompts.
- **The pull request** goes through ``gh`` when it is installed and logged in, otherwise
  through the REST API with the token in ``$[publish].token_env``. The token travels in a
  request header only. No message recorded here contains it, or credentials in a URL.

Publishing again after a crash is safe. The push of the same commit is a no-op, and an
open pull request for the branch is found and reused, not opened twice.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from msa.config import PublishConfig
from msa.core.values import Publication, PublishMode

BODY_LIMIT = 65_000  # GitHub refuses a pull request body over 65,536 characters
_CREDENTIALS_IN_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_NAME = re.compile(r"^[\w.-]+$")


class PublishError(RuntimeError):
    """A push or a pull request did not happen. The message is safe to record."""


def scrub(text: str, secrets: Sequence[str] = ()) -> str:
    """``text`` without credentials in URLs, and without any of ``secrets``."""
    text = _CREDENTIALS_IN_URL.sub(r"\1***@", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def repository_of(url: str) -> str:
    """``owner/name`` from a GitHub remote URL: https, ssh, or scp-like ``git@host:o/n``."""
    path = url.strip().removesuffix("/").removesuffix(".git")
    parts = re.split(r"[/:]", path)
    if len(parts) >= 2 and _NAME.match(parts[-2]) and _NAME.match(parts[-1]):
        return f"{parts[-2]}/{parts[-1]}"
    raise PublishError(
        f"cannot tell the GitHub repository from the remote URL {scrub(url)!r}: "
        "set [publish].repository"
    )


def pr_body(markdown: str) -> str:
    """``pr.md`` as a pull request body: without its ``# title`` line, which is the pull
    request's title, and within GitHub's size limit."""
    lines = markdown.lstrip("\n").splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    body = "\n".join(lines).strip() + "\n"
    if len(body) <= BODY_LIMIT:
        return body
    note = "\n\n_(Truncated: the whole description is the release commit's message.)_\n"
    return body[: BODY_LIMIT - len(note)] + note


async def _run(
    argv: Sequence[str],
    *,
    timeout: float,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    """Run ``argv``; its exit code and its stdout and stderr together."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=dict(env) if env is not None else None,
        )
    except OSError as exc:
        raise PublishError(f"{argv[0]} could not start: {exc.strerror or exc}") from None
    try:
        out, _ = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise PublishError(f"{argv[0]} {argv[1]} timed out after {timeout:g}s") from None
    return process.returncode or 0, out.decode(errors="replace")


def _git_env() -> dict[str, str]:
    """The user's own environment and git configuration (their credentials), but never a
    prompt: a push that needs a password fails instead of waiting for one."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


async def push(
    repo: Path,
    remote: str,
    branch: str,
    commit: str,
    *,
    timeout: float,
    secrets: Sequence[str] = (),
) -> str:
    """Push ``commit`` to ``remote`` as ``branch``, never forced; the remote branch."""
    argv = [
        "git",
        "-C",
        str(repo),
        "-c",
        f"core.hooksPath={os.devnull}",
        "push",
        "--porcelain",
        remote,
        f"{commit}:refs/heads/{branch}",
    ]
    code, out = await _run(argv, timeout=timeout, env=_git_env())
    if code != 0:
        raise PublishError(f"git push to {remote} failed: {scrub(out.strip(), secrets)}")
    return branch


async def remote_url(repo: Path, remote: str, *, timeout: float) -> str:
    argv = ["git", "-C", str(repo), "remote", "get-url", remote]
    code, out = await _run(argv, timeout=timeout, env=_git_env())
    if code != 0:
        raise PublishError(f"no remote {remote!r} in {repo}: {scrub(out.strip())}")
    return out.strip()


class PullRequests(Protocol):
    """Where pull requests are opened. ``base`` empty: the repository's default branch."""

    async def find(self, head: str) -> str | None: ...

    async def create(self, *, title: str, body: str, head: str, base: str) -> str: ...


@dataclass(frozen=True)
class GhCli:
    """Pull requests through ``gh``, which holds its own login."""

    repository: str
    timeout: float
    gh: str = "gh"

    async def find(self, head: str) -> str | None:
        argv = [self.gh, "pr", "list", "--repo", self.repository, "--head", head]
        argv += ["--state", "open", "--json", "url", "--jq", ".[0].url // empty"]
        code, out = await _run(argv, timeout=self.timeout)
        if code != 0:
            raise PublishError(f"gh pr list failed: {scrub(out.strip())}")
        return out.strip() or None

    async def create(self, *, title: str, body: str, head: str, base: str) -> str:
        argv = [self.gh, "pr", "create", "--repo", self.repository, "--head", head]
        argv += ["--title", title, "--body-file", "-"]
        if base:
            argv += ["--base", base]
        code, out = await _run(argv, timeout=self.timeout, stdin=body)
        urls = [line for line in out.split() if line.startswith(("https://", "http://"))]
        if code != 0 or not urls:
            raise PublishError(f"gh pr create failed: {scrub(out.strip())}")
        return urls[-1]


@dataclass(frozen=True)
class GitHubRest:
    """Pull requests through the REST API, with a token. The token is in no repr and in
    no message: only in the Authorization header of each request."""

    api_url: str
    repository: str
    token: str = field(repr=False)
    timeout: float = 60.0

    async def find(self, head: str) -> str | None:
        owner = self.repository.split("/")[0]
        found = await self._call("GET", f"/pulls?head={quote(f'{owner}:{head}')}&state=open")
        return str(found[0]["html_url"]) if isinstance(found, list) and found else None

    async def create(self, *, title: str, body: str, head: str, base: str) -> str:
        if not base:
            base = str((await self._call("GET", ""))["default_branch"])
        payload = {"title": title, "body": body, "head": head, "base": base}
        return str((await self._call("POST", "/pulls", payload))["html_url"])

    async def _call(self, method: str, path: str, payload: object = None) -> Any:
        return await asyncio.to_thread(
            self._request, method, f"/repos/{self.repository}{path}", payload
        )

    def _request(self, method: str, path: str, payload: object) -> Any:
        request = urllib.request.Request(
            self.api_url.rstrip("/") + path,
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "msa-publish",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        where = f"GitHub API {method} {path.split('?', maxsplit=1)[0]}"
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            detail = _message(exc)
            raise PublishError(scrub(f"{where}: {exc.code} {detail}", [self.token])) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PublishError(scrub(f"{where}: {exc}", [self.token])) from None


def _message(error: urllib.error.HTTPError) -> str:
    try:
        found = json.loads(error.read() or b"{}")
    except ValueError:
        return error.reason or ""
    return str(found.get("message", "")) if isinstance(found, dict) else ""


async def pull_requests(
    config: PublishConfig,
    repo: Path,
    *,
    environ: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
) -> PullRequests:
    """``gh`` if it is installed and logged in, else the REST API with the token."""
    repository = config.repository or repository_of(
        await remote_url(repo, config.remote, timeout=config.timeout_seconds)
    )
    gh = which("gh")
    if gh is not None:
        code, _ = await _run([gh, "auth", "status"], timeout=config.timeout_seconds)
        if code == 0:
            return GhCli(repository, config.timeout_seconds, gh)
    token = environ.get(config.token_env, "")
    if token:
        return GitHubRest(config.api_url, repository, token, config.timeout_seconds)
    why = "not logged in" if gh is not None else "not installed"
    raise PublishError(
        f"cannot open a pull request: gh is {why}, and ${config.token_env} is not set"
    )


type Opener = Callable[[], Awaitable[PullRequests]]


async def publish(
    publication: Publication,
    *,
    repo: Path,
    config: PublishConfig,
    title: str,
    body: str,
    opener: Opener,
    secrets: Sequence[str] = (),
) -> Publication:
    """The committed release pushed, and its pull request opened, as ``publication.mode``
    asks. Never raises: what did not happen is recorded as the publication's error."""
    if publication.mode is PublishMode.LOCAL:
        return publication
    try:
        remote_branch = await push(
            repo,
            config.remote,
            publication.branch,
            publication.commit,
            timeout=config.timeout_seconds,
            secrets=secrets,
        )
    except Exception as exc:  # recorded, not raised: the release is committed either way
        return publication.model_copy(update={"error": _failure(exc, secrets)})
    pushed = publication.model_copy(
        update={"remote": config.remote, "remote_branch": remote_branch}
    )
    if publication.mode is PublishMode.PUSH:
        return pushed
    try:
        client = await opener()
        url = await client.find(publication.branch) or await client.create(
            title=title, body=pr_body(body), head=publication.branch, base=config.base
        )
    except Exception as exc:  # the branch is pushed; the pull request is what failed
        return pushed.model_copy(update={"error": _failure(exc, secrets)})
    return pushed.model_copy(update={"pr_url": url})


def _failure(exc: Exception, secrets: Sequence[str]) -> str:
    text = str(exc) if isinstance(exc, PublishError) else f"{type(exc).__name__}: {exc}"
    return scrub(text, secrets)


def describe(publication: Publication) -> str:
    """One line for the run's outcome: where the release is."""
    line = f"{publication.branch} at {publication.commit}"
    if publication.remote_branch:
        line += f", pushed to {publication.remote}/{publication.remote_branch}"
    if publication.pr_url:
        line += f", pull request {publication.pr_url}"
    if publication.error:
        line += f"; {publication.mode.value} did not complete: {publication.error}"
    return line

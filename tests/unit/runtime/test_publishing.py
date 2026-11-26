"""msa.runtime.publishing (ADR-018): pushing an approved release and opening its PR.

No real network: the remote is a bare repository on disk, ``gh`` is a script that records
how it was called, and the REST API is a server on the loopback interface.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import threading
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from msa.config import PublishConfig
from msa.core.values import Publication, PublishMode
from msa.runtime import publishing
from msa.runtime.publishing import GhCli, GitHubRest, PublishError

TOKEN = "ghp_" + "s3cr3t" * 6
IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@localhost",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@localhost",
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, **IDENTITY},
    )  # fmt: skip
    return done.stdout.strip()


@dataclass
class Repo:
    path: Path
    remote: Path
    commit: str


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """A repository with one commit, whose ``origin`` is a bare repository on disk."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "a.py").write_text("A = 1\n")
    git(path, "add", "a.py")
    git(path, "commit", "-qm", "base")
    git(path, "remote", "add", "origin", str(remote))
    return Repo(path, remote, git(path, "rev-parse", "HEAD"))


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


# ---------- the pieces


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octo/demo.git",
        "https://github.com/octo/demo",
        "https://github.com/octo/demo/",
        "git@github.com:octo/demo.git",
        "ssh://git@github.com/octo/demo.git",
        "https://x-access-token:abc@github.com/octo/demo.git",
    ],
)
def test_the_repository_is_read_from_any_github_remote_url(url: str) -> None:
    assert publishing.repository_of(url) == "octo/demo"


def test_a_remote_url_with_no_repository_in_it_asks_for_one() -> None:
    with pytest.raises(PublishError, match=r"set \[publish\]\.repository"):
        publishing.repository_of("demo")


def test_credentials_in_urls_and_known_secrets_are_scrubbed() -> None:
    text = f"fatal: https://user:{TOKEN}@github.com/o/r.git refused; token {TOKEN}"
    scrubbed = publishing.scrub(text, [TOKEN, ""])
    assert TOKEN not in scrubbed
    assert scrubbed == "fatal: https://***@github.com/o/r.git refused; token ***"


def test_the_pr_body_is_pr_md_without_its_title_and_within_githubs_limit() -> None:
    assert publishing.pr_body("# Add farewell\n\n## Summary\n\nIt says goodbye.\n") == (
        "## Summary\n\nIt says goodbye.\n"
    )
    assert publishing.pr_body("## Summary\nkept\n") == "## Summary\nkept\n"
    long = publishing.pr_body("# t\n\n" + "x" * 100_000)
    assert len(long) <= publishing.BODY_LIMIT
    assert long.endswith("_(Truncated: the whole description is the release commit's message.)_\n")


# ---------- the push


def test_a_push_puts_the_commit_on_the_remote_branch_and_is_safe_to_repeat(repo: Repo) -> None:
    for _ in range(2):  # again, as after a crash: nothing changes
        pushed = run(publishing.push(repo.path, "origin", "aegis/run-1", repo.commit, timeout=30))
        assert pushed == "aegis/run-1"
        assert git(repo.remote, "rev-parse", "refs/heads/aegis/run-1") == repo.commit


def test_a_push_never_moves_a_remote_branch_that_holds_something_else(repo: Repo) -> None:
    run(publishing.push(repo.path, "origin", "aegis/run-1", repo.commit, timeout=30))
    (repo.path / "b.py").write_text("B = 2\n")
    git(repo.path, "add", "b.py")
    git(repo.path, "commit", "-qm", "other", "--amend")  # not a descendant: a rewrite
    other = git(repo.path, "rev-parse", "HEAD")
    with pytest.raises(PublishError, match="git push to origin failed"):
        run(publishing.push(repo.path, "origin", "aegis/run-1", other, timeout=30))
    assert git(repo.remote, "rev-parse", "refs/heads/aegis/run-1") == repo.commit


def test_a_push_to_a_remote_that_does_not_exist_fails_with_a_reason(repo: Repo) -> None:
    with pytest.raises(PublishError, match="git push to nowhere failed"):
        run(publishing.push(repo.path, "nowhere", "aegis/run-1", repo.commit, timeout=30))


# ---------- gh


def fake_gh(tmp_path: Path, *, auth: int = 0, existing: str = "") -> Path:
    """A ``gh`` that records each call's arguments and standard input as JSON lines."""
    log = tmp_path / "gh.log"
    script = tmp_path / "gh"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
body = sys.stdin.read() if "--body-file" in args else None
with open({str(log)!r}, "a") as f:
    f.write(json.dumps({{"args": args, "stdin": body}}) + "\\n")
if args[:2] == ["auth", "status"]:
    sys.exit({auth})
if args[:2] == ["pr", "list"]:
    print({existing!r})
if args[:2] == ["pr", "create"]:
    print("https://github.com/octo/demo/pull/9")
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def gh_calls(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "gh.log").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_gh_opens_the_pull_request_with_the_body_on_stdin(tmp_path: Path) -> None:
    gh = GhCli("octo/demo", 30, str(fake_gh(tmp_path)))
    assert run(gh.find("aegis/run-1")) is None
    url = run(gh.create(title="Add farewell", body="## Summary\n", head="aegis/run-1", base=""))
    assert url == "https://github.com/octo/demo/pull/9"
    listed, created = gh_calls(tmp_path)
    assert listed["args"][:6] == ["pr", "list", "--repo", "octo/demo", "--head", "aegis/run-1"]
    assert created["args"] == [
        "pr", "create", "--repo", "octo/demo", "--head", "aegis/run-1",
        "--title", "Add farewell", "--body-file", "-",
    ]  # fmt: skip
    assert created["stdin"] == "## Summary\n"


def test_gh_is_given_the_base_branch_only_when_one_is_configured(tmp_path: Path) -> None:
    gh = GhCli("octo/demo", 30, str(fake_gh(tmp_path)))
    run(gh.create(title="t", body="b", head="aegis/run-1", base="develop"))
    (created,) = gh_calls(tmp_path)
    assert created["args"][-2:] == ["--base", "develop"]


def test_gh_finds_an_open_pull_request_for_the_branch(tmp_path: Path) -> None:
    gh = GhCli("octo/demo", 30, str(fake_gh(tmp_path, existing="https://github.com/o/r/pull/3")))
    assert run(gh.find("aegis/run-1")) == "https://github.com/o/r/pull/3"


# ---------- the REST API


@dataclass
class FakeGitHub:
    """A GitHub API on 127.0.0.1 that records what it was sent."""

    pulls: list[dict[str, Any]] = field(default_factory=list)
    requests: list[tuple[str, str, dict[str, str], Any]] = field(default_factory=list)
    refuse: tuple[int, str] | None = None
    url: str = ""


@pytest.fixture
def github() -> Iterator[FakeGitHub]:
    fake = FakeGitHub()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            fake.requests.append(("GET", self.path, dict(self.headers), None))
            if self.path.startswith("/repos/octo/demo/pulls"):
                self._send(200, [p for p in fake.pulls if p["state"] == "open"])
            else:
                self._send(200, {"default_branch": "trunk"})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fake.requests.append(("POST", self.path, dict(self.headers), body))
            if fake.refuse:
                self._send(fake.refuse[0], {"message": fake.refuse[1]})
                return
            pull = {"html_url": f"https://github.com/octo/demo/pull/{len(fake.pulls) + 1}"}
            fake.pulls.append(pull | {"state": "open"})
            self._send(201, pull)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake
    server.shutdown()
    server.server_close()


def test_the_rest_api_opens_a_pull_request_with_the_token_in_the_header_only(
    github: FakeGitHub,
) -> None:
    api = GitHubRest(github.url, "octo/demo", TOKEN, 30)
    assert TOKEN not in repr(api)
    assert run(api.find("aegis/run-1")) is None
    url = run(api.create(title="Add farewell", body="b", head="aegis/run-1", base=""))
    assert url == "https://github.com/octo/demo/pull/1"
    (find, repository, create) = github.requests
    assert find[1] == "/repos/octo/demo/pulls?head=octo%3Aaegis/run-1&state=open"
    assert repository[1] == "/repos/octo/demo"  # no base configured: the default branch
    assert create[3] == {
        "title": "Add farewell",
        "body": "b",
        "head": "aegis/run-1",
        "base": "trunk",
    }
    assert all(r[2]["Authorization"] == f"Bearer {TOKEN}" for r in github.requests)
    assert run(api.find("aegis/run-1")) == url  # found again, as after a crash


def test_a_refusal_from_the_api_is_reported_without_the_token(github: FakeGitHub) -> None:
    github.refuse = (422, f"Validation Failed: bad credentials {TOKEN}")
    api = GitHubRest(github.url, "octo/demo", TOKEN, 30)
    with pytest.raises(PublishError) as raised:
        run(api.create(title="t", body="b", head="aegis/run-1", base="main"))
    message = str(raised.value)
    assert TOKEN not in message
    assert message == (
        "GitHub API POST /repos/octo/demo/pulls: 422 Validation Failed: bad credentials ***"
    )


def test_an_api_that_cannot_be_reached_is_reported(tmp_path: Path) -> None:
    api = GitHubRest("http://127.0.0.1:9", "octo/demo", TOKEN, 5)
    with pytest.raises(PublishError, match="GitHub API GET /repos/octo/demo/pulls"):
        run(api.find("aegis/run-1"))


# ---------- choosing gh or the API


def test_gh_is_used_when_it_is_installed_and_logged_in(repo: Repo, tmp_path: Path) -> None:
    gh = str(fake_gh(tmp_path, auth=0))
    config = PublishConfig(repository="octo/demo")
    chosen = run(publishing.pull_requests(config, repo.path, environ={}, which=lambda _: gh))
    assert chosen == GhCli("octo/demo", 60.0, gh)


def test_the_api_is_used_when_gh_is_not_logged_in_and_a_token_is_set(
    repo: Repo, tmp_path: Path
) -> None:
    gh = str(fake_gh(tmp_path, auth=1))
    config = PublishConfig(repository="octo/demo", token_env="GH_PAT")
    chosen = run(
        publishing.pull_requests(config, repo.path, environ={"GH_PAT": TOKEN}, which=lambda _: gh)
    )
    assert chosen == GitHubRest("https://api.github.com", "octo/demo", TOKEN, 60.0)


def test_with_neither_gh_nor_a_token_it_says_what_is_missing(repo: Repo) -> None:
    config = PublishConfig(repository="octo/demo")
    with pytest.raises(PublishError, match=r"gh is not installed, and \$GITHUB_TOKEN is not set"):
        run(publishing.pull_requests(config, repo.path, environ={}, which=lambda _: None))


def test_the_repository_comes_from_the_remote_url_unless_configured(repo: Repo) -> None:
    git(repo.path, "remote", "set-url", "origin", "git@github.com:octo/from-remote.git")
    chosen = run(
        publishing.pull_requests(
            PublishConfig(), repo.path, environ={"GITHUB_TOKEN": TOKEN}, which=lambda _: None
        )
    )
    assert isinstance(chosen, GitHubRest)
    assert chosen.repository == "octo/from-remote"


# ---------- publishing a release


@dataclass
class Recorder:
    """Pull requests in memory: what was found and created."""

    open: dict[str, str] = field(default_factory=dict)
    created: list[dict[str, str]] = field(default_factory=list)
    fail: Exception | None = None

    async def find(self, head: str) -> str | None:
        if self.fail:
            raise self.fail
        return self.open.get(head)

    async def create(self, *, title: str, body: str, head: str, base: str) -> str:
        self.created.append({"title": title, "body": body, "head": head, "base": base})
        self.open[head] = f"https://github.com/octo/demo/pull/{len(self.created)}"
        return self.open[head]


def publish(repo: Repo, mode: PublishMode, recorder: Recorder, **config: Any) -> Publication:
    async def opener() -> Recorder:
        return recorder

    return run(
        publishing.publish(
            Publication(mode=mode, branch="aegis/run-1", commit=repo.commit),
            repo=repo.path,
            config=PublishConfig(**config),
            title="Add farewell",
            body="# Add farewell\n\n## Summary\n",
            opener=opener,
            secrets=[TOKEN],
        )
    )


def test_a_local_release_goes_nowhere(repo: Repo) -> None:
    recorder = Recorder()
    published = publish(repo, PublishMode.LOCAL, recorder)
    local = Publication(mode=PublishMode.LOCAL, branch="aegis/run-1", commit=repo.commit)
    assert published == local
    assert git(repo.remote, "branch", "--list") == ""
    assert recorder.created == []


def test_push_mode_pushes_and_opens_nothing(repo: Repo) -> None:
    recorder = Recorder()
    published = publish(repo, PublishMode.PUSH, recorder)
    assert (published.remote, published.remote_branch, published.pr_url, published.error) == (
        "origin",
        "aegis/run-1",
        None,
        None,
    )
    assert git(repo.remote, "rev-parse", "refs/heads/aegis/run-1") == repo.commit
    assert recorder.created == []


def test_pr_mode_pushes_then_opens_one_pull_request_however_often_it_runs(repo: Repo) -> None:
    recorder = Recorder()
    first = publish(repo, PublishMode.PR, recorder, base="develop")
    again = publish(repo, PublishMode.PR, recorder, base="develop")  # after a crash
    assert first == again
    assert first.pr_url == "https://github.com/octo/demo/pull/1"
    assert recorder.created == [
        {"title": "Add farewell", "body": "## Summary\n", "head": "aegis/run-1", "base": "develop"}
    ]


def test_a_push_that_fails_is_recorded_and_no_pull_request_is_tried(repo: Repo) -> None:
    recorder = Recorder()
    published = publish(repo, PublishMode.PR, recorder, remote="nowhere")
    assert published.remote is None
    assert published.pr_url is None
    assert (published.error or "").startswith("git push to nowhere failed")
    assert recorder.created == []


def test_a_pull_request_that_fails_is_recorded_after_the_push(repo: Repo) -> None:
    recorder = Recorder(fail=PublishError(f"GitHub said no to {TOKEN}"))
    published = publish(repo, PublishMode.PR, recorder)
    assert published.remote_branch == "aegis/run-1"  # the push happened
    assert published.pr_url is None
    assert published.error == "GitHub said no to ***"


def test_an_unexpected_error_is_recorded_too_with_its_type(repo: Repo) -> None:
    published = publish(repo, PublishMode.PR, Recorder(fail=KeyError("html_url")))
    assert published.error == "KeyError: 'html_url'"


def test_the_outcome_line_says_where_the_release_went() -> None:
    local = Publication(mode=PublishMode.LOCAL, branch="aegis/r", commit="c0ffee")
    assert publishing.describe(local) == "aegis/r at c0ffee"
    opened = Publication(
        mode=PublishMode.PR,
        branch="aegis/r",
        commit="c0ffee",
        remote="origin",
        remote_branch="aegis/r",
        pr_url="https://github.com/o/r/pull/1",
    )
    assert publishing.describe(opened) == (
        "aegis/r at c0ffee, pushed to origin/aegis/r, pull request https://github.com/o/r/pull/1"
    )
    failed = Publication(mode=PublishMode.PUSH, branch="aegis/r", commit="c0ffee", error="denied")
    assert publishing.describe(failed) == "aegis/r at c0ffee; push did not complete: denied"

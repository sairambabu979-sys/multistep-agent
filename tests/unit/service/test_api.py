"""The HTTP API (ADR-015 §3, §4): tokens and roles, every human gate through the core with
the token's person on the event, the audit log, the live stream, and its refusals.

Runs are simulated, except that every question for a person parks, as it does in a live
run, so each gate is answered here through the API."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from msa.core.effects import Effect, RequestArbitration, RunIntegrationTests, RunNegotiationRound
from msa.core.events import (
    AnyEvent,
    ArbitrationRuled,
    IntegrationTestsFailed,
    NegotiationRoundCompleted,
)
from msa.core.ids import RunId
from msa.core.values import FailureKind, Severity, Triage
from msa.runtime.clock import LogicalClock
from msa.runtime.interpreter import Interpreter, Parked
from msa.runtime.journal import SqliteJournal
from msa.runtime.simulation import Profile, SimulatedEnvironment, SimulationConfig
from msa.service.app import Settings, create_app
from msa.service.tokens import Role, TokenError, TokenStore
from tests.support.driver import Driver
from tests.support.factory import BUILD, digest, finding, limits

HAPPY = SimulationConfig(seed=0, profile=Profile.HAPPY)


class People:
    """The happy simulator, except that every question for a person parks."""

    def __init__(self) -> None:
        self.happy = SimulatedEnvironment(HAPPY)

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        if effect.command.human:
            raise Parked(f"waiting for a person: {effect.command.kind}")
        return await self.happy.execute(effect, now=now)


@asynccontextmanager
async def parking(
    _: RunId, __: dict[str, Any], journal: SqliteJournal
) -> AsyncIterator[Interpreter]:
    yield Interpreter(journal, People(), clock=LogicalClock())


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("MSA_USER_SKILLS", str(tmp_path / "none"))
    return tmp_path / "state"


@pytest.fixture
def tokens(state_dir: Path) -> dict[str, str]:
    store = TokenStore(state_dir / "tokens.json")
    return {role.name.lower(): store.issue(f"{role.name.lower()}-person", role) for role in Role}


@pytest.fixture
def api(state_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = Settings(repo=repo, state_dir=state_dir, poll_seconds=0.01, clock=LogicalClock())
    with TestClient(create_app(settings, factory=parking)) as client:
        yield client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wait_until_parked(api: TestClient, token: str, run_id: str) -> dict[str, Any]:
    """The run once its driver has stopped: parked on a person, or over."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body: dict[str, Any] = api.get(f"/api/runs/{run_id}", headers=auth(token)).json()
        if not body["driving"]:
            return body
        time.sleep(0.02)
    raise AssertionError(f"{run_id} is still being driven")


def start(api: TestClient, token: str, run_id: str = "r1") -> dict[str, Any]:
    response = api.post(
        "/api/runs",
        json={"task": "Add farewell()", "run_id": run_id, "simulate": {"seed": 0}},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    return wait_until_parked(api, token, run_id)


def journaled(state_dir: Path, d: Driver) -> None:
    """A run with ``d``'s history, as if it had been driven to here."""
    with SqliteJournal(state_dir / "runs.db") as journal:
        journal.create(HAPPY.model_dump(mode="json"), d.events[0], d.steps[0].state)  # type: ignore[arg-type]
        for event, step in zip(d.events[1:], d.steps[1:], strict=True):
            journal.append(event, step.state, snapshot=False)


YEARS = limits(wall_clock=timedelta(days=3650))


# ---------- tokens and roles


def test_health_needs_no_token_and_everything_else_does(api: TestClient, state_dir: Path) -> None:
    assert api.get("/api/health").json()["status"] == "ok"
    for request in (api.get("/api/runs"), api.get("/api/runs", headers=auth("msa_not-a-token"))):
        assert request.status_code == 401
    entries = (state_dir / "audit.jsonl").read_text().splitlines()
    assert len(entries) == 2
    assert '"principal": "anonymous"' in entries[0]
    assert "msa_not-a-token" not in "\n".join(entries)  # tokens never reach the log


def test_each_role_can_do_exactly_what_it_should(api: TestClient, tokens: dict[str, str]) -> None:
    assert api.get("/api/me", headers=auth(tokens["viewer"])).json() == {
        "name": "viewer-person",
        "role": "viewer",
    }
    denied = api.post(
        "/api/runs", json={"task": "x", "simulate": {}}, headers=auth(tokens["viewer"])
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "this needs the operator role"
    assert api.get("/api/audit", headers=auth(tokens["approver"])).status_code == 403
    log = api.get("/api/audit", headers=auth(tokens["admin"])).json()
    assert {(e["principal"], e["status"]) for e in log} >= {
        ("viewer-person", 403),
        ("approver-person", 403),
    }


def test_tokens_are_for_people_and_stored_only_as_hashes(state_dir: Path) -> None:
    store = TokenStore(state_dir / "tokens.json")
    with pytest.raises(TokenError, match="tokens are for people"):
        store.issue("agent:coder", Role.ADMIN)
    token = store.issue("grace", Role.APPROVER)
    assert token.startswith("msa_")
    assert token not in (state_dir / "tokens.json").read_text()
    assert (state_dir / "tokens.json").stat().st_mode & 0o077 == 0
    principal = store.verify(token)
    assert principal is not None
    assert (principal.name, principal.role) == ("grace", Role.APPROVER)
    with pytest.raises(TokenError, match="already has a token"):
        store.issue("grace", Role.VIEWER)
    store.revoke("grace")
    assert store.verify(token) is None


# ---------- a run, its gates, its stream


def test_a_run_parks_for_release_approval_and_an_approver_publishes_it(
    api: TestClient, tokens: dict[str, str], state_dir: Path
) -> None:
    parked = start(api, tokens["operator"])
    assert parked["phase"] == "human_approval"
    assert parked["pending"] == ["await_release_approval"]
    assert parked["waiting_on_person"] is True

    refused = api.post("/api/runs/r1/approve", headers=auth(tokens["operator"]))
    assert refused.status_code == 403
    approved = api.post("/api/runs/r1/approve", headers=auth(tokens["approver"]))
    assert approved.status_code == 200, approved.text
    assert approved.json()["event"]["kind"] == "release_approved"
    assert approved.json()["event"]["by"] == "approver-person"  # the token's person

    final = wait_until_parked(api, tokens["viewer"], "r1")
    assert final["phase"] == "completed"
    assert final["state"]["approved_by"] == "approver-person"
    log = api.get("/api/audit", headers=auth(tokens["admin"])).json()
    assert [(e["action"], e["status"]) for e in log if e["principal"] == "approver-person"] == [
        ("approve", 200)
    ]


def test_a_rejected_release_goes_back_for_rework_and_returns_for_approval(
    api: TestClient, tokens: dict[str, str]
) -> None:
    start(api, tokens["operator"])
    sent_back = api.post(
        "/api/runs/r1/reject", json={"feedback": "rename it"}, headers=auth(tokens["approver"])
    )
    assert sent_back.status_code == 200, sent_back.text
    assert sent_back.json()["run"]["phase"] == "rework"
    again = wait_until_parked(api, tokens["viewer"], "r1")
    assert again["phase"] == "human_approval"
    assert again["state"]["revisions"] == 1


def test_answers_the_run_is_not_waiting_for_are_refused_by_the_core(
    api: TestClient, tokens: dict[str, str]
) -> None:
    start(api, tokens["operator"])
    for path, body in (
        ("unblock", {"note": "x"}),
        ("rule", {"verdicts": {"c0-1": "upheld"}}),
        ("pause", {}),
    ):
        response = api.post(f"/api/runs/r1/{path}", json=body, headers=auth(tokens["approver"]))
        assert response.status_code == 409, (path, response.text)
    assert (
        "not waiting for await_unblock"
        in api.post("/api/runs/r1/unblock", json={}, headers=auth(tokens["operator"])).json()[
            "detail"
        ]
    )


def test_an_unblock_reruns_the_stage(
    api: TestClient, tokens: dict[str, str], state_dir: Path
) -> None:
    d = Driver(YEARS, run_id="b1")
    d.intake()
    d.plan()
    d.build()
    d.static()
    d.unit()
    d.complete(
        RunIntegrationTests,
        IntegrationTestsFailed,
        tree=BUILD,
        triage=Triage(kind=FailureKind.ENVIRONMENT, owner=None),
        report=digest("no db"),
    )
    journaled(state_dir, d)
    response = api.post(
        "/api/runs/b1/unblock", json={"note": "started it"}, headers=auth(tokens["operator"])
    )
    assert response.status_code == 200, response.text
    assert response.json()["event"]["by"] == "operator-person"
    assert wait_until_parked(api, tokens["viewer"], "b1")["phase"] == "human_approval"


def test_a_dispute_is_ruled_on_through_the_api(
    api: TestClient, tokens: dict[str, str], state_dir: Path
) -> None:
    d = Driver(YEARS, run_id="d1")
    d.to_review()
    d.review(finding("c0-1", Severity.BLOCKER))
    d.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    d.complete(RequestArbitration, ArbitrationRuled, resolutions=())
    journaled(state_dir, d)
    bad = api.post(
        "/api/runs/d1/rule",
        json={"verdicts": {"c0-1": "accepted"}},
        headers=auth(tokens["approver"]),
    )
    assert bad.status_code == 422
    ruled = api.post(
        "/api/runs/d1/rule",
        json={"verdicts": {"c0-1": "dismissed"}},
        headers=auth(tokens["approver"]),
    )
    assert ruled.status_code == 200, ruled.text
    assert wait_until_parked(api, tokens["viewer"], "d1")["phase"] == "human_approval"


def test_pause_and_resume_through_the_api(
    api: TestClient, tokens: dict[str, str], state_dir: Path
) -> None:
    d = Driver(YEARS, run_id="p1")
    d.intake()
    journaled(state_dir, d)
    paused = api.post(
        "/api/runs/p1/pause", json={"reason": "freeze"}, headers=auth(tokens["operator"])
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["run"]["phase"] == "paused"
    assert "pending" in paused.json()["run"]
    resumed = api.post("/api/runs/p1/resume", headers=auth(tokens["operator"]))
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["event"]["kind"] == "resumed"
    assert wait_until_parked(api, tokens["viewer"], "p1")["phase"] == "human_approval"


def test_the_stream_replays_every_event_then_ends(api: TestClient, tokens: dict[str, str]) -> None:
    start(api, tokens["operator"])
    api.post("/api/runs/r1/abort", json={"reason": "enough"}, headers=auth(tokens["approver"]))
    with api.stream("GET", "/api/runs/r1/stream", headers=auth(tokens["viewer"])) as response:
        text = "".join(response.iter_text())
    frames = [f for f in text.split("\n\n") if f.strip()]
    assert frames[0].startswith("id: 1\nevent: run_started\n")
    assert frames[-1] == 'event: end\ndata: {"phase": "cancelled"}'
    ids = [int(f.split("\n")[0].removeprefix("id: ")) for f in frames[:-1]]
    assert ids == list(range(1, len(ids) + 1))
    later = api.get("/api/runs/r1/events?after=3", headers=auth(tokens["viewer"])).json()
    assert later[0]["seq"] == 4


def test_unknown_runs_bad_ids_and_files_outside_the_allowlist_are_refused(
    api: TestClient, tokens: dict[str, str]
) -> None:
    viewer = auth(tokens["viewer"])
    assert api.get("/api/runs/nope", headers=viewer).status_code == 404
    assert api.get("/api/runs/..%2Fetc", headers=viewer).status_code in {404, 422}
    assert api.get("/api/runs/a.b", headers=viewer).status_code == 422
    assert api.get("/api/runs/r1/files/runs.db", headers=viewer).status_code == 404
    assert api.get("/api/runs/r1/records/../tokens", headers=viewer).status_code == 404
    assert api.post("/api/runs/nope/approve", headers=auth(tokens["approver"])).status_code == 404


def test_a_task_holding_a_secret_is_refused_and_not_stored(
    api: TestClient, tokens: dict[str, str], state_dir: Path
) -> None:
    key = "AKIA" + "Q3EGX7PLMR2TBN5W"
    response = api.post(
        "/api/runs",
        json={"task": f"deploy with {key}", "simulate": {}},
        headers=auth(tokens["operator"]),
    )
    assert response.status_code == 422
    assert key not in response.text
    assert key not in (state_dir / "audit.jsonl").read_text()
    assert api.get("/api/runs", headers=auth(tokens["viewer"])).json() == []


def test_the_workflow_is_the_transition_table(api: TestClient, tokens: dict[str, str]) -> None:
    body = api.get("/api/workflow", headers=auth(tokens["viewer"])).json()
    assert len(body["phases"]) == 20
    assert body["terminal"] == ["cancelled", "completed", "failed"]
    assert {"source": "human_approval", "event": "ReleasePublished", "targets": ["completed"]} in (
        body["rules"]
    )
    assert any(e["event"] == "PauseRequested" for e in body["escapes"])


# ---------- the reads a workbench needs, and the defaults


def test_runs_files_records_and_timeline_are_readable(
    api: TestClient, tokens: dict[str, str], state_dir: Path
) -> None:
    start(api, tokens["operator"])
    viewer = auth(tokens["viewer"])
    (listed,) = api.get("/api/runs", headers=viewer).json()
    assert (listed["run_id"], listed["phase"], listed["kind"]) == (
        "r1",
        "human_approval",
        "simulation",
    )
    lines = api.get("/api/runs/r1/timeline", headers=viewer).json()
    assert lines[0].strip().startswith("1  START -> CREATED")
    files = state_dir / "runs" / "r1"
    files.mkdir(parents=True)
    (files / "pr.md").write_text("# Add farewell\n", encoding="utf-8")
    (files / "findings.jsonl").write_text('{"id": "c0-1"}\n', encoding="utf-8")
    assert api.get("/api/runs/r1/files/pr.md", headers=viewer).text == "# Add farewell\n"
    assert api.get("/api/runs/r1/files/report.md", headers=viewer).status_code == 404
    assert api.get("/api/runs/r1/records/findings", headers=viewer).json() == [{"id": "c0-1"}]
    assert api.get("/api/runs/nope/timeline", headers=viewer).status_code == 404
    assert api.get("/api/runs/nope/events", headers=viewer).status_code == 404
    assert api.get("/api/runs/nope/stream", headers=viewer).status_code == 404


def test_skills_policy_and_preferences_are_readable(
    api: TestClient, tokens: dict[str, str]
) -> None:
    viewer = auth(tokens["viewer"])
    assert api.get("/api/skills", headers=viewer).json() == []
    assert api.get("/api/policy", headers=viewer).json()["locked"] == []
    preferences = api.get("/api/preferences", headers=viewer).json()
    bare = next(p for p in preferences if p["id"] == "py.errors.no-bare-except")
    assert (bare["enforcement"], bare["origin"]) == ("hard", "built-in defaults")


def test_cancel_and_abort_end_a_run(api: TestClient, tokens: dict[str, str]) -> None:
    start(api, tokens["operator"], "c1")
    cancelled = api.post("/api/runs/c1/cancel", json={}, headers=auth(tokens["operator"]))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["run"]["phase"] == "cancelled"
    assert cancelled.json()["event"]["reason"] == "cancelled by operator-person"
    again = api.post("/api/runs/c1/cancel", json={}, headers=auth(tokens["operator"]))
    assert again.status_code == 409
    start(api, tokens["operator"], "c2")
    aborted = api.post(
        "/api/runs/c2/abort", json={"reason": "no"}, headers=auth(tokens["approver"])
    )
    assert aborted.json()["run"]["outcome"]["detail"] == "approver-person: no"


def test_a_duplicate_run_id_and_a_live_run_without_settings_are_refused(
    api: TestClient, tokens: dict[str, str]
) -> None:
    start(api, tokens["operator"])
    operator = auth(tokens["operator"])
    twice = api.post(
        "/api/runs", json={"task": "x", "run_id": "r1", "simulate": {}}, headers=operator
    )
    assert twice.status_code == 422
    assert "already exists" in twice.json()["detail"]
    live = api.post("/api/runs", json={"task": "x"}, headers=operator)
    assert live.status_code == 422
    assert "live runs need msa.toml" in live.json()["detail"]


def test_the_default_factory_drives_a_simulated_run_to_completion(
    state_dir: Path, tmp_path: Path, tokens: dict[str, str]
) -> None:
    repo = tmp_path / "repo2"
    repo.mkdir()
    settings = Settings(
        repo=repo,
        state_dir=state_dir,
        poll_seconds=0.01,
        cors_origins=("http://localhost:3000",),
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/runs",
            json={"task": "x", "run_id": "s1", "simulate": {"seed": 3}, "budget_usd": 2.0},
            headers=auth(tokens["operator"]),
        )
        assert response.status_code == 200, response.text
        final = wait_until_parked(client, tokens["viewer"], "s1")
        assert final["phase"] in {"completed", "failed", "cancelled"}
        assert final["state"]["limits"]["budget_usd_micros"] == 2_000_000
        preflight = client.options(
            "/api/runs",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        assert preflight.headers["access-control-allow-origin"] == "http://localhost:3000"
        other = client.get("/api/health", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in other.headers

"""Seed a state directory for the workbench's end-to-end tests.

Usage: python web/tests/e2e/seed.py <dir>   (from the repository root)

Writes, under <dir>: an msa.toml whose API allows the test server's origin, tokens for a
viewer and an approver (and a JSON file the tests read them from), an empty repository,
and three journaled simulated runs: one waiting for release approval, one blocked on a
ruling, and one completed. Answering the first two through the workbench makes the
service drive them on with the simulator.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from msa.core.effects import RequestArbitration, RunNegotiationRound  # noqa: E402
from msa.core.events import ArbitrationRuled, NegotiationRoundCompleted  # noqa: E402
from msa.core.values import Severity  # noqa: E402
from msa.runtime.clock import LogicalClock  # noqa: E402
from msa.runtime.interpreter import Interpreter  # noqa: E402
from msa.runtime.journal import SqliteJournal  # noqa: E402
from msa.runtime.simulation import (  # noqa: E402
    Profile,
    SimulatedEnvironment,
    SimulationConfig,
    digest,
)
from msa.service.tokens import Role, TokenStore  # noqa: E402
from tests.support.driver import Driver  # noqa: E402
from tests.support.factory import finding, limits  # noqa: E402

HAPPY = SimulationConfig(seed=0, profile=Profile.HAPPY)
YEARS = limits(wall_clock=timedelta(days=3650))


def journal(path: Path, d: Driver) -> None:
    with SqliteJournal(path) as opened:
        opened.create(HAPPY.model_dump(mode="json"), d.events[0], d.steps[0].state)  # type: ignore[arg-type]
        for event, step in zip(d.events[1:], d.steps[1:], strict=True):
            opened.append(event, step.state, snapshot=True)


def main(target: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    (target / "repo").mkdir(parents=True)
    config = (ROOT / "msa.toml").read_text(encoding="utf-8")
    config = config.replace(
        'cors_origins = ["http://localhost:3000"]',
        'cors_origins = ["http://127.0.0.1:3100", "http://localhost:3100"]',
    )
    (target / "msa.toml").write_text(config, encoding="utf-8")
    store = TokenStore(target / "tokens.json")
    tokens = {
        "approver": store.issue("grace", Role.APPROVER),
        "viewer": store.issue("vera", Role.VIEWER),
    }
    (target / "e2e-tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
    runs = target / "runs.db"

    release = Driver(YEARS, run_id="release-1")
    release.to_review()
    release.review()
    release.verify_release()
    journal(runs, release)

    ruling = Driver(YEARS, run_id="ruling-1")
    ruling.to_review()
    ruling.review(finding("c0-1", Severity.BLOCKER))
    ruling.complete(RunNegotiationRound, NegotiationRoundCompleted, resolutions=())
    ruling.complete(RequestArbitration, ArbitrationRuled, resolutions=())
    journal(runs, ruling)

    with SqliteJournal(runs) as opened:
        done = Interpreter(opened, SimulatedEnvironment(HAPPY), clock=LogicalClock())
        asyncio.run(
            done.start(
                run_id="done-1",  # type: ignore[arg-type]
                task_digest=digest("t"),
                limits=YEARS,
                environment=HAPPY.model_dump(mode="json"),
            )
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())

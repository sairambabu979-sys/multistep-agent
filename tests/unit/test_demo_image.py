"""The demo's container image runs the tools uv.lock pins (tests/fixtures/images/demo).

The definition-of-done run gives the same verdicts in a container as on this machine only
if pytest and ruff are the same versions in both. A lockfile bump that leaves the image
behind fails here, not as a puzzling difference in a ruff or pytest report."""

from __future__ import annotations

import re
import tomllib

from tests.support.scripts import ROOT

DOCKERFILE = (ROOT / "tests/fixtures/images/demo/Dockerfile").read_text(encoding="utf-8")


def test_the_image_pins_the_locked_versions_of_its_tools() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock["package"]}
    pinned = dict(re.findall(r"\b(pytest|ruff)==([\w.]+)", DOCKERFILE))
    assert pinned == {"pytest": locked["pytest"], "ruff": locked["ruff"]}


def test_ci_builds_it_for_the_demo() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "docker build --tag msa-demo-sandbox tests/fixtures/images/demo" in workflow
    assert 'MSA_DEMO_IMAGE: "msa-demo-sandbox"' in workflow

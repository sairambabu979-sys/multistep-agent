"""Evidence integrity (ADR-013): a wrong change cannot finish DONE by making the Tester's
tests not run, not count, or pass on files that are not delivered.

Each test closes a hole that a review of M7 found. In all of them the plan's criterion is
``farewell("ada") == "goodbye ada"``, and the Coder ships a ``farewell`` that returns
"bye ada", with a unit test of its own that agrees with it. Before ADR-013, each of these
runs finished DONE.

Most runs stop at the first failure (``max_revisions = 0``): what matters is which rule
caught the change, and whose it is to fix. The first test follows a run to the end.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from msa.core.values import OutcomeReason, Phase
from msa.llm import fake
from tests.integration.test_m7_runs import flaky_test
from tests.support.repos import git
from tests.support.runs import (
    ACCEPTANCE,
    ACCEPTANCE_TEST,
    BASE_GREETER,
    FAREWELL_TEST,
    GOOD_GREETER,
    SUMMARY,
    TESTS_SUMMARY,
    Harness,
    RoleScripts,
    author_script,
    coder_script,
    create,
    edit,
    make_harness,
    planner_script,
    read,
    tool_turn,
)

WRONG = BASE_GREETER + '\n\ndef farewell(name: str) -> str:\n    return f"bye {name}"\n'
OWN_TEST = FAREWELL_TEST.replace("goodbye ada", "bye ada")  # agrees with the wrong code
TEST_ID = f"{ACCEPTANCE}::test_farewell_says_goodbye"
IMPL_BUG = {"kind": "impl_bug", "test_id": TEST_ID, "diagnosis": "farewell says bye."}


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    return make_harness(tmp_path)


def stop_at_first_failure(harness: Harness) -> Harness:
    limits = harness.config.limits.model_copy(update={"max_revisions": 0})
    harness.config = harness.config.model_copy(update={"limits": limits})
    return harness


def wrong_coder(*extra: dict[str, Any]) -> list[fake.Step]:
    return [
        tool_turn(read("c1", "src/greeter.py")),
        tool_turn(
            edit("c2", "src/greeter.py", BASE_GREETER, WRONG, BASE_GREETER),
            create("c3", "tests/test_farewell.py", OWN_TEST),
            *extra,
        ),
        fake.answer(SUMMARY),
    ]


def records(harness: Harness, name: str) -> list[dict[str, Any]]:
    text = (harness.run_dir / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def stopped(harness: Harness, client: RoleScripts) -> None:
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.FAILED, final.outcome
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.REVISION_LIMIT, final.outcome
    assert client.exhausted


# ---------- tests that do not run are not evidence


SKIPPED = ACCEPTANCE_TEST.replace(
    "from greeter import farewell\n\n\ndef",
    'import pytest\n\nfrom greeter import farewell\n\n\n@pytest.mark.skip(reason="later")\ndef',
)


def test_a_skipped_acceptance_test_goes_back_and_the_wrong_change_is_then_caught(
    harness: Harness,
) -> None:
    coder = [
        *wrong_coder(),
        tool_turn(
            edit("c4", "src/greeter.py", '"bye {name}"', '"goodbye {name}"', WRONG),
            edit("c5", "tests/test_farewell.py", "bye ada", "goodbye ada", OWN_TEST),
        ),
        fake.answer({**SUMMARY, "summary": "farewell now says goodbye."}),
    ]
    tester = [
        *author_script(SKIPPED),
        tool_turn(read("v1", ACCEPTANCE)),
        tool_turn(edit("v2", ACCEPTANCE, SKIPPED, ACCEPTANCE_TEST, SKIPPED)),
        fake.answer({**TESTS_SUMMARY, "summary": "The test runs now."}),
        fake.answer(IMPL_BUG),
    ]
    client = RoleScripts(planner=planner_script(), coder=coder, tester=tester)
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.DONE, final.outcome
    assert client.exhausted

    skipped, caught, passed = records(harness, "tests.jsonl")
    assert (skipped["verdict"], skipped["kind"]) == ("failed", "test_bug")
    report = harness.artifact(skipped["report"])
    assert f"| `{TEST_ID}` | fail | skipped (later) |" in report
    assert f"`{TEST_ID}` was skipped (later) on the head tree" in report
    # Once it runs, the wrong farewell fails it, and the Coder fixes the code.
    assert (caught["verdict"], caught["kind"]) == ("failed", "impl_bug")
    assert f"| `{TEST_ID}` | fail | fail |" in harness.artifact(caught["report"])
    assert passed["verdict"] == "passed"
    assert f"| `{TEST_ID}` | fail | pass |" in harness.artifact(passed["report"])


XFAILED = SKIPPED.replace('@pytest.mark.skip(reason="later")', "@pytest.mark.xfail")


def test_an_xfailed_acceptance_test_is_not_evidence(harness: Harness) -> None:
    client = RoleScripts(
        planner=planner_script(), coder=wrong_coder(), tester=author_script(XFAILED)
    )
    stopped(stop_at_first_failure(harness), client)
    (only,) = records(harness, "tests.jsonl")
    assert (only["verdict"], only["kind"]) == ("failed", "test_bug")
    assert f"`{TEST_ID}` was skipped (xfail: " in harness.artifact(only["report"])


def test_an_acceptance_module_without_tests_is_not_evidence(harness: Harness) -> None:
    empty = '"""Acceptance tests for farewell."""\n'
    client = RoleScripts(planner=planner_script(), coder=wrong_coder(), tester=author_script(empty))
    stopped(stop_at_first_failure(harness), client)
    (only,) = records(harness, "tests.jsonl")
    assert (only["verdict"], only["kind"]) == ("failed", "test_bug")
    assert f"`{ACCEPTANCE}` defines no tests" in harness.artifact(only["report"])


def test_tests_outside_a_test_module_are_not_acceptance_tests(harness: Harness) -> None:
    """pytest's default collection would never run ``tests/acceptance/goodbye.py``."""
    client = RoleScripts(
        planner=planner_script(),
        coder=wrong_coder(),
        tester=author_script(ACCEPTANCE_TEST, path="tests/acceptance/goodbye.py"),
    )
    stopped(stop_at_first_failure(harness), client)
    (only,) = records(harness, "tests.jsonl")
    assert (only["verdict"], only["kind"]) == ("failed", "test_bug")
    assert "No acceptance tests were written" in harness.artifact(only["report"])


# ---------- the Coder cannot switch the Tester's tests off from paths it owns


CONFTEST = (
    "import pytest\n\n\n"
    "def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:\n"
    "    for item in items:\n"
    '        if "acceptance" in item.nodeid:\n'
    '            item.add_marker(pytest.mark.skip(reason="not ready"))\n'
)


def test_a_conftest_above_the_acceptance_tests_is_refused_in_verification(
    harness: Harness,
) -> None:
    client = RoleScripts(
        planner=planner_script(),
        coder=wrong_coder(create("c4", "tests/conftest.py", CONFTEST)),
        tester=author_script(),
    )
    stopped(stop_at_first_failure(harness), client)
    (only,) = records(harness, "verifications.jsonl")
    assert (only["passed"], only["failed"]) == (False, ["pytest configuration"])
    report = harness.artifact(only["report"])
    assert "`tests/conftest.py` created: a conftest.py above the tests" in report
    assert not (harness.run_dir / "tests.jsonl").exists()  # it never reached TESTING


def test_pytest_configuration_that_ignores_the_acceptance_tests_is_refused(
    harness: Harness,
) -> None:
    pyproject = (harness.repo / "pyproject.toml").read_text(encoding="utf-8")
    old = 'addopts = ["-p", "no:cacheprovider"]'
    new = 'addopts = ["-p", "no:cacheprovider", "--ignore=tests/acceptance"]'
    assert old in pyproject
    client = RoleScripts(
        planner=planner_script(),
        coder=wrong_coder(
            edit("c4", "pyproject.toml", pyproject, pyproject.replace(old, new), pyproject)
        ),
        tester=author_script(),
    )
    stopped(stop_at_first_failure(harness), client)
    (only,) = records(harness, "verifications.jsonl")
    assert only["failed"] == ["pytest configuration"]
    report = harness.artifact(only["report"])
    assert "`pyproject.toml`: its pytest configuration was changed" in report


def test_a_conftest_beside_the_acceptance_tests_cannot_reach_them(harness: Harness) -> None:
    """A conftest.py that is not above the Tester's directories is the Coder's to write,
    but pytest hooks are global within a session: the acceptance tests run in their own."""
    client = RoleScripts(
        planner=planner_script(),
        coder=[
            tool_turn(read("c1", "src/greeter.py")),
            tool_turn(
                edit("c2", "src/greeter.py", BASE_GREETER, WRONG, BASE_GREETER),
                create("c3", "tests/unit/test_farewell.py", OWN_TEST),
                create("c4", "tests/unit/conftest.py", CONFTEST),
            ),
            fake.answer(SUMMARY),
        ],
        tester=[*author_script(), fake.answer(IMPL_BUG)],
    )
    stopped(stop_at_first_failure(harness), client)
    (verification,) = records(harness, "verifications.jsonl")
    assert verification["passed"]
    (only,) = records(harness, "tests.jsonl")
    assert (only["verdict"], only["kind"]) == ("failed", "impl_bug")
    assert f"| `{TEST_ID}` | fail | fail |" in harness.artifact(only["report"])


# ---------- checks see exactly the delivered tree


def test_code_that_needs_a_gitignored_file_fails_verification(harness: Harness) -> None:
    (harness.repo / ".gitignore").write_text("local_settings.py\n", encoding="utf-8")
    git(harness.repo, "add", ".")
    git(harness.repo, "commit", "-qm", "ignore local settings")
    greeter = (
        '"""Greetings."""\n\nfrom local_settings import FAREWELL\n\n\n'
        'def greet(name: str) -> str:\n    return f"hello {name}"\n\n\n'
        'def farewell(name: str) -> str:\n    return f"{FAREWELL} {name}"\n'
    )
    client = RoleScripts(
        planner=planner_script(),
        coder=[
            tool_turn(read("c1", "src/greeter.py")),
            tool_turn(
                create("c2", "src/local_settings.py", 'FAREWELL = "goodbye"\n'),
                edit("c3", "src/greeter.py", BASE_GREETER, greeter, BASE_GREETER),
                create("c4", "tests/test_farewell.py", FAREWELL_TEST),
            ),
            fake.answer(SUMMARY),
        ],
        tester=author_script(),
    )
    stopped(stop_at_first_failure(harness), client)
    (only,) = records(harness, "verifications.jsonl")
    assert only["failed"] == ["pytest"]
    report = harness.artifact(only["report"])
    assert "## Files outside the tree" in report
    assert "- `src/local_settings.py`" in report
    assert "No module named 'local_settings'" in report


# ---------- a test module that does not import is a failure to triage


def test_an_acceptance_test_of_code_that_was_never_written_goes_to_triage(
    harness: Harness,
) -> None:
    """The acceptance module fails to import on the head tree too: one failure for the
    module. It used to be rerun under a node id pytest does not know, which read as an
    environment failure that no agent could fix."""
    renamed = GOOD_GREETER.replace("def farewell(", "def goodbye(")
    triage = {"kind": "impl_bug", "test_id": ACCEPTANCE, "diagnosis": "farewell is missing."}
    client = RoleScripts(
        planner=planner_script(),
        coder=[
            tool_turn(read("c1", "src/greeter.py")),
            tool_turn(edit("c2", "src/greeter.py", BASE_GREETER, renamed, BASE_GREETER)),
            fake.answer(SUMMARY),
        ],
        tester=[*author_script(), fake.answer(triage)],
    )
    stopped(stop_at_first_failure(harness), client)
    (only,) = records(harness, "tests.jsonl")
    assert (only["verdict"], only["kind"]) == ("failed", "impl_bug")
    report = harness.artifact(only["report"])
    assert f"- `{ACCEPTANCE}`: collection failure" in report
    assert f"| `{TEST_ID}` | fail | fail |" in report


# ---------- the guard: a Tester revision may not skip what it was asked to fix


def test_a_tester_revision_that_skips_a_flaky_test_fails_the_run(harness: Harness) -> None:
    counter = f"msa-flaky-{uuid.uuid4().hex}"
    flaky = flaky_test(counter, deterministic=False)
    old = "def test_farewell_every_time"
    skipped = flaky.replace("import tempfile\n", "import tempfile\n\nimport pytest\n").replace(
        old, f'@pytest.mark.skip(reason="flaky")\n{old}'
    )
    client = RoleScripts(
        planner=planner_script(),
        coder=coder_script(GOOD_GREETER),
        tester=[
            *author_script(flaky),
            tool_turn(read("v1", ACCEPTANCE)),
            tool_turn(edit("v2", ACCEPTANCE, flaky, skipped, flaky)),
            fake.answer({**TESTS_SUMMARY, "summary": "Skipped the flaky test."}),
        ],
    )
    final, journal = harness.run(client)
    journal.close()
    assert final.phase is Phase.FAILED
    assert final.outcome is not None
    assert final.outcome.reason is OutcomeReason.AGENT_FAILED
    assert final.outcome.detail == (
        "the tester weakened its tests without a reviewer's approval: "
        f"{ACCEPTANCE}::test_farewell_every_time is now marked skip"
    )

"""TESTING's evidence rules on synthetic pytest outcomes (ADR-013): what makes an acceptance
test evidence, and what every other outcome asks of the Tester."""

from __future__ import annotations

import pytest

from msa.runtime.testing import (
    Case,
    Findings,
    Outcome,
    Run,
    after_text,
    before_text,
    cases_of,
    is_test_module,
    judge,
)
from msa.tools.sandbox import Completed

DONE = Completed(command="pytest", exit_code=0, seconds=0.0, stdout="", stderr="", elided_bytes=0)
MODULE = "tests/acceptance/test_x.py"
TEST = f"{MODULE}::test_a"


def run(*cases: Case) -> Run:
    return Run(DONE, cases)


def test_test_modules_are_the_files_pytest_collects_by_default() -> None:
    assert is_test_module("tests/acceptance/test_x.py")
    assert is_test_module("tests/acceptance/x_test.py")
    assert not is_test_module("tests/acceptance/conftest.py")
    assert not is_test_module("tests/acceptance/helpers.py")
    assert not is_test_module("tests/acceptance/test_data.json")


def test_cases_are_a_tests_parametrizations_or_else_its_whole_module() -> None:
    ran = run(
        Case(f"{MODULE}::test_a[1]", "passed"),
        Case(f"{MODULE}::test_a[2]", "failed"),
        Case(f"{MODULE}::test_ab", "passed"),  # another test, though its name starts alike
        Case(f"{MODULE}::TestG::test_a", "passed"),
    )
    assert [c.nodeid for c in cases_of(ran, MODULE, "test_a")] == [
        f"{MODULE}::test_a[1]",
        f"{MODULE}::test_a[2]",
    ]
    assert [c.nodeid for c in cases_of(ran, MODULE, "TestG::test_a")] == [
        f"{MODULE}::TestG::test_a"
    ]
    broken = run(Case(MODULE, "failed", "collection failure"))
    assert cases_of(broken, MODULE, "test_a") == [Case(MODULE, "failed", "collection failure")]
    assert cases_of(run(), MODULE, "test_a") == []


@pytest.mark.parametrize(
    ("outcomes", "flaky", "after", "before"),
    [
        ([], False, "not run", "not run"),
        (["passed"], False, "pass", "pass"),
        (["failed"], False, "fail", "fail"),
        (["failed"], True, "flaky", "fail"),
        (["skipped"], False, "skipped (why)", "skipped"),
        (["passed", "skipped"], False, "skipped (why)", "skipped"),
        (["skipped", "failed"], False, "fail", "fail"),  # failing outranks skipping
    ],
)
def test_outcomes_over_a_tests_parametrizations(
    outcomes: list[Outcome], flaky: bool, after: str, before: str
) -> None:
    cases = [
        Case(f"{TEST}[{i}]", o, "why" if o == "skipped" else "") for i, o in enumerate(outcomes)
    ]
    marked = {cases[0].nodeid: "fail, pass"} if flaky else {}
    assert after_text(cases, marked) == after
    assert before_text(cases) == before
    assert after_text([Case(TEST, "skipped")], {}) == "skipped (no reason given)"


@pytest.mark.parametrize(
    ("after", "before", "row", "problem", "rejected"),
    [
        # Evidence: failed before, passed after.
        ("passed", "failed", ("fail", "pass"), None, False),
        # Failing after is for triage, not a rule.
        ("failed", "failed", ("fail", "fail"), None, False),
        # Not evidence, whatever the reason.
        ("passed", "passed", ("pass", "pass"), "passes on the base tree, before the change", True),
        ("passed", "skipped", ("skipped", "pass"), "was skipped on the base tree", True),
        ("passed", None, ("not run", "pass"), "was not run on the base tree", True),
        (
            "skipped",
            "failed",
            ("fail", "skipped (later)"),
            "was skipped (later) on the head tree",
            False,
        ),
        (None, "failed", ("fail", "not run"), "did not run on the head tree", False),
    ],
)
def test_an_acceptance_test_is_evidence_only_if_it_fails_before_and_passes_after(
    after: Outcome | None,
    before: Outcome | None,
    row: tuple[str, str],
    problem: str | None,
    rejected: bool,
) -> None:
    def outcome(o: Outcome | None) -> Run:
        return run(Case(TEST, o, "later" if o == "skipped" else "")) if o else run()

    found = Findings()
    judge({MODULE: ["test_a"]}, outcome(after), outcome(before), found)
    assert found.matrix == [(TEST, *row)]
    assert [p for p in found.tester if problem is None or problem in p] == found.tester
    assert len(found.tester) == (problem is not None)
    assert found.rejected == ([TEST] if rejected else [])


def test_a_module_that_does_not_import_fails_every_test_in_it() -> None:
    found = Findings()
    broken = run(Case(MODULE, "failed", "collection failure"))
    judge({MODULE: ["test_a", "test_b"]}, broken, broken, found)
    assert found.matrix == [(TEST, "fail", "fail"), (f"{MODULE}::test_b", "fail", "fail")]
    assert found.tester == []

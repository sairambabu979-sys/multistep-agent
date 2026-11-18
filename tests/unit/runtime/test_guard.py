"""The weakened-test guard: counting checks per test, and what counts as weakening."""

from __future__ import annotations

from msa.runtime.guard import checks, weakened

SOURCE = """
import pytest


def helper():
    assert True  # not a test: not counted


def test_one():
    assert 1 == 1
    assert 2 == 2


class TestGroup:
    def test_raises(self):
        with pytest.raises(ValueError):
            int("x")

    async def test_async(self):
        assert pytest.approx(0.1 + 0.2) == 0.3


def test_none():
    pass
"""


def test_checks_counts_asserts_and_asserting_calls_per_test() -> None:
    assert checks(SOURCE) == {
        "test_one": 2,
        "TestGroup::test_raises": 1,
        "TestGroup::test_async": 2,  # the assert and the approx
        "test_none": 0,
    }
    assert checks("def broken(:\n") == {}


def test_dropping_a_check_or_a_test_is_weakening() -> None:
    fewer = SOURCE.replace("    assert 2 == 2\n", "")
    gone = SOURCE.replace("def test_none():\n    pass\n", "")
    assert weakened({"t.py": SOURCE}, {"t.py": fewer}) == [
        "t.py::test_one went from 2 check(s) to 1"
    ]
    assert weakened({"t.py": SOURCE}, {"t.py": gone}) == ["t.py::test_none was removed"]
    assert weakened({"t.py": SOURCE}, {"t.py": None}) == [
        f"t.py::{name} was removed"
        for name in sorted(
            ["test_one", "TestGroup::test_raises", "TestGroup::test_async", "test_none"]
        )
    ]


def test_strengthening_renaming_rejected_tests_and_new_files_are_fine() -> None:
    stronger = SOURCE.replace("def test_none():\n    pass\n", "def test_none():\n    assert 1\n")
    assert weakened({"t.py": SOURCE}, {"t.py": stronger, "new.py": "def test_x(): pass\n"}) == []
    renamed = SOURCE.replace("def test_one():", "def test_one_more():")
    assert weakened({"t.py": SOURCE}, {"t.py": renamed}, exempt={"t.py::test_one"}) == []
    assert weakened({"t.py": SOURCE}, {"t.py": renamed}) == ["t.py::test_one was removed"]

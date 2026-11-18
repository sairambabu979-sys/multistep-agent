"""The guards on the Tester's evidence: live checks per test, what counts as weakening, and
what counts as reconfiguring pytest."""

from __future__ import annotations

import pytest

from msa.runtime.guard import (
    checks,
    find_tests,
    marks,
    pytest_controls,
    reconfigured,
    weakened,
)

SOURCE = """
import pytest


def helper():
    assert inc(1) == 2  # not a test: not counted


def test_one():
    assert inc(1) == 2
    assert inc(2) == 3


class TestGroup:
    def test_raises(self):
        with pytest.raises(ValueError):
            int("x")

    async def test_async(self):
        assert pytest.approx(0.1 + 0.2) == 0.3

    class TestNested:
        def test_deep(self):
            assert inc(0)


class Helpers:
    def test_not_collected(self):
        assert inc(1)


class Legacy(unittest.TestCase):
    def test_case(self):
        self.assertEqual(inc(1), 2)


def test_none():
    pass
"""


def test_tests_are_found_as_pytest_finds_them_by_default() -> None:
    assert find_tests(SOURCE) == [
        "test_one",
        "TestGroup::test_raises",
        "TestGroup::test_async",
        "TestGroup::TestNested::test_deep",
        "Legacy::test_case",
        "test_none",
    ]
    assert find_tests("def broken(:\n") == []
    assert find_tests('"""Only a docstring."""\n') == []


def test_checks_counts_asserts_and_asserting_calls_per_test() -> None:
    assert checks(SOURCE) == {
        "test_one": 2,
        "TestGroup::test_raises": 1,
        "TestGroup::test_async": 2,  # the assert and the approx
        "TestGroup::TestNested::test_deep": 1,
        "Legacy::test_case": 0,  # unittest's assert methods are not counted
        "test_none": 0,
    }
    assert checks("def broken(:\n") == {}


@pytest.mark.parametrize(
    "body",
    [
        "    assert True",
        "    assert 1",
        "    assert not False",
        '    assert (inc(1) == 3, "always true: a tuple")',
        "    assert [inc(1)]",
        "    assert {1: inc(1)}",
        "    assert inc(1) == inc(1)",
        "    assert x is x",
    ],
)
def test_asserts_that_cannot_fail_are_not_checks(body: str) -> None:
    assert checks(f"def test_x():\n{body}\n") == {"test_x": 0}


@pytest.mark.parametrize(
    "body",
    [
        "    assert False",
        "    assert inc(1)",
        "    assert inc(1) == 2",
        "    assert inc(1) != inc(1)",  # can be false (it is, here): a check
        "    assert ()",  # an empty literal is false
        "    assert x < x",
        "    pytest.fail('no')",
    ],
)
def test_asserts_that_can_fail_are_checks(body: str) -> None:
    assert checks(f"def test_x():\n{body}\n") == {"test_x": 1}


@pytest.mark.parametrize(
    ("body", "live"),
    [
        ("return\n    assert inc(1) == 2", 0),
        ("raise SystemExit\n    assert inc(1) == 2", 0),
        ("pytest.skip('later')\n    assert inc(1) == 2", 0),
        ("pytest.xfail('known')\n    assert inc(1) == 2", 0),
        ("if True:\n        return\n    assert inc(1) == 2", 0),
        ("if not False:\n        return\n    assert inc(1) == 2", 0),
        ("if False:\n        assert inc(1) == 2", 0),
        ("if False:\n        pass\n    else:\n        return\n    assert inc(1) == 2", 0),
        ("if x:\n        return\n    else:\n        return\n    assert inc(1) == 2", 0),
        ("if True:\n        pass\n    else:\n        assert inc(1) == 2", 0),
        ("while False:\n        assert inc(1) == 2", 0),
        ("for i in []:\n        continue\n        assert inc(i)", 0),
        ("for i in []:\n        break\n        assert inc(i)", 0),
        # Unsure means live: conditional returns, context managers, try blocks.
        ("if x:\n        return\n    assert inc(1) == 2", 1),
        ("if x:\n        assert inc(1)\n    else:\n        assert inc(2)", 2),
        ("with pytest.raises(E):\n        raise E\n    assert inc(1) == 2", 2),
        ("try:\n        assert inc(1)\n    except E:\n        assert inc(2)", 2),
        ("try:\n        pass\n    finally:\n        assert inc(2)", 1),
        ("while x:\n        assert inc(1)\n    else:\n        assert inc(2)", 2),
        ("for i in x:\n        assert inc(i)\n    else:\n        assert inc(2)", 2),
        ("match x:\n        case 1:\n            assert inc(1)", 1),
        ("def check(n):\n        assert inc(n)\n    check(1)", 1),  # helpers' checks count
        ("assert inc(1)\n    return", 1),
    ],
)
def test_checks_after_the_end_of_a_block_are_dead(body: str, live: int) -> None:
    assert checks(f"def test_x():\n    {body}\n") == {"test_x": live}


def test_async_loops_and_withs_count_their_bodies() -> None:
    source = (
        "async def test_x():\n"
        "    async for i in pytest.approx(x):\n        assert inc(i)\n"
        "    async with pytest.raises(E):\n        assert inc(1)\n"
    )
    assert checks(source) == {"test_x": 4}


def test_skip_and_xfail_marks_are_found_on_tests_classes_and_modules() -> None:
    source = """
import pytest
import unittest
from pytest import mark

pytestmark = [pytest.mark.filterwarnings("ignore")]


@pytest.mark.skip(reason="later")
def test_skipped(): ...


@mark.skipif(True, reason="never")
def test_skipif(): ...


@pytest.mark.parametrize("n", [1, pytest.param(2, marks=pytest.mark.xfail)])
def test_param(n): ...


@pytest.mark.parametrize("n", [1, 2])
def test_plain(n): ...


@pytest.mark.xfail
class TestAll:
    pytestmark: list = [pytest.mark.skipif(sys.platform == "win32", reason="posix")]

    @unittest.expectedFailure
    def test_method(self): ...
"""
    assert marks(source) == {
        "test_skipped": frozenset({"skip"}),
        "test_skipif": frozenset({"skipif"}),
        "test_param": frozenset({"xfail"}),
        "TestAll::test_method": frozenset({"xfail", "skipif", "expectedFailure"}),
    }
    whole = "import pytest\n\npytestmark = pytest.mark.skip\n\n\ndef test_a(): ...\n"
    assert marks(whole) == {"test_a": frozenset({"skip"})}
    assert marks("def broken(:\n") == {}


def test_dropping_a_check_or_a_test_is_weakening() -> None:
    fewer = SOURCE.replace("    assert inc(2) == 3\n", "")
    gone = SOURCE.replace("def test_none():\n    pass\n", "")
    assert weakened({"t.py": SOURCE}, {"t.py": fewer}) == [
        "t.py::test_one went from 2 check(s) to 1"
    ]
    assert weakened({"t.py": SOURCE}, {"t.py": gone}) == ["t.py::test_none was removed"]
    assert weakened({"t.py": SOURCE}, {"t.py": None}) == [
        f"t.py::{name} was removed" for name in sorted(find_tests(SOURCE))
    ]


ORIGINAL = """
import pytest

from greeter import farewell


def test_farewell():
    assert farewell("ada") == "goodbye ada"
"""


@pytest.mark.parametrize(
    ("weaker", "problems"),
    [
        (
            ORIGINAL.replace(
                "def test_farewell", '@pytest.mark.skip(reason="x")\ndef test_farewell'
            ),
            ["t.py::test_farewell is now marked skip"],
        ),
        (
            ORIGINAL.replace("def test_farewell", "@pytest.mark.xfail\ndef test_farewell"),
            ["t.py::test_farewell is now marked xfail"],
        ),
        (
            ORIGINAL.replace("\n\n\ndef test_", "\n\npytestmark = pytest.mark.skip\n\n\ndef test_"),
            ["t.py::test_farewell is now marked skip"],
        ),
        (
            ORIGINAL.replace("    assert", "    return\n    assert"),
            ["t.py::test_farewell went from 1 check(s) to 0"],
        ),
        (
            ORIGINAL.replace("    assert", "    pytest.skip('later')\n    assert"),
            ["t.py::test_farewell went from 1 check(s) to 0"],
        ),
        (
            ORIGINAL.replace('farewell("ada") == "goodbye ada"', "True"),
            ["t.py::test_farewell went from 1 check(s) to 0"],
        ),
        (
            ORIGINAL.replace("    assert", "    if False:\n        assert"),
            ["t.py::test_farewell went from 1 check(s) to 0"],
        ),
        (
            ORIGINAL.replace("def test_farewell", "@pytest.mark.skip\ndef test_farewell").replace(
                "    assert", "    return\n    assert"
            ),
            [
                "t.py::test_farewell went from 1 check(s) to 0",
                "t.py::test_farewell is now marked skip",
            ],
        ),
    ],
)
def test_skipping_xfailing_and_making_checks_dead_or_vacuous_is_weakening(
    weaker: str, problems: list[str]
) -> None:
    assert weakened({"t.py": ORIGINAL}, {"t.py": weaker}) == problems


def test_strengthening_renaming_rejected_tests_and_new_files_are_fine() -> None:
    stronger = SOURCE.replace(
        "def test_none():\n    pass\n", "def test_none():\n    assert inc(1)\n"
    )
    assert weakened({"t.py": SOURCE}, {"t.py": stronger, "new.py": "def test_x(): pass\n"}) == []
    renamed = SOURCE.replace("def test_one():", "def test_one_more():")
    assert weakened({"t.py": SOURCE}, {"t.py": renamed}, exempt={"t.py::test_one"}) == []
    assert weakened({"t.py": SOURCE}, {"t.py": renamed}) == ["t.py::test_one was removed"]
    # A mark that was already there is not new; neither is one on a rejected test.
    marked = ORIGINAL.replace("def test_farewell", "@pytest.mark.skip\ndef test_farewell")
    assert weakened({"t.py": marked}, {"t.py": marked}) == []
    assert weakened({"t.py": ORIGINAL}, {"t.py": marked}, exempt={"t.py::test_farewell"}) == []


# ---------- pytest's controls


def test_the_controls_of_a_directory_are_the_files_above_it() -> None:
    names = ["conftest.py", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"]
    above = ["", "tests/"]
    assert pytest_controls(["tests/acceptance", "tests/repro"]) == sorted(
        f"{a}{n}" for a in above for n in names
    )
    assert pytest_controls(["acceptance"]) == sorted(names)
    assert pytest_controls([]) == []


PYPROJECT = """
[project]
name = "greeter"
dependencies = []

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
addopts = ["-p", "no:cacheprovider"]
"""


@pytest.mark.parametrize(
    ("path", "before", "after", "problem"),
    [
        # What pytest reads changed.
        (
            "pyproject.toml",
            PYPROJECT,
            PYPROJECT.replace('"no:cacheprovider"', '"no:cacheprovider", "--ignore=tests"'),
            "`pyproject.toml`: its pytest configuration was changed",
        ),
        (
            "pyproject.toml",
            PYPROJECT,
            PYPROJECT + '\n[project.entry-points.pytest11]\nmine = "src.plugin"\n',
            "`pyproject.toml`: its pytest configuration was changed",
        ),
        (
            "pyproject.toml",
            PYPROJECT,
            PYPROJECT.replace("[tool.pytest.ini_options]\n", "[tool.other]\n"),
            "`pyproject.toml`: its pytest configuration was deleted",
        ),
        (
            "tests/pyproject.toml",
            None,
            "[tool.pytest]\naddopts = []\n",
            "`tests/pyproject.toml`: its pytest configuration was created",
        ),
        (
            "pyproject.toml",
            PYPROJECT,
            "this is [not toml",
            "`pyproject.toml`: its pytest configuration was changed",
        ),
        ("pytest.ini", None, "", "`pytest.ini`: its pytest configuration was created"),
        (
            "tests/.pytest.ini",
            "[pytest]\n",
            None,
            "`tests/.pytest.ini`: its pytest configuration was deleted",
        ),
        (
            "tox.ini",
            "[tox]\nenv_list = py312\n",
            "[tox]\nenv_list = py312\n[pytest]\naddopts = -p mine\n",
            "`tox.ini`: its pytest configuration was created",
        ),
        (
            "setup.cfg",
            "[tool:pytest]\naddopts = -q\n",
            "[tool:pytest]\naddopts = -q --ignore=tests\n",
            "`setup.cfg`: its pytest configuration was changed",
        ),
        (
            "setup.cfg",
            "[metadata]\nname = x\n",
            "[metadata\nname = x\n",
            "`setup.cfg`: its pytest configuration was created",
        ),
        (
            "tests/conftest.py",
            None,
            "def pytest_collection_modifyitems(items): ...\n",
            "`tests/conftest.py` created: a conftest.py above the tests runs inside their session",
        ),
        (
            "conftest.py",
            "import pytest\n",
            "import pytest  \n",
            "`conftest.py` changed: a conftest.py above the tests runs inside their session",
        ),
        (
            "conftest.py",
            "import pytest\n",
            None,
            "`conftest.py` deleted: a conftest.py above the tests runs inside their session",
        ),
        # What pytest does not read changed: no problem.
        (
            "pyproject.toml",
            PYPROJECT,
            PYPROJECT.replace("dependencies = []", 'dependencies = ["x"]'),
            None,
        ),
        ("pyproject.toml", None, '[project]\nname = "x"\n', None),
        ("pyproject.toml", "[project]\ntool = 1\n", "[project]\ntool = 2\n", None),
        ("pyproject.toml", "tool = 1\n", "tool = 2\n", None),
        ("tox.ini", "[tox]\nenv_list = py312\n", "[tox]\nenv_list = py313\n", None),
        ("setup.cfg", None, "[metadata]\nname = x\n", None),
        ("conftest.py", "import pytest\n", "import pytest\n", None),
        ("conftest.py", None, None, None),
    ],
)
def test_reconfigured_compares_only_what_pytest_reads(
    path: str, before: str | None, after: str | None, problem: str | None
) -> None:
    assert reconfigured(path, before, after) == problem

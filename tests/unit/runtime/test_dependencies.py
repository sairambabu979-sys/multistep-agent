"""msa.runtime.dependencies: the pinned Python dependencies a change adds, read from files
only, and pip-audit's JSON verdict on them."""

from __future__ import annotations

import json

import pytest

from msa.runtime import dependencies
from msa.runtime.dependencies import Pin


def pins(path: str, head: str, base: str = "") -> list[tuple[str, str]]:
    return [(p.name, p.version) for p in dependencies.additions(path, head, base).pins]


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("requirements.txt", "requirements"),
        ("deps/requirements-dev.txt", "requirements"),
        ("requirements.in", "requirements"),
        ("pyproject.toml", "pyproject"),
        ("uv.lock", "lock"),
        ("poetry.lock", "lock"),
        ("Pipfile.lock", "pipfile"),
        ("package.json", None),
        ("Cargo.lock", None),
    ],
)
def test_the_python_dependency_files_are_recognized(path: str, kind: str | None) -> None:
    assert dependencies.kind_of(path) == kind


def test_requirements_lines_are_read_with_extras_markers_hashes_and_continuations() -> None:
    text = (
        "# a comment\n"
        "Requests[socks]==2.19.0  # pinned\n"
        'idna==3.7 ; python_version >= "3.8"\n'
        "certifi==2024.2.2 \\\n    --hash=sha256:abc\n"
        "-r other.txt\n"
        "--index-url https://example.invalid/simple\n"
        "flask>=2\n"
        "attrs===23.1.0\n"
        "wild==1.*\n"
    )
    added = dependencies.additions("requirements.txt", text, "")
    assert [(p.name, p.version) for p in added.pins] == [
        ("attrs", "23.1.0"),
        ("certifi", "2024.2.2"),
        ("idna", "3.7"),
        ("requests", "2.19.0"),
    ]
    assert added.unpinned == ("requirements.txt: flask>=2", "requirements.txt: wild==1.*")


def test_only_what_the_change_adds_is_audited_a_new_version_included() -> None:
    base = "requests==2.31.0\nidna==3.7\nflask>=2\n"
    head = "requests==2.32.0\nidna==3.7\nflask>=2\nurllib3==2.8.0\n"
    assert pins("requirements.txt", head, base) == [("requests", "2.32.0"), ("urllib3", "2.8.0")]
    assert dependencies.additions("requirements.txt", head, base).unpinned == ()


def test_names_are_compared_normalized() -> None:
    assert pins("requirements.txt", "Zope_Interface==6.0\n", "zope.interface==6.0\n") == []


def test_pyproject_pins_count_and_ranges_are_listed() -> None:
    head = """
[project]
dependencies = ["httpx==0.28.1", "pydantic>=2"]
[project.optional-dependencies]
pg = ["psycopg==3.2.3"]
[dependency-groups]
dev = ["pytest==8.3.0", {include-group = "lint"}]
lint = ["ruff"]
"""
    added = dependencies.additions("pyproject.toml", head, "")
    assert [(p.name, p.version) for p in added.pins] == [
        ("httpx", "0.28.1"),
        ("psycopg", "3.2.3"),
        ("pytest", "8.3.0"),
    ]
    assert added.unpinned == ("pyproject.toml: pydantic>=2", "pyproject.toml: ruff")


def test_a_lock_files_index_packages_count_and_the_project_itself_does_not() -> None:
    base = """
[[package]]
name = "demo"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "idna"
version = "3.6"
source = { registry = "https://pypi.org/simple" }
"""
    head = (
        base.replace('version = "3.6"', 'version = "3.7"')
        + """
[[package]]
name = "local-thing"
version = "1.0"
source = { path = "../local" }

[[package]]
name = "requests"
version = "2.32.0"
source = { registry = "https://pypi.org/simple" }
"""
    )
    assert pins("uv.lock", head, base) == [("idna", "3.7"), ("requests", "2.32.0")]


def test_a_poetry_lock_reads_the_same_way() -> None:
    head = '[[package]]\nname = "Flask"\nversion = "3.0.0"\n'
    assert pins("poetry.lock", head) == [("flask", "3.0.0")]


def test_a_pipfile_lock_reads_its_default_and_develop_packages() -> None:
    head = json.dumps(
        {
            "default": {"requests": {"version": "==2.32.0"}},
            "develop": {"pytest": {"version": "==8.3.0"}, "local": {"path": "."}},
        }
    )
    assert pins("Pipfile.lock", head) == [("pytest", "8.3.0"), ("requests", "2.32.0")]


def test_a_dependency_file_that_does_not_parse_is_reported() -> None:
    with pytest.raises(ValueError, match=r"pyproject\.toml does not parse"):
        dependencies.additions("pyproject.toml", "[project\n", "")


def test_a_file_that_is_not_a_python_dependency_file_adds_nothing() -> None:
    assert dependencies.additions("package.json", '{"dependencies": {}}', "") == (
        dependencies.Added((), ())
    )


def test_the_audit_requirements_pin_each_addition_once() -> None:
    found = [Pin("requests", "2.19.0", "a.txt"), Pin("requests", "2.19.0", "b.txt")]
    found += [Pin("idna", "3.7", "a.txt")]
    assert dependencies.requirements(found) == "idna==3.7\nrequests==2.19.0\n"


def test_pip_audits_json_names_the_vulnerable_and_skips_the_unknown() -> None:
    report = json.dumps(
        {
            "dependencies": [
                {
                    "name": "Requests",
                    "version": "2.19.0",
                    "vulns": [
                        {"id": "PYSEC-2018-28", "fix_versions": ["2.20.0"], "aliases": []},
                        {"id": "PYSEC-2023-74", "fix_versions": ["2.31.0"], "aliases": []},
                    ],
                },
                {"name": "idna", "version": "3.7", "vulns": []},
                {"name": "nowhere", "skip_reason": "Dependency not found on PyPI"},
            ],
            "fixes": [],
        }
    )
    assert dependencies.vulnerable(report) == [
        dependencies.Vulnerable(
            "requests", "2.19.0", ("PYSEC-2018-28", "PYSEC-2023-74"), ("2.20.0", "2.31.0")
        )
    ]


@pytest.mark.parametrize("report", ["", "not json", '{"no": "dependencies"}'])
def test_anything_but_pip_audits_json_is_refused(report: str) -> None:
    with pytest.raises(ValueError, match="not pip-audit's JSON output"):
        dependencies.vulnerable(report)

"""Resolution, conflicts, selection, rendering and lint (PLAN §5.2-5.4)."""

from __future__ import annotations

import random
import re
import subprocess
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from msa.skills.preferences import Layer, PreferenceConflict, PreferenceError, resolve, store
from msa.skills.preferences.lint import lint
from msa.skills.preferences.render import render
from msa.skills.preferences.resolve import run_layer, scopes_overlap
from msa.skills.preferences.schema import Scope, language_of

BASE: dict[str, Any] = {"rule": "A rule.", "enforcement": "soft"}


def layer(name: str, **entries: dict[str, Any]) -> Layer:
    patches = [{"id": key.replace("_", "."), **fields} for key, fields in entries.items()]
    return Layer.from_document(name, f"{name}.yaml", {"version": 1, "preferences": patches})  # type: ignore[arg-type]


# ---------- documents


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ({"version": 2, "preferences": []}, "version"),
        ({"version": 1, "preferences": [{"id": "Bad Id", **BASE}]}, "preferences.0.id"),
        ({"version": 1, "preferences": [{"id": "a.b", "colour": "red"}]}, "colour"),
        ({"version": 1, "preferences": [{"id": "a.b"}, {"id": "a.b"}]}, "repeated: ['a.b']"),
        (
            {
                "version": 1,
                "preferences": [{"id": "a.b", "check": {"kind": "regex", "pattern": "("}}],
            },
            "not a valid regular expression",
        ),
        (
            {
                "version": 1,
                "preferences": [{"id": "a.b", "check": {"kind": "ruff", "rule": "e722"}}],
            },
            "check.ruff.rule",
        ),
        (
            {"version": 1, "preferences": [{"id": "a.b", "scope": {"paths": ["../x"]}}]},
            "use a relative path glob",
        ),
        ([1, 2], "expected a mapping with version and preferences"),
    ],
)
def test_invalid_documents_are_refused_with_their_location(data: object, problem: str) -> None:
    with pytest.raises(PreferenceError, match=r"^repo\.yaml: ") as info:
        Layer.from_document("repo", "repo.yaml", data)
    assert problem in str(info.value)


# ---------- resolution


def test_higher_layers_override_field_by_field_and_provenance_says_who() -> None:
    resolved = resolve(
        [
            layer("repo", a_b={"enforcement": "hard"}),
            layer("built-in", a_b={"rule": "Base rule.", "enforcement": "soft", "rationale": "r"}),
        ]
    )
    (p,) = resolved.preferences
    assert (p.rule, p.enforcement, p.rationale) == ("Base rule.", "hard", "r")
    assert resolved.provenance["a.b"] == {
        "rule": "built-in.yaml",
        "enforcement": "repo.yaml",
        "rationale": "built-in.yaml",
        "id": "built-in.yaml",
    }
    assert resolved.origin("a.b") == "built-in.yaml + repo.yaml"
    assert resolved.origin("nope") == "nowhere"
    assert resolved.get("nope") is None


def test_an_entry_no_layer_completes_is_refused_precisely() -> None:
    with pytest.raises(PreferenceError, match=r"^a\.b: no layer sets rule \(defined by --pref\)$"):
        resolve([run_layer(["a.b=hard"])])


def test_a_merged_entry_with_a_bad_value_names_the_problem() -> None:
    bad = Layer("repo", "repo.yaml", {"a.b": {"rule": "", "enforcement": "hard"}})
    with pytest.raises(PreferenceError, match=r"^a\.b: String should have at least 1 character"):
        resolve([bad])


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("a.b", "expected <id>=hard"),
        ("a.b=loud", "expected <id>=hard"),
        ("Nope=off", "is not a preference id"),
    ],
)
def test_bad_overrides_are_refused(text: str, problem: str) -> None:
    with pytest.raises(PreferenceError, match=problem):
        run_layer([text])


def test_overrides_for_the_same_id_combine() -> None:
    assert dict(run_layer(["a.b=hard", "a.b=off", "c.d=info"]).patches) == {
        "a.b": {"enforcement": "off"},
        "c.d": {"enforcement": "info"},
    }


# ---------- conflicts (I-16 at the unit level)


def conflicting(**scope_b: Any) -> list[Layer]:
    return [
        layer("user", a_quotes={**BASE, "enforcement": "hard", "conflicts_with": ["b.quotes"]}),
        layer("repo", b_quotes={**BASE, "enforcement": "hard", "scope": scope_b}),
    ]


def test_contradicting_hard_preferences_where_both_apply_are_refused_precisely() -> None:
    with pytest.raises(PreferenceConflict) as info:
        resolve(conflicting(language="python", paths=["src/**"]))
    assert str(info.value) == (
        "conflicting hard preferences: a.quotes (from user.yaml) conflicts with b.quotes "
        "(from repo.yaml): both are hard and both apply to any language, all paths / "
        "python, src/**. make one soft, narrow a scope, or switch one off with --pref <id>=off"
    )


@pytest.mark.parametrize(
    "fix",
    [
        lambda layers: [*layers, run_layer(["a.quotes=off"])],
        lambda layers: [*layers, run_layer(["b.quotes=soft"])],
    ],
)
def test_a_run_override_resolves_a_conflict(fix: Any) -> None:
    assert len(resolve(fix(conflicting())).preferences) == 2


def test_disjoint_scopes_do_not_conflict() -> None:
    layers = [
        layer(
            "repo",
            a_x={
                **BASE,
                "enforcement": "hard",
                "scope": {"paths": ["src/**"]},
                "conflicts_with": ["b.x"],
            },
        ),
        layer("user", b_x={**BASE, "enforcement": "hard", "scope": {"paths": ["tests/**"]}}),
    ]
    assert len(resolve(layers).preferences) == 2
    other_language = [
        layer(
            "repo",
            a_x={
                **BASE,
                "enforcement": "hard",
                "scope": {"language": "go"},
                "conflicts_with": ["b.x"],
            },
        ),
        layer("user", b_x={**BASE, "enforcement": "hard", "scope": {"language": "python"}}),
    ]
    assert len(resolve(other_language).preferences) == 2


SCOPES = st.builds(
    Scope,
    language=st.sampled_from([None, "python", "go"]),
    paths=st.lists(
        st.sampled_from(["src/**", "tests/**", "**/*.py", "src/a.py", "docs/*"]), max_size=2
    ).map(tuple),
)
PATHS = st.sampled_from(
    ["src/a.py", "src/b.go", "tests/t.py", "docs/x.md", "a.py", "src/deep/c.py"]
)


@given(SCOPES, SCOPES, PATHS)
def test_scope_overlap_never_misses_a_file_both_apply_to(a: Scope, b: Scope, path: str) -> None:
    if a.applies_to(path) and b.applies_to(path):
        assert scopes_overlap(a, b)


# ---------- selection and rendering


def test_selection_by_language_and_path_skips_switched_off_preferences() -> None:
    resolved = resolve(
        [
            layer(
                "repo",
                py_x={**BASE, "scope": {"language": "python"}},
                src_y={**BASE, "scope": {"paths": ["src/**"]}},
                any_z=BASE,
                off_w={**BASE, "enforcement": "off"},
            )
        ]
    )
    assert [p.id for p in resolved.select(["docs/readme.md"])] == ["any.z"]
    assert [p.id for p in resolved.select(["src/app.py"])] == ["any.z", "py.x", "src.y"]
    assert resolved.select([]) == ()


def test_languages_come_from_extensions_in_the_last_segment() -> None:
    assert language_of("src/app.PY") == "python"
    assert language_of("dir.py/README") is None
    assert language_of("Makefile") is None


ENTRIES = {
    f"p_{i}": {
        **BASE,
        "enforcement": e,
        "rationale": "Because.\r\n  It matters.  ",
        "examples": {"good": "ok()", "bad": "bad()"},
        "check": c,
    }
    for i, (e, c) in enumerate(
        [("hard", {"kind": "ruff", "rule": "E722"}), ("soft", None), ("info", None), ("off", None)]
    )
}


@given(st.randoms())
def test_the_rendered_block_is_byte_stable_whatever_the_input_order(rnd: random.Random) -> None:
    def rendered(entries: dict[str, dict[str, Any]], files: list[str]) -> str:
        return render(resolve([layer("repo", **entries)]).select(files))

    expected = rendered(ENTRIES, ["a.py", "b.md"])
    keys = list(ENTRIES)
    rnd.shuffle(keys)
    files = ["b.md", "a.py", "a.py"]
    rnd.shuffle(files)
    assert rendered({k: ENTRIES[k] for k in keys}, files) == expected


def test_the_rendered_block_reads_well() -> None:
    text = render(resolve([layer("repo", **ENTRIES)]).select(["a.py"]))
    assert text.startswith("## Coding preferences\n")
    assert "- p.0 | HARD (a blocker) [checked] | any language, all paths\n  Rule: A rule." in text
    # Line endings unified and trailing space cut; a line's own indent is kept for code.
    assert "  Why: Because.\n      It matters.\n" in text
    assert "p.3" not in text  # off
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert render([]) == ""


# ---------- lint


def test_lint_reports_what_resolution_lets_through() -> None:
    resolved = resolve(
        [
            layer(
                "repo",
                hard_unchecked={**BASE, "enforcement": "hard"},
                regex_misses={
                    **BASE,
                    "check": {"kind": "regex", "pattern": "TODO"},
                    "examples": {"bad": "# FIXME", "good": "# TODO later"},
                },
                soft_a={**BASE, "conflicts_with": ["soft.b", "ghost.id"]},
                soft_b=BASE,
            )
        ]
    )
    assert [f.render() for f in lint(resolved)] == [
        "error: regex.misses: its check does not catch its own bad example",
        "error: regex.misses: its check flags its own good example",
        "warning: hard.unchecked: hard but unchecked: only the reviewer enforces it, at LLM "
        "cost. Add a check if a linter or a pattern can catch it.",
        "warning: soft.a: conflicts with soft.b where both apply (soft vs soft); the reviewer "
        "will see both",
        "warning: soft.a: conflicts_with unknown 'ghost.id'",
    ]


def test_an_empty_layer_file_is_an_empty_layer() -> None:
    assert dict(Layer.from_document("repo", "repo.yaml", None).patches) == {}


def test_the_built_in_defaults_lint_clean() -> None:
    assert lint(resolve([store.builtin_layer()])) == []


@pytest.mark.parametrize(
    ("pattern", "refused"),
    [
        ("(?<=a)b", True),
        ("(?<!a)b", True),
        ("x(?=y)", True),
        ("x(?!y)", True),
        ("(?P<n>a)(?P=n)", True),
        (r"(a)\1", True),
        (r"(a)\\\1", True),  # an escaped backslash, then a backreference
        (r"\\1", False),  # an escaped backslash, then the digit 1
        (r"\(?=", False),  # an escaped paren, an optional one, then '='
        (r"\d+\s*", False),
        ("(?i)case(?:group)", False),
    ],
)
def test_patterns_ripgrep_cannot_run_are_refused_at_load(pattern: str, refused: bool) -> None:
    data = {
        "version": 1,
        "preferences": [{"id": "a.b", "check": {"kind": "regex", "pattern": pattern}}],
    }
    if refused:
        with pytest.raises(
            PreferenceError, match="lookaround and backreferences are not supported"
        ):
            Layer.from_document("repo", "repo.yaml", data)
    else:
        Layer.from_document("repo", "repo.yaml", data)


@pytest.mark.parametrize(
    ("pattern", "accepted"),
    [
        (r"(a)?(?(1)b|c)", False),
        (r"(?a)word", False),
        (r"(?#comment)word", False),
        (r"(?P<word>TODO|FIXME)\b", True),
        (r"^$", True),
    ],
)
def test_python_patterns_are_also_validated_by_ripgrep(pattern: str, accepted: bool) -> None:
    re.compile(pattern)
    data = {
        "version": 1,
        "preferences": [{"id": "a.b", "check": {"kind": "regex", "pattern": pattern}}],
    }
    if accepted:
        Layer.from_document("repo", "repo.yaml", data)
    else:
        with pytest.raises(PreferenceError, match="not a valid ripgrep regular expression"):
            Layer.from_document("repo", "repo.yaml", data)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError(), "rg is not installed"),
        (subprocess.TimeoutExpired("rg", 5), "ripgrep regex validation timed out"),
    ],
)
def test_regex_validation_fails_closed_when_the_engine_cannot_run(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, message: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    data = {
        "version": 1,
        "preferences": [
            {"id": "a.b", "check": {"kind": "regex", "pattern": "uncached-validation"}}
        ],
    }
    with pytest.raises(PreferenceError, match=message):
        Layer.from_document("repo", "repo.yaml", data)


def test_a_minimal_preference_renders_just_its_rule() -> None:
    text = render(resolve([layer("repo", a_b=BASE)]).preferences)
    assert text.endswith("- a.b | soft (a nit) | any language, all paths\n  Rule: A rule.\n")

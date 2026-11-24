import ast
import inspect

import configlib.env
import configlib.loader
from configlib import layered, with_env
from configlib.merge import merge


def test_merge():
    base = {"a": {"x": 1, "y": 2}, "b": {"z": 3}}
    override = {"a": {"y": 20}, "c": {"w": 4}}
    assert merge(base, override) == {"a": {"x": 1, "y": 20}, "b": {"z": 3}, "c": {"w": 4}}
    assert base == {"a": {"x": 1, "y": 2}, "b": {"z": 3}}
    assert override == {"a": {"y": 20}, "c": {"w": 4}}


def test_the_result_shares_nothing_with_the_arguments():
    base = {"a": {"x": 1}}
    merged = merge(base, {})
    merged["a"]["x"] = 2
    assert base["a"]["x"] == 1


def _called(module):
    tree = ast.parse(inspect.getsource(module))
    return {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def test_both_use_merge():
    assert "merge" in _called(configlib.loader)
    assert "merge" in _called(configlib.env)


def test_behaviour_is_unchanged():
    assert layered("[s]\na = 1\nb = 2", "[s]\nb = 3")["s"] == {"a": 1, "b": 3}
    assert with_env({"s": {"a": 1}}, {"APP_S_B": "2"}) == {"s": {"a": 1, "b": 2}}

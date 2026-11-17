"""Pure-logic tests for the shrink-only cleanup ratchets in scripts/ci."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

CI = Path(__file__).parents[1] / "scripts/ci"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, CI / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ruff_ratchet = _load("ruff_ratchet")
dead = _load("check_dead_symbols")
dup = _load("check_duplicate_functions")


def test_ruff_ratchet_flags_increases_and_new_files_only():
    baseline = {"C901": {"a.py": 2}, "PLC0415": {"a.py": 1}}
    current = {"C901": {"a.py": 1, "b.py": 1}, "PLC0415": {"a.py": 2}}
    increases, decreases = ruff_ratchet.compare(baseline, current)
    assert increases == [
        "b.py: C901 1 > baseline 0",
        "a.py: PLC0415 2 > baseline 1",
    ]
    assert decreases == ["a.py: C901 1 < baseline 2"]


def test_dead_symbol_references_ignore_reexports_all_and_self_use():
    tree = ast.parse(
        "from .m import reexported, aliased as other\n"
        "__all__ = ['listed']\n"
        "def recursive():\n    return recursive()\n"
        "getattr(obj, 'by_string')\n"
        "obj.attribute\n"
    )
    refs = dead.References()
    refs.scan(tree, "frisket.pkg.mod", is_package=False)
    assert {"aliased", "by_string", "attribute"} <= set(refs.names)
    assert not {"reexported", "listed", "recursive"} & set(refs.names)
    assert {"frisket.pkg.m", "frisket.pkg.m.reexported"} <= refs.modules


def test_duplicate_shape_erases_names_and_strings_but_not_structure():
    def body(source: str) -> str:
        return dup.shape(ast.parse(source).body[0].body)

    assert body("def f(a):\n    return a + 'x'") == body(
        "def g(b):\n    return b + 'y'"
    )
    assert body("def f(a):\n    return a + 1") != body("def f(a):\n    return a - 1")


def test_duplicate_groups_may_shrink_but_not_grow_or_appear():
    baseline = {"near": [["a::f", "b::f", "c::f"]], "private": []}
    shrunk = {"near": [["a::f", "b::f"]], "private": []}
    grown = {"near": [["a::f", "b::f", "d::f"]], "private": []}
    fresh = {"near": [], "private": [["x::_h", "y::_h", "z::_h"]]}
    assert dup.regressions(baseline, shrunk) == []
    assert "not in baseline: d::f" in dup.regressions(baseline, grown)[0]
    assert "not in baseline: x::_h, y::_h, z::_h" in dup.regressions(baseline, fresh)[0]


def test_class_body_binding_defines_a_method_and_is_not_a_use():
    tree = ast.parse(
        "class Facade:\n    read = leaf.read\n    renamed = leaf.other\n    limit = 3\n"
    )
    refs = dead.References()
    refs.scan(tree, "pkg.facade", False)
    assert refs.names["read"] == 0
    assert refs.names["other"] == 1
    assert refs.names["renamed"] == 0
    found = dict(dead.candidates(tree, "facade.py"))
    assert found["facade.py::Facade.read"] == "read"
    assert found["facade.py::Facade.renamed"] == "renamed"
    assert "facade.py::Facade.limit" not in found

from __future__ import annotations

from pathlib import Path

from scripts.ci.check_import_boundaries import (
    ImportBoundaryRule,
    check_import_boundaries,
)


RULE = ImportBoundaryRule(
    id="fixture-leaf",
    modules=("pkg.leaf",),
    allowed_imports=("pkg.allowed", "pkg.nested.tool"),
    allow_stdlib=True,
)


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def test_checker_reports_exact_diagnostics_for_forbidden_and_unresolved_mutations(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    _write_tree(
        source_root,
        {
            "pkg/__init__.py": "",
            "pkg/allowed.py": "",
            "pkg/hidden.py": "",
            "pkg/leaf.py": "",
            "json.py": "",
        },
    )
    mutations = {
        "import vendor.client as client\n": (
            "fixture-leaf: pkg/leaf.py:1: forbidden import: vendor.client "
            "(import vendor.client as client)"
        ),
        "from . import hidden as alias\n": (
            "fixture-leaf: pkg/leaf.py:1: forbidden import: pkg.hidden "
            "(from . import hidden as alias)"
        ),
        "import importlib as loader\nloader.import_module('vendor.plugin')\n": (
            "fixture-leaf: pkg/leaf.py:2: forbidden import: vendor.plugin "
            "(loader.import_module('vendor.plugin'))"
        ),
        "from importlib import import_module as load\nload(target)\n": (
            "fixture-leaf: pkg/leaf.py:2: unresolved dynamic import: <dynamic> "
            "(load(target))"
        ),
        "__import__(target)\n": (
            "fixture-leaf: pkg/leaf.py:1: unresolved dynamic import: <dynamic> "
            "(__import__(target))"
        ),
        "import json\n": (
            "fixture-leaf: pkg/leaf.py:1: forbidden import: json (import json)"
        ),
    }

    leaf = source_root / "pkg" / "leaf.py"
    for mutation, expected in mutations.items():
        leaf.write_text(mutation, encoding="utf-8")
        diagnostics = tuple(map(str, check_import_boundaries(source_root, (RULE,))))
        assert expected in diagnostics

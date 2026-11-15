from __future__ import annotations

from pathlib import Path

import pytest

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


MEMBER_RULE = ImportBoundaryRule(
    id="fixture-member-owner",
    modules=("pkg.*",),
    exclude=("pkg.cache", "pkg.process_owner"),
    forbidden_members=(
        "pkg.cache.promote",
        "pkg.cache.uninstall",
        "pkg.process_owner.Process._*",
    ),
    reject_unresolved_dynamic=False,
)


@pytest.mark.parametrize(
    "source, forbidden",
    [
        ("from pkg.cache import promote as publish\npublish()\n", "pkg.cache.promote"),
        (
            "from pkg import cache as storage\nstorage.uninstall()\n",
            "pkg.cache.uninstall",
        ),
        ("import pkg.cache as storage\nstorage.promote()\n", "pkg.cache.promote"),
        ("import pkg.cache\npkg.cache.promote()\n", "pkg.cache.promote"),
        ("from . import cache\ncallback = cache.promote\n", "pkg.cache.promote"),
        ("from .cache import uninstall\nuninstall()\n", "pkg.cache.uninstall"),
        ("from pkg.cache import *\n", "pkg.cache.*"),
        (
            "from pkg.process_owner import Process as Child\nChild._cleanup()\n",
            "pkg.process_owner.Process._cleanup",
        ),
    ],
)
def test_member_owner_rejects_aliases_and_callback_escape(
    tmp_path: Path, source: str, forbidden: str
) -> None:
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/cache.py": "def promote(): pass\n",
            "pkg/process_owner.py": "",
            "pkg/caller.py": source,
        },
    )
    violations = check_import_boundaries(tmp_path, (MEMBER_RULE,))
    assert any(
        item.imported == forbidden and item.reason == "forbidden member access"
        for item in violations
    )


def test_member_owner_allows_reads_public_commands_and_owner_implementation(
    tmp_path: Path,
) -> None:
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/cache.py": "from pkg import cache\ndef promote(): cache.promote()\n",
            "pkg/process_owner.py": "class Process:\n def _cleanup(self): pass\n",
            "pkg/caller.py": (
                "from pkg import cache\nfrom pkg.process_owner import Process\n"
                "cache.is_installed()\nProcess.start()\n"
                "# cache.promote() is documentation, not a use.\n"
                "example = 'pkg.cache.uninstall()'\n"
                "def promote(): pass\n"
            ),
        },
    )
    assert not check_import_boundaries(tmp_path, (MEMBER_RULE,))

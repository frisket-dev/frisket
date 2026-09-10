"""Focused tests for the protected-cell write architecture check."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


CHECKER = Path(__file__).parents[1] / "scripts/ci/check_cell_write_ownership.py"
spec = importlib.util.spec_from_file_location("check_cell_write_ownership", CHECKER)
assert spec and spec.loader
checker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = checker
spec.loader.exec_module(checker)


def _scan(
    tmp_path: Path, source: str, *, owners: dict[str, tuple[str, ...]] | None = None
):
    path = tmp_path / "fixture.py"
    path.write_text(source, encoding="utf-8")
    return checker.check_paths([path], root=tmp_path, owners=owners or {})


def test_literal_write_outside_owner_is_reported(tmp_path: Path):
    # rule19: this test intentionally supplies source text to an architectural checker.
    violations = _scan(tmp_path, 'db.execute("INSERT INTO cells VALUES (?)", (1,))')
    assert [(v.table, v.operation) for v in violations] == [("cells", "INSERT")]


def test_named_owner_is_allowed(tmp_path: Path):
    violations = _scan(
        tmp_path,
        'db.executemany("REPLACE INTO cells VALUES (?)", rows)',
        owners={"cells": ("fixture.py",)},
    )
    assert violations == []


def test_f_string_and_executescript_are_checked(tmp_path: Path):
    source = """
name = "cells"
db.executescript(f"UPDATE {name} SET value = NULL; DELETE FROM results WHERE id = 1")
"""
    violations = _scan(tmp_path, source)
    assert {(v.table, v.operation) for v in violations} == {
        ("cells", "UPDATE"),
        ("results", "DELETE"),
    }


def test_every_initial_protected_table_and_write_verb_is_checked(tmp_path: Path):
    # rule19: generated fixture source exercises the checker independently of the repository.
    statements = []
    for table in sorted(checker.PROTECTED_TABLES):
        statements.extend(
            (
                f'db.execute("INSERT INTO {table} VALUES (?)")',
                f'db.execute("REPLACE INTO {table} VALUES (?)")',
                f'db.execute("UPDATE {table} SET value = NULL")',
                f'db.execute("DELETE FROM {table}")',
            )
        )
    violations = _scan(tmp_path, "\n".join(statements))
    assert {(v.table, v.operation) for v in violations} == {
        (table, operation)
        for table in checker.PROTECTED_TABLES
        for operation in ("INSERT", "REPLACE", "UPDATE", "DELETE")
    }


def test_reads_and_dynamic_table_names_are_outside_static_proof(tmp_path: Path):
    source = """
db.execute("SELECT * FROM cells")
db.execute("SELECT 'INSERT INTO cells VALUES (?)'")
db.execute("-- DELETE FROM cells\nSELECT 1")
db.execute(f"UPDATE {table} SET value = NULL")
"""
    assert _scan(tmp_path, source) == []


def test_structural_delete_is_owned_but_other_structural_writes_are_unscoped(
    tmp_path: Path,
):
    # rule19: this fixture exercises the operation-specific architecture rule.
    source = """
db.execute("DELETE FROM rows WHERE sheet_id=?")
db.execute("INSERT INTO rows (sheet_id) VALUES (?)")
db.execute("UPDATE columns SET hidden=1 WHERE id=?")
"""
    violations = _scan(tmp_path, source, owners={"rows": ("owner.py",)})
    assert [(v.table, v.operation) for v in violations] == [("rows", "DELETE")]

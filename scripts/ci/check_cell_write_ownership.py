#!/usr/bin/env python3
"""Check that protected cell tables have one named Python store owner.

This is intentionally a small architectural check, not a SQL parser.  It
finds literal and partly-literal f-string SQL passed to sqlite-style
``execute``, ``executemany``, and ``executescript`` calls.  Dynamic SQL is
outside its proof boundary; callers must keep table names static or use the
owning store API.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


PROTECTED_TABLES = frozenset(
    {
        "cells",
        "edits",
        "current_cells",
        "base_cell_producers",
        "results",
        "cell_result_heads",
    }
)

# Exact paths are intentional.  Add a path only when that module is the
# named store owner; do not replace these with a broad ``store/**`` exemption.
TABLE_OWNERS: Mapping[str, tuple[str, ...]] = {
    "cells": ("src/frisket/engine/store/cell_writes.py",),
    "edits": ("src/frisket/engine/store/cell_writes.py",),
    "current_cells": ("src/frisket/engine/store/current_cells.py",),
    "base_cell_producers": ("src/frisket/engine/store/cell_writes.py",),
    "results": (
        "src/frisket/engine/store/runs.py",
        "src/frisket/engine/store/bundle_io.py",  # Discarded-run garbage collection.
    ),
    "cell_result_heads": ("src/frisket/engine/store/result_generations.py",),
}

# Structural metadata is intentionally scoped to DELETE only. INSERT and
# UPDATE of rows/columns/sheets remain outside this checker.
DELETE_OWNERS: Mapping[str, tuple[str, ...]] = {
    "rows": (
        "src/frisket/engine/store/cell_writes.py",
        "src/frisket/engine/store/sheet_lifecycle.py",
    ),
    "columns": ("src/frisket/engine/store/sheet_lifecycle.py",),
    "sheets": (
        "src/frisket/engine/store/sheet_lifecycle.py",
        "src/frisket/engine/store/streaming_import.py",
    ),
}

_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_]\w*)'
_TABLE = rf"(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})?)"
_SQL_NON_CODE = re.compile(r"'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/", re.S)
_WRITE_PATTERNS = (
    re.compile(rf"\bINSERT(?:\s+OR\s+\w+)?\s+INTO\s+{_TABLE}", re.I),
    re.compile(rf"\bREPLACE(?:\s+INTO)?\s+{_TABLE}", re.I),
    re.compile(rf"\bUPDATE(?:\s+OR\s+\w+)?\s+{_TABLE}", re.I),
    re.compile(rf"\bDELETE\s+FROM\s+{_TABLE}", re.I),
)


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    table: str
    operation: str


def _constant_strings(tree: ast.AST) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = _static_sql(node.value, values)
            if isinstance(target, ast.Name) and value is not None:
                values[target.id] = value
    return values


def _static_sql(node: ast.AST, names: Mapping[str, str] | None = None) -> str | None:
    names = names or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_sql(node.left, names), _static_sql(node.right, names)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        # A dynamic expression, including a table name, remains a placeholder
        # and is outside the proof.
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append(_static_sql(value.value, names) or "{}")
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def _table_name(token: str) -> str:
    return token.split(".")[-1].strip().strip('"`[]').lower()


def _writes(sql: str) -> Iterable[tuple[str, str]]:
    sql = _SQL_NON_CODE.sub(" ", sql)
    for pattern in _WRITE_PATTERNS:
        for match in pattern.finditer(sql):
            table = _table_name(match.group("table"))
            operation = match.group(0).split()[0].upper()
            if table in PROTECTED_TABLES or (
                operation == "DELETE" and table in DELETE_OWNERS
            ):
                yield table, operation


def _owners_for(
    table: str,
    operation: str,
    owners: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...] | None:
    if table in PROTECTED_TABLES:
        return owners.get(table, ())
    if operation == "DELETE" and table in DELETE_OWNERS:
        # Custom maps keep fixture tests independent of repository paths.
        return owners.get(table, DELETE_OWNERS[table])
    return None


def _scan_file(
    path: Path, *, root: Path, owners: Mapping[str, tuple[str, ...]]
) -> list[Violation]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        print(f"{path}: unable to inspect: {exc}", file=sys.stderr)
        return []
    names = _constant_strings(tree)
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"execute", "executemany", "executescript"}:
            continue
        sql = _static_sql(node.args[0], names) if node.args else None
        if sql is None:
            continue
        for table, operation in _writes(sql):
            allowed = _owners_for(table, operation, owners)
            if allowed is not None and relative not in allowed:
                violations.append(Violation(path, node.lineno, table, operation))
    return violations


def check_paths(
    paths: Iterable[Path],
    *,
    root: Path,
    owners: Mapping[str, tuple[str, ...]] = TABLE_OWNERS,
) -> list[Violation]:
    violations: list[Violation] = []
    for entry in paths:
        files = [entry] if entry.is_file() else sorted(entry.rglob("*.py"))
        for path in files:
            violations.extend(_scan_file(path, root=root, owners=owners))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="*", type=Path, help="Python files/directories (default: src)"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root",
    )
    args = parser.parse_args(argv)
    paths = args.paths or [args.root / "src"]
    violations = check_paths(paths, root=args.root)
    for violation in violations:
        print(
            f"{violation.path}:{violation.line}: {violation.operation} write to "
            f"protected table {violation.table!r} is outside its named store owner"
        )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())

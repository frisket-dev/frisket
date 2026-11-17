#!/usr/bin/env python3
"""Check that cell and execution lifecycle writes use named Python owners.

This is intentionally a small architectural check, not a SQL parser.  It
finds literal and partly-literal f-string SQL passed to sqlite-style
``execute``, ``executemany``, and ``executescript`` calls.  Dynamic SQL is
outside its proof boundary; callers must keep table names static or use the
owning store API. Static runs updates inspect assignment targets, not predicates
or right-hand expressions. Unknown runs assignment targets require the run owner.

Write-transaction boundaries have one owner too: in the store and job modules
the ``"BEGIN IMMEDIATE"`` literal belongs to ``store/transactions.py``; the
remaining hand-written boundaries are an exact, shrink-only list.
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
        "execution_attempts",
    }
)

# Exact paths are intentional.  Add a path only when that module is the
# named store owner; do not replace these with a broad ``store/**`` exemption.
TABLE_OWNERS: Mapping[str, tuple[str, ...]] = {
    "execution_attempts": (
        "src/frisket/execution/attempt.py",  # Mint, admit, claim, and explicit close.
        "src/frisket/engine/store/output_claims.py",  # Fenced dead-writer recovery.
        "src/frisket/engine/executor/project_run_terminalization.py",  # Pending closure.
        "src/frisket/upgrades/project/domain_migration.py",  # Frozen stopped-work cutover.
    ),
    "cells": ("src/frisket/engine/store/cell_writes.py",),
    "edits": ("src/frisket/engine/store/cell_writes.py",),
    "current_cells": (
        "src/frisket/engine/store/current_cells.py",
        "src/frisket/upgrades/project/historical_current_cells.py",  # Frozen pre-cutover migrations.
        "src/frisket/engine/store/cell_projection.py",
    ),
    "base_cell_producers": ("src/frisket/engine/store/cell_writes.py",),
    "results": (
        "src/frisket/engine/store/runs.py",
        "src/frisket/engine/store/result_generations.py",  # Atomic publication fact.
        "src/frisket/engine/store/bundle_io.py",  # Discarded-run garbage collection.
    ),
    "cell_result_heads": ("src/frisket/engine/store/result_generations.py",),
}

# Frozen one-time upgrades write preceding schemas, not current store commands.
# Keep this list exact; it is not an exemption for arbitrary migration modules.
for _table in ("cells", "base_cell_producers", "current_cells", "results"):
    TABLE_OWNERS[_table] += ("src/frisket/upgrades/project/domain_migration.py",)
for _table in ("cells", "base_cell_producers", "current_cells", "edits"):
    TABLE_OWNERS[_table] += (
        "src/frisket/upgrades/project/domain_document_text_migration.py",
    )

# Additional owners scoped to DELETE only. Offline purge cannot insert/update
# result facts. Structural INSERT/UPDATE remain outside this checker.
DELETE_OWNERS: Mapping[str, tuple[str, ...]] = {
    "results": ("src/frisket/engine/store/purge.py",),
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

# Run metadata/progress has several legitimate writers. Lifecycle fields have
# narrower owners; e.g. Undo can request cancellation, never mark a run complete.
_RUN_STORE = "src/frisket/engine/store/runs.py"
_RUN_TERMINALIZER = "src/frisket/engine/executor/project_run_terminalization.py"
_DOMAIN_MIGRATION = "src/frisket/upgrades/project/domain_migration.py"
RUN_FIELD_OWNERS: Mapping[str, tuple[str, ...]] = {
    "status": (_RUN_STORE, _RUN_TERMINALIZER, _DOMAIN_MIGRATION),
    "finished_at": (_RUN_STORE, _RUN_TERMINALIZER, _DOMAIN_MIGRATION),
    "current_attempt_id": (
        _RUN_STORE,
        "src/frisket/execution/attempt.py",
        "src/frisket/engine/store/output_claims.py",
        _DOMAIN_MIGRATION,
    ),
    "cancel_requested_at": (_RUN_STORE, "src/frisket/engine/store/changes.py"),
}


TRANSACTION_SCOPE = ("src/frisket/engine/store", "src/frisket/engine/jobs")
TRANSACTION_OWNER = "src/frisket/engine/store/transactions.py"
# Boundaries that do not fit ``write_transaction``: savepoint scopes, busy
# retries, commit flags that also commit a caller's transaction, deferred
# read-or-write scopes, separate databases, and the job queue's dialect seam.
# Shrink-only: an entry that no longer spells the literal fails the check.
HAND_WRITTEN_TRANSACTIONS = frozenset(
    {
        "src/frisket/engine/jobs/queue.py",
        "src/frisket/engine/jobs/rate_limiter.py",
        "src/frisket/engine/store/blob_metadata.py",
        "src/frisket/engine/store/cells.py",
        "src/frisket/engine/store/column_descriptors.py",
        "src/frisket/engine/store/evaluation.py",
        "src/frisket/engine/store/execution_routes.py",
        "src/frisket/engine/store/project_blobs.py",
        "src/frisket/engine/store/result_generations.py",
        "src/frisket/engine/store/runs.py",
    }
)


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
    field: str | None = None


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


def _run_update_fields(tail: str) -> set[str]:
    """Read only SET targets, including SQLite row-value assignments.

    This is a bounded token scan, not expression evaluation. Parenthesis depth
    skips subqueries/function arguments; quoted identifiers stay single tokens.
    ``*`` means a dynamic or unsupported target, never proof of metadata-only SQL.
    """
    header = re.match(rf"\s+(?:(?:AS\s+)?{_IDENT}\s+)?SET\b", tail, re.I)
    if header is None:
        return {"*"}
    fields: set[str] = set()
    lhs: list[str] = []
    depth = 0
    in_value = False
    for match in re.finditer(rf"{_IDENT}|\S", tail[header.end() :]):
        token = match.group()
        if depth == 0 and token.upper() in {
            "WHERE",
            "FROM",
            "RETURNING",
            "ORDER",
            "LIMIT",
            ";",
        }:
            break
        if token == "=" and depth == 0 and not in_value:
            target = " ".join(lhs)
            if re.fullmatch(_IDENT, target):
                fields.add(_table_name(target))
            elif re.fullmatch(rf"\(\s*{_IDENT}(?:\s*,\s*{_IDENT})*\s*\)", target):
                fields.update(
                    _table_name(t.group()) for t in re.finditer(_IDENT, target)
                )
            else:
                fields.add("*")
            in_value = True
        elif token == "," and depth == 0:
            if not in_value:
                fields.add("*")
            lhs, in_value = [], False
        elif not in_value:
            lhs.append(token)
        depth += (token == "(") - (token == ")")
    if not in_value:
        fields.add("*")
    return fields


def _writes(sql: str) -> Iterable[tuple[str, str, str | None]]:
    sql = _SQL_NON_CODE.sub(" ", sql)
    for pattern in _WRITE_PATTERNS:
        for match in pattern.finditer(sql):
            table = _table_name(match.group("table"))
            operation = match.group(0).split()[0].upper()
            if table == "runs":
                if operation == "UPDATE":
                    for field in sorted(_run_update_fields(sql[match.end() :])):
                        if field in RUN_FIELD_OWNERS or field == "*":
                            yield table, operation, field
                else:
                    # Inserting a row establishes lifecycle defaults too.
                    yield table, operation, None
            elif table in PROTECTED_TABLES or (
                operation == "DELETE" and table in DELETE_OWNERS
            ):
                yield table, operation, None


def _owners_for(
    table: str,
    operation: str,
    owners: Mapping[str, tuple[str, ...]],
    field: str | None = None,
) -> tuple[str, ...] | None:
    if table == "runs":
        return RUN_FIELD_OWNERS.get(field, (_RUN_STORE,))
    if table in PROTECTED_TABLES:
        return owners.get(table, ()) + (
            DELETE_OWNERS.get(table, ()) if operation == "DELETE" else ()
        )
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
        for table, operation, field in _writes(sql):
            allowed = _owners_for(table, operation, owners, field)
            if allowed is not None and relative not in allowed:
                violations.append(Violation(path, node.lineno, table, operation, field))
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


def _spells_begin_immediate(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.strip().upper().startswith("BEGIN IMMEDIATE")
    ]


def check_transaction_owners(
    *,
    root: Path,
    scope: Iterable[str] = TRANSACTION_SCOPE,
    owner: str = TRANSACTION_OWNER,
    allowed: frozenset[str] = HAND_WRITTEN_TRANSACTIONS,
) -> list[str]:
    problems: list[str] = []
    spelled: set[str] = set()
    for directory in scope:
        for path in sorted((root / directory).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            lines = _spells_begin_immediate(path)
            if not lines:
                continue
            spelled.add(relative)
            if relative != owner and relative not in allowed:
                problems.extend(
                    f"{relative}:{line}: BEGIN IMMEDIATE outside {owner}; "
                    "use write_transaction"
                    for line in lines
                )
    problems.extend(
        f"{relative}: no longer spells BEGIN IMMEDIATE; delete it from "
        "HAND_WRITTEN_TRANSACTIONS"
        for relative in sorted(allowed - spelled)
    )
    return problems


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
            f"protected target {violation.table!r}"
            f"{'.' + violation.field if violation.field else ''} "
            "is outside its named owner"
        )
    problems = [] if args.paths else check_transaction_owners(root=args.root)
    for problem in problems:
        print(problem)
    return 1 if violations or problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

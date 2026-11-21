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
remaining hand-written boundaries are an exact, shrink-only list. Explicit
``.cursor()`` calls are an exact, shrink-only list as well: the project
connection rolls back the implicit transaction a failing ``execute`` opened,
and a cursor's statements bypass that rule. Server work handed to another
thread ends its unit of work there: a hand-off in the server and team
packages goes through ``await_thread_worker`` or wraps its callable in
``ends_unit_of_work``.
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
# Boundaries that do not fit ``write_transaction``, each for the reason given.
# Shrink-only: an entry that no longer spells the literal fails the check.
HAND_WRITTEN_TRANSACTIONS = frozenset(
    {
        # SQLAlchemy dialect seam: IMMEDIATE on SQLite, plain on Postgres.
        "src/frisket/engine/jobs/queue.py",
        # Its own database and connection, outside the project store.
        "src/frisket/engine/jobs/rate_limiter.py",
        # Savepoint scopes, and add_sheet owning only under a parent Change.
        "src/frisket/engine/store/cells.py",
        # An editor write that nests as a savepoint inside a caller's transaction.
        "src/frisket/engine/store/evaluation.py",
        # Busy-timeout retry around the route chain write.
        "src/frisket/engine/store/execution_routes.py",
        # Deferred read-or-write scopes and staged garbage-collection commits.
        "src/frisket/engine/store/project_blobs.py",
        # Savepoint scopes around result and provider-fact batches.
        "src/frisket/engine/store/runs.py",
    }
)


CURSOR_SCOPE = "src/frisket"
# Explicit cursors, counted per module. Each project-connection cursor runs
# only inside a block that owns how its transaction ends (BEGIN IMMEDIATE, a
# savepoint, write_transaction, or the connection as a context manager), so a
# failing statement is that owner's to roll back. A new write goes through
# the connection's own ``execute``. Shrink-only: a count that no longer
# matches fails the check.
EXPLICIT_CURSORS: Mapping[str, int] = {
    "src/frisket/engine/executor/action_lifecycle.py": 1,
    "src/frisket/engine/executor/column_transform_action.py": 2,
    "src/frisket/engine/executor/group_summary_publication.py": 1,
    "src/frisket/engine/executor/group_summary_runtime.py": 1,
    "src/frisket/engine/executor/map_find_publication.py": 1,
    "src/frisket/engine/executor/page_capture.py": 1,
    "src/frisket/engine/executor/plugin_write_apply.py": 1,
    "src/frisket/engine/executor/sheet_refresh_action.py": 1,
    "src/frisket/engine/executor/table_action.py": 1,
    "src/frisket/engine/executor/temporal_extract_publication.py": 1,
    "src/frisket/engine/store/cells.py": 2,
    "src/frisket/engine/store/derived_updates.py": 1,
    "src/frisket/engine/store/citations.py": 1,
    "src/frisket/features/graph/sheet_graph.py": 1,
    "src/frisket/server/feed_enqueue.py": 1,
    # The job queue's own database, not a project connection.
    "src/frisket/engine/jobs/queue.py": 2,
    "src/frisket/engine/jobs/queue_provision.py": 1,
}


HANDOFF_SCOPE = ("src/frisket/server", "src/frisket/team")
# The one module that hands work to the executor itself.
HANDOFF_OWNER = "src/frisket/server/thread_worker.py"
_HANDOFF_CALLS = frozenset(
    {"to_thread", "run_in_threadpool", "run_in_executor", "run_sync"}
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


def _cursor_call_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "cursor"
        and not node.args
        and not node.keywords
    ]


def check_explicit_cursors(
    *,
    root: Path,
    scope: str = CURSOR_SCOPE,
    allowed: Mapping[str, int] = EXPLICIT_CURSORS,
) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for path in sorted((root / scope).rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        lines = _cursor_call_lines(path)
        if not lines:
            continue
        seen.add(relative)
        expected = allowed.get(relative, 0)
        if len(lines) > expected:
            problems.extend(
                f"{relative}:{line}: explicit .cursor(); write through the "
                "connection's execute so a failing statement ends the "
                "transaction it opened"
                for line in lines
            )
        elif len(lines) < expected:
            problems.append(
                f"{relative}: {len(lines)} explicit .cursor() calls; lower its "
                "EXPLICIT_CURSORS count"
            )
    problems.extend(
        f"{relative}: no explicit .cursor() calls; delete it from EXPLICIT_CURSORS"
        for relative in sorted(set(allowed) - seen)
    )
    return problems


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _bare_handoff_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or _call_name(node.func) not in _HANDOFF_CALLS
        ):
            continue
        callable_index = 1 if _call_name(node.func) == "run_in_executor" else 0
        target = node.args[callable_index] if len(node.args) > callable_index else None
        if not (
            isinstance(target, ast.Call)
            and _call_name(target.func) == "ends_unit_of_work"
        ):
            lines.append(node.lineno)
    return sorted(lines)


def check_thread_handoffs(
    *,
    root: Path,
    scope: Iterable[str] = HANDOFF_SCOPE,
    owner: str = HANDOFF_OWNER,
) -> list[str]:
    problems: list[str] = []
    for directory in scope:
        for path in sorted((root / directory).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if relative == owner:
                continue
            problems.extend(
                f"{relative}:{line}: thread hand-off without a unit of work; use "
                "await_thread_worker or wrap the callable in ends_unit_of_work"
                for line in _bare_handoff_lines(path)
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
    problems = (
        []
        if args.paths
        else check_transaction_owners(root=args.root)
        + check_explicit_cursors(root=args.root)
        + check_thread_handoffs(root=args.root)
    )
    for problem in problems:
        print(problem)
    return 1 if violations or problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

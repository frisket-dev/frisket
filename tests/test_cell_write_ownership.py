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


def test_isolated_project_migrations_have_only_their_named_write_authority(tmp_path):
    # rule19: synthetic SQL verifies exact upgrade owners without running migrations.
    directory = tmp_path / "src/frisket/upgrades/project"
    directory.mkdir(parents=True)
    fixtures = {
        "domain_migration.py": (
            'db.execute("UPDATE execution_attempts SET state=1")\n'
            'db.execute("UPDATE runs SET status=1,finished_at=1,current_attempt_id=1")\n'
            'db.execute("UPDATE results SET value=1")\n'
        ),
        "domain_document_text_migration.py": (
            'db.execute("INSERT INTO cells VALUES (1)")\n'
            'db.execute("INSERT INTO edits VALUES (1)")\n'
            'db.execute("INSERT INTO base_cell_producers VALUES (1)")\n'
            'db.execute("INSERT INTO current_cells VALUES (1)")\n'
        ),
        "historical_current_cells.py": 'db.execute("UPDATE current_cells SET value=1")\n',
    }
    for name, source in fixtures.items():
        path = directory / name
        path.write_text(source)
        assert checker.check_paths([path], root=tmp_path) == []
    arbitrary = directory / "arbitrary_migration.py"
    arbitrary.write_text('db.execute("UPDATE current_cells SET value=1")\n')
    assert len(checker.check_paths([arbitrary], root=tmp_path)) == 1


def test_offline_purge_owns_deletion_but_cannot_publish_results(tmp_path: Path):
    # rule19: synthetic source tests the operation-specific owner boundary.
    path = tmp_path / "src/frisket/engine/store/purge.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        'db.execute("DELETE FROM results WHERE run_id=?")\n'
        'db.execute("INSERT INTO results VALUES (?)")\n'
        'db.execute("UPDATE results SET value=NULL")\n'
    )
    violations = checker.check_paths([path], root=tmp_path)
    assert [(v.table, v.operation) for v in violations] == [
        ("results", "INSERT"),
        ("results", "UPDATE"),
    ]


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


def test_run_progress_and_predicates_do_not_become_lifecycle_writes(tmp_path: Path):
    source = """
SQL = "UPDATE runs AS r SET completed_rows=completed_rows+1, "
SQL = SQL + "work_progress=(SELECT status FROM jobs WHERE finished_at IS NULL) "
SQL = SQL + "WHERE r.status='running' AND r.current_attempt_id=?"
statement = SQL
db.execute(statement)
db.execute("UPDATE runs SET review_completed_at=CASE WHEN ? THEN datetime('now') END WHERE status<>'running'")
"""
    assert _scan(tmp_path, source) == []


def test_run_lifecycle_targets_are_checked_with_aliases_and_static_sql(tmp_path: Path):
    source = """
table = "runs"
field = "status"
sql = f"UPDATE main.{table} AS r SET {field}=? WHERE r.id=?"
statement = sql
db.execute(statement)
db.executemany('UPDATE "runs" SET [finished_at]=NULL, `current_attempt_id`=?', rows)
db.executescript("UPDATE runs SET cancel_requested_at=datetime('now'); UPDATE runs SET completed_rows=1")
"""
    violations = _scan(tmp_path, source)
    assert {(v.table, v.operation, v.field) for v in violations} == {
        ("runs", "UPDATE", field)
        for field in (
            "status",
            "finished_at",
            "current_attempt_id",
            "cancel_requested_at",
        )
    }


def test_run_tuple_assignments_and_dynamic_targets_are_not_a_bypass(tmp_path: Path):
    source = """
db.execute("UPDATE runs SET (status, finished_at) = (?, ?), work_progress=NULL")
db.execute(f"UPDATE runs SET {assignments} WHERE id=?")
db.execute(f"UPDATE runs SET work_progress={progress} WHERE id=?")
"""
    assert [v.field for v in _scan(tmp_path, source)] == ["finished_at", "status", "*"]


def test_run_creation_and_deletion_require_lifecycle_owner(tmp_path: Path):
    source = """
db.execute("INSERT INTO runs (sheet_id) VALUES (?)")
db.execute("REPLACE INTO runs (sheet_id) VALUES (?)")
db.execute("DELETE FROM runs WHERE id=?")
"""
    assert [(v.table, v.operation) for v in _scan(tmp_path, source)] == [
        ("runs", "INSERT"),
        ("runs", "REPLACE"),
        ("runs", "DELETE"),
    ]


def test_change_owner_may_request_stop_but_cannot_complete_a_run(tmp_path: Path):
    path = tmp_path / "src/frisket/engine/store/changes.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "db.execute(\"UPDATE runs SET cancel_requested_at=COALESCE(cancel_requested_at,datetime('now')) WHERE status='running'\")\n"
        "db.execute(\"UPDATE runs SET status='completed',finished_at=datetime('now')\")\n"
        "db.execute(\"UPDATE execution_attempts SET state='effected'\")\n"
    )
    violations = checker.check_paths([path], root=tmp_path)
    assert {(v.table, v.field) for v in violations} == {
        ("runs", "status"),
        ("runs", "finished_at"),
        ("execution_attempts", None),
    }


def test_run_owner_and_named_attempt_owner_are_allowed(tmp_path: Path):
    run_owner = tmp_path / "src/frisket/engine/store/runs.py"
    run_owner.parent.mkdir(parents=True)
    run_owner.write_text(
        'db.execute("INSERT INTO runs (sheet_id) VALUES (?)")\n'
        'db.execute("UPDATE runs SET status=?,finished_at=NULL,cancel_requested_at=NULL,current_attempt_id=NULL")\n'
    )
    attempt_owner = tmp_path / "src/frisket/execution/attempt.py"
    attempt_owner.parent.mkdir(parents=True)
    attempt_owner.write_text(
        'db.execute("INSERT INTO execution_attempts (id,state) VALUES (?,?)")\n'
        'db.execute("UPDATE runs SET current_attempt_id=? WHERE id=?")\n'
    )
    assert checker.check_paths([run_owner, attempt_owner], root=tmp_path) == []


def test_comments_literals_and_nested_rhs_do_not_create_lifecycle_targets(
    tmp_path: Path,
):
    source = """
db.executescript("UPDATE runs SET params='status=failed; UPDATE runs SET status=1', completed_rows=(SELECT count(*) FROM runs WHERE finished_at IS NOT NULL); -- UPDATE runs SET status=1")
db.execute("/* UPDATE execution_attempts SET state='effected' */ SELECT 1")
"""
    assert _scan(tmp_path, source) == []


def test_begin_immediate_belongs_to_the_transaction_owner(tmp_path: Path):
    # rule19: synthetic modules verify the transaction-owner gate without a repo.
    store = tmp_path / "src/store"
    store.mkdir(parents=True)
    (store / "transactions.py").write_text('db.execute("BEGIN IMMEDIATE")\n')
    (store / "rogue.py").write_text('db.execute("BEGIN IMMEDIATE")\n')
    (store / "prose.py").write_text('"""Runs under BEGIN IMMEDIATE."""\n')
    (store / "listed.py").write_text('db.execute("begin immediate")\n')
    problems = checker.check_transaction_owners(
        root=tmp_path,
        scope=("src/store",),
        owner="src/store/transactions.py",
        allowed=frozenset({"src/store/listed.py", "src/store/gone.py"}),
    )
    assert problems == [
        "src/store/rogue.py:1: BEGIN IMMEDIATE outside src/store/transactions.py; "
        "use write_transaction",
        "src/store/gone.py: no longer spells BEGIN IMMEDIATE; delete it from "
        "HAND_WRITTEN_TRANSACTIONS",
    ]


def test_explicit_cursors_are_an_exact_list(tmp_path: Path):
    # rule19: synthetic modules verify the cursor gate without a repo.
    package = tmp_path / "src/pkg"
    package.mkdir(parents=True)
    (package / "listed.py").write_text("cur = db.cursor()\n")
    (package / "rogue.py").write_text("x = 1\ncur = project.db.cursor()\n")
    (package / "shrunk.py").write_text("cur = db.cursor()\n")
    (package / "prose.py").write_text('"""Reads through db.cursor()."""\n')
    problems = checker.check_explicit_cursors(
        root=tmp_path,
        scope="src/pkg",
        allowed={"src/pkg/listed.py": 1, "src/pkg/shrunk.py": 2, "src/pkg/gone.py": 1},
    )
    assert problems == [
        "src/pkg/rogue.py:2: explicit .cursor(); write through the connection's "
        "execute so a failing statement ends the transaction it opened",
        "src/pkg/shrunk.py: 1 explicit .cursor() calls; lower its "
        "EXPLICIT_CURSORS count",
        "src/pkg/gone.py: no explicit .cursor() calls; delete it from EXPLICIT_CURSORS",
    ]


def test_server_thread_handoffs_end_a_unit_of_work(tmp_path: Path):
    # rule19: synthetic modules verify the hand-off gate without a repo.
    server = tmp_path / "src/server"
    server.mkdir(parents=True)
    (server / "thread_worker.py").write_text(
        "loop.run_in_executor(None, ctx.run, work)\n"
    )
    (server / "routes.py").write_text(
        "await asyncio.to_thread(ends_unit_of_work(work), 1)\n"
        "await run_in_threadpool(ends_unit_of_work(work))\n"
        "await asyncio.to_thread(work)\n"
        "await run_in_threadpool(work)\n"
        "loop.run_in_executor(None, work)\n"
        "await anyio.to_thread.run_sync(work)\n"
    )
    problems = checker.check_thread_handoffs(
        root=tmp_path, scope=("src/server",), owner="src/server/thread_worker.py"
    )
    suffix = (
        ": thread hand-off without a unit of work; use await_thread_worker "
        "or wrap the callable in ends_unit_of_work"
    )
    assert problems == [f"src/server/routes.py:{line}{suffix}" for line in (3, 4, 5, 6)]


def test_commit_parameters_are_banned_outside_transactions_module(tmp_path: Path):
    # rule19: synthetic modules verify the commit-parameter gate without a repo.
    pkg = tmp_path / "src/pkg"
    pkg.mkdir(parents=True)
    (pkg / "transactions.py").write_text(
        "def write_transaction(db, *, commit: bool = True):\n    pass\n"
    )
    (pkg / "store.py").write_text(
        "def save_row(db, *, commit: bool = True):\n"
        "    pass\n"
        "async def async_save(db, commit=False):\n"
        "    pass\n"
        "def save_row_in_transaction(db):\n"
        "    commit = True\n"
        "    return commit\n"
    )
    problems = checker.check_commit_parameters(
        root=tmp_path,
        scope="src/pkg",
        owner="src/pkg/transactions.py",
    )
    assert problems == [
        "src/pkg/store.py:1: function 'save_row' defines a 'commit' parameter "
        "outside src/pkg/transactions.py; split owning and *_in_transaction write paths",
        "src/pkg/store.py:3: function 'async_save' defines a 'commit' parameter "
        "outside src/pkg/transactions.py; split owning and *_in_transaction write paths",
    ]

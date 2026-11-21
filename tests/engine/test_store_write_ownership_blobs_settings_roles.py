"""Transaction ownership for blob, settings, op-log, document-text and
column-retirement writes.

Each case checks committed state from a second SQLite connection:
- owning writes commit atomically when no transaction is open, refuse inside a
  caller's open transaction without publishing it, and roll back cleanly on a
  mid-operation failure;
- joining writes refuse outside a caller transaction and roll back with their
  caller (including on mid-operation failure).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from frisket.engine.executor.blob_outputs import commit_row_blob_plan, stage_blob_bytes
from frisket.engine.runner.column_retirement import (
    retire_dropped_generated_columns,
    retire_unbound_created_columns,
)
from frisket.engine.store.document_text import ensure_document_text_role
from frisket.engine.store.transactions import write_transaction
from frisket.ops.base import ActionProgram
from frisket.project_settings import (
    PROJECT_MCP_SERVERS_META_KEY,
    PROJECT_SETTINGS_META_KEY,
    ProjectSettings,
    write_project_mcp_servers,
    write_project_settings,
)

CALLER_SHEET_NAME = "Caller edit"


@pytest.fixture
def project(make_project):
    project = make_project("pass6-ownership")
    sheet_id = project.add_sheet("Source")
    column_id = project.add_column(sheet_id, "value")
    project.add_rows(sheet_id, [{"value": "original"}], {"value": column_id})
    try:
        yield project
    finally:
        if project.db.in_transaction:
            project.db.rollback()
        project.close()


def _sheet_id(project) -> int:
    return int(project.db.execute("SELECT id FROM sheets").fetchone()[0])


def _committed(project, sql: str, params: tuple[object, ...] = ()):
    peer = sqlite3.connect(project.db_path)
    try:
        return peer.execute(sql, params).fetchall()
    finally:
        peer.close()


def _committed_count(project, table: str) -> int:
    return int(_committed(project, f"SELECT COUNT(*) FROM {table}")[0][0])


def _open_caller_transaction(project) -> None:
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute(
        "UPDATE sheets SET name=? WHERE id=?", (CALLER_SHEET_NAME, _sheet_id(project))
    )


def _assert_caller_work_kept_uncommitted(project) -> None:
    assert project.db.in_transaction
    assert (
        _committed(project, "SELECT name FROM sheets ORDER BY id LIMIT 1")[0][0]
        == "Source"
    )


def _assert_nothing_left_behind(project) -> None:
    assert not project.db.in_transaction
    project.db.commit()


def _refuse(project, statement: str, table: str) -> None:
    project.db.execute(
        f"CREATE TEMP TRIGGER refuse_{statement.lower()}_{table} "
        f"BEFORE {statement} ON main.{table} "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )


# -- project_blobs and blob_outputs ------------------------------------------


def test_add_blob_owns_its_transaction_and_refuses_inside_caller_transaction(project):
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        project.add_blob(b"owned bytes", filename="owned.bin")

    _assert_caller_work_kept_uncommitted(project)
    assert _committed_count(project, "blobs") == 0
    project.db.rollback()

    digest = project.add_blob(b"owned bytes", filename="owned.bin")
    assert not project.db.in_transaction
    assert _committed(project, "SELECT hash FROM blobs WHERE hash=?", (digest,)) == [
        (digest,)
    ]


def test_add_blob_rolls_back_row_when_metadata_merge_fails(project):
    _refuse(project, "UPDATE", "blobs")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        project.add_blob(
            b"atomic blob bytes",
            filename="atomic.bin",
            metadata={"probe": {"kind": "file"}},
        )

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "blobs") == 0


def test_add_blob_from_path_requires_and_rolls_back_with_caller_transaction(
    project, tmp_path: Path
):
    source = tmp_path / "clip.bin"
    source.write_bytes(b"streamed blob bytes")

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        project.add_blob_from_path(source, filename="clip.bin")
    assert not project.db.in_transaction
    assert _committed_count(project, "blobs") == 0

    _open_caller_transaction(project)
    digest = project.add_blob_from_path(source, filename="clip.bin")
    assert project.db.in_transaction
    assert _committed_count(project, "blobs") == 0
    project.db.rollback()
    assert _committed(project, "SELECT hash FROM blobs WHERE hash=?", (digest,)) == []


def test_record_blob_derivation_requires_and_rolls_back_with_caller_transaction(
    project,
):
    source = project.add_blob(b"source audio", filename="src.wav")
    derived = project.add_blob(b"derived clip", filename="clip.wav")

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        project.record_blob_derivation(
            derived_hash=derived, source_hash=source, op="clip"
        )
    assert not project.db.in_transaction
    assert _committed_count(project, "blob_derivations") == 0

    _open_caller_transaction(project)
    project.record_blob_derivation(derived_hash=derived, source_hash=source, op="clip")
    assert project.db.in_transaction
    assert _committed_count(project, "blob_derivations") == 0
    project.db.rollback()
    assert _committed_count(project, "blob_derivations") == 0


def test_commit_row_blob_plan_requires_and_rolls_back_with_caller_transaction(project):
    plan = stage_blob_bytes(
        project,
        b"captured page bytes",
        role="primary",
        filename="page.html",
        mime="text/html",
    )

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        commit_row_blob_plan(project, plan)
    assert not project.db.in_transaction
    assert _committed_count(project, "blobs") == 0

    _open_caller_transaction(project)
    digest = commit_row_blob_plan(project, plan)
    assert digest == plan.content_digest
    assert project.db.in_transaction
    assert _committed_count(project, "blobs") == 0
    project.db.rollback()
    assert _committed_count(project, "blobs") == 0


# -- op_log.set_undo_info ----------------------------------------------------


def test_set_undo_info_requires_and_rolls_back_with_caller_transaction(project):
    col_id = project.add_column(_sheet_id(project), "generated", ai_generated=True)
    op_id = project.append_change("map", {"recipe": "probe"})

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        project.set_undo_info(op_id, {"created_columns": [col_id]})
    assert not project.db.in_transaction
    assert _committed(project, "SELECT undo_info FROM ops WHERE id=?", (op_id,)) == [
        ("{}",)
    ]

    _open_caller_transaction(project)
    project.set_undo_info(op_id, {"created_columns": [col_id]})
    assert project.db.in_transaction
    assert _committed(project, "SELECT undo_info FROM ops WHERE id=?", (op_id,)) == [
        ("{}",)
    ]
    project.db.rollback()
    assert _committed(project, "SELECT undo_info FROM ops WHERE id=?", (op_id,)) == [
        ("{}",)
    ]


# -- project_settings --------------------------------------------------------


def test_write_project_settings_requires_and_rolls_back_with_caller_transaction(
    project,
):
    settings = ProjectSettings(media_allow_private_hosts=False)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        write_project_settings(project, settings)
    assert not project.db.in_transaction
    assert (
        _committed(
            project,
            "SELECT value FROM meta WHERE key=?",
            (PROJECT_SETTINGS_META_KEY,),
        )
        == []
    )

    _open_caller_transaction(project)
    write_project_settings(project, settings)
    assert project.db.in_transaction
    project.db.rollback()
    assert (
        _committed(
            project,
            "SELECT value FROM meta WHERE key=?",
            (PROJECT_SETTINGS_META_KEY,),
        )
        == []
    )


def test_write_project_mcp_servers_requires_and_rolls_back_with_caller_transaction(
    project,
):
    servers = [{"id": "s1", "name": "Demo"}]

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        write_project_mcp_servers(project, servers)
    assert not project.db.in_transaction
    assert (
        _committed(
            project,
            "SELECT value FROM meta WHERE key=?",
            (PROJECT_MCP_SERVERS_META_KEY,),
        )
        == []
    )

    _open_caller_transaction(project)
    write_project_mcp_servers(project, servers)
    assert project.db.in_transaction
    project.db.rollback()
    assert (
        _committed(
            project,
            "SELECT value FROM meta WHERE key=?",
            (PROJECT_MCP_SERVERS_META_KEY,),
        )
        == []
    )


# -- document_text -----------------------------------------------------------


def test_ensure_document_text_role_requires_and_rolls_back_with_caller_transaction(
    project,
):
    sheet_id = _sheet_id(project)
    file_col = project.add_column(sheet_id, "Document", type="file")
    text_col = project.add_column(sheet_id, "Text", type="text")

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        ensure_document_text_role(
            project, source_column_id=file_col, text_column_id=text_col
        )
    assert not project.db.in_transaction
    assert _committed_count(project, "document_text_column_roles") == 0

    _open_caller_transaction(project)
    role = ensure_document_text_role(
        project, source_column_id=file_col, text_column_id=text_col
    )
    assert role.text_column_id == text_col
    assert project.db.in_transaction
    assert _committed_count(project, "document_text_column_roles") == 0
    project.db.rollback()
    assert _committed_count(project, "document_text_column_roles") == 0


# -- column_retirement -------------------------------------------------------


class _RetireProgram(ActionProgram):
    consumes_resolution = False
    cost_class = "free"

    def retired_output_names(self, spec: dict) -> list[str]:
        return ["out_confidence"]


def test_retire_dropped_generated_columns_requires_caller_txn_and_is_atomic(project):
    sheet_id = _sheet_id(project)
    op_id = project.append_change("map", {"action_kind": "map.fake"})
    project.add_column(sheet_id, "out", ai_generated=True)
    dropped = project.add_column(sheet_id, "out_confidence", ai_generated=True)
    columns = project.columns(sheet_id, include_hidden=True)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        retire_dropped_generated_columns(
            project,
            op_id=op_id,
            program=_RetireProgram(),
            spec={"action_kind": "map.fake"},
            columns=columns,
            current_output_names={"out"},
        )
    assert not project.db.in_transaction
    assert _committed(
        project, "SELECT default_hidden FROM columns WHERE id=?", (dropped,)
    ) == [(0,)]

    _refuse(project, "UPDATE", "ops")
    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        with write_transaction(project.db):
            retire_dropped_generated_columns(
                project,
                op_id=op_id,
                program=_RetireProgram(),
                spec={"action_kind": "map.fake"},
                columns=columns,
                current_output_names={"out"},
            )

    _assert_nothing_left_behind(project)
    assert _committed(
        project, "SELECT default_hidden FROM columns WHERE id=?", (dropped,)
    ) == [(0,)]
    assert _committed(project, "SELECT undo_info FROM ops WHERE id=?", (op_id,)) == [
        ("{}",)
    ]


def test_retire_unbound_created_columns_owns_and_rolls_back_atomically(project):
    sheet_id = _sheet_id(project)
    draft_col = project.add_column(sheet_id, "draft", ai_generated=True)
    op_id = project.append_change("map", {"action_kind": "map.fake"})
    with write_transaction(project.db):
        project.set_undo_info(op_id, {"created_columns": [draft_col]})

    _open_caller_transaction(project)
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        retire_unbound_created_columns(project, op_id, frozenset({draft_col}))
    _assert_caller_work_kept_uncommitted(project)
    assert _committed(
        project, "SELECT active, hidden FROM columns WHERE id=?", (draft_col,)
    ) == [(1, 0)]
    project.db.rollback()

    _refuse(project, "UPDATE", "ops")
    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        retire_unbound_created_columns(project, op_id, frozenset({draft_col}))

    _assert_nothing_left_behind(project)
    assert _committed(
        project, "SELECT active, hidden FROM columns WHERE id=?", (draft_col,)
    ) == [(1, 0)]

    project.db.execute("DROP TRIGGER temp.refuse_update_ops")
    retire_unbound_created_columns(project, op_id, frozenset({draft_col}))
    assert not project.db.in_transaction
    assert _committed(
        project, "SELECT active, hidden FROM columns WHERE id=?", (draft_col,)
    ) == [(0, 1)]
    stored_info = json.loads(
        _committed(project, "SELECT undo_info FROM ops WHERE id=?", (op_id,))[0][0]
    )
    assert stored_info["created_columns"] == []

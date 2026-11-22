from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store.media_blobs import media_cell


def test_apply_edits_default_commit_preserves_ambient_transaction(
    make_project,
) -> None:
    project = make_project("ambient-edits")
    sheet_id = project.add_sheet("Source")
    column_id = project.add_column(sheet_id, "value")
    row_id = project.add_rows(
        sheet_id,
        [{"value": "original"}],
        {"value": column_id},
    )[0]

    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("UPDATE sheets SET name='Sentinel' WHERE id=?", (sheet_id,))
    project.apply_edits([{"row_id": row_id, "column_id": column_id, "value": "edited"}])

    assert project.db.in_transaction
    assert project.get_values(sheet_id, column_id) == {row_id: "edited"}

    project.db.rollback()

    assert (
        project.db.execute(
            "SELECT name FROM sheets WHERE id=?", (sheet_id,)
        ).fetchone()["name"]
        == "Source"
    )
    assert project.get_values(sheet_id, column_id) == {row_id: "original"}
    assert (
        project.db.execute("SELECT COUNT(*) FROM ops WHERE kind='edit'").fetchone()[0]
        == 0
    )


def test_apply_edits_failure_rolls_back_only_its_savepoint(make_project) -> None:
    project = make_project("failed-ambient-edits")
    sheet_id = project.add_sheet("Source")
    column_id = project.add_column(sheet_id, "value")
    row_id = project.add_rows(
        sheet_id,
        [{"value": "original"}],
        {"value": column_id},
    )[0]

    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("UPDATE sheets SET name='Sentinel' WHERE id=?", (sheet_id,))
    with pytest.raises(ValueError, match="same sheet"):
        project.apply_edits(
            [{"row_id": row_id, "column_id": column_id + 10_000, "value": "bad"}]
        )

    assert project.db.in_transaction
    assert (
        project.db.execute(
            "SELECT name FROM sheets WHERE id=?", (sheet_id,)
        ).fetchone()["name"]
        == "Sentinel"
    )
    assert (
        project.db.execute("SELECT COUNT(*) FROM ops WHERE kind='edit'").fetchone()[0]
        == 0
    )
    project.db.rollback()


def test_add_rows_in_transaction_preserves_ambient_transaction_and_owning_refuses(
    make_project,
) -> None:
    project = make_project("ambient-rows")
    sheet_id = project.add_sheet("Source")
    column_id = project.add_column(sheet_id, "value")

    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("UPDATE sheets SET name='Sentinel' WHERE id=?", (sheet_id,))
    with pytest.raises(sqlite3.OperationalError):
        project.add_rows(
            sheet_id,
            [{"value": "rejected"}],
            {"value": column_id},
        )
    assert project.db.in_transaction
    row_ids = project.add_rows_in_transaction(
        sheet_id,
        [{"value": "uncommitted"}],
        {"value": column_id},
    )

    assert project.db.in_transaction
    assert project.get_values(sheet_id, column_id) == {row_ids[0]: "uncommitted"}

    project.db.rollback()

    assert (
        project.db.execute(
            "SELECT name FROM sheets WHERE id=?", (sheet_id,)
        ).fetchone()["name"]
        == "Source"
    )
    assert project.row_count(sheet_id) == 0
    assert project.get_values(sheet_id, column_id) == {}
    assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0


def test_delete_sheet_rejects_ambient_transaction_before_changes_or_gc(
    make_project,
) -> None:
    project = make_project("ambient-delete")
    keep_id = project.add_sheet("Keep")
    sheet_id = project.add_sheet("Delete")
    column_id = project.add_column(sheet_id, "document", "file")
    digest = project.add_blob(
        b"must survive rollback", filename="document.txt", mime="text/plain"
    )
    project.add_rows(
        sheet_id,
        [{"document": media_cell(digest, filename="document.txt", mime="text/plain")}],
        {"document": column_id},
    )

    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("UPDATE sheets SET name='Sentinel' WHERE id=?", (keep_id,))

    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        project.delete_sheet(sheet_id)

    assert project.db.in_transaction
    project.db.rollback()

    assert (
        project.db.execute("SELECT name FROM sheets WHERE id=?", (keep_id,)).fetchone()[
            "name"
        ]
        == "Keep"
    )
    assert (
        project.db.execute("SELECT 1 FROM sheets WHERE id=?", (sheet_id,)).fetchone()
        is not None
    )
    assert project.db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone()

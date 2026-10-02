from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.project import Project


def test_add_rows_default_commit_preserves_ambient_transaction(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "ambient-rows.frisket", "ambient rows")
    try:
        sheet_id = project.add_sheet("Source")
        column_id = project.add_column(sheet_id, "value")

        project.db.execute("BEGIN IMMEDIATE")
        project.db.execute("UPDATE sheets SET name='Sentinel' WHERE id=?", (sheet_id,))
        row_ids = project.add_rows(
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
    finally:
        project.close()


def test_delete_sheet_rejects_ambient_transaction_before_changes_or_gc(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "ambient-delete.frisket", "ambient delete")
    try:
        keep_id = project.add_sheet("Keep")
        sheet_id = project.add_sheet("Delete")
        column_id = project.add_column(sheet_id, "document", "file")
        digest = project.add_blob(
            b"must survive rollback", filename="document.txt", mime="text/plain"
        )
        project.add_rows(
            sheet_id,
            [
                {
                    "document": media_cell(
                        digest, filename="document.txt", mime="text/plain"
                    )
                }
            ],
            {"document": column_id},
        )

        project.db.execute("BEGIN IMMEDIATE")
        project.db.execute("UPDATE sheets SET name='Sentinel' WHERE id=?", (keep_id,))

        with pytest.raises(RuntimeError, match="caller-owned transaction"):
            project.delete_sheet(sheet_id)

        assert project.db.in_transaction
        project.db.rollback()

        assert (
            project.db.execute(
                "SELECT name FROM sheets WHERE id=?", (keep_id,)
            ).fetchone()["name"]
            == "Keep"
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM sheets WHERE id=?", (sheet_id,)
            ).fetchone()
            is not None
        )
        assert project.db.execute(
            "SELECT 1 FROM blobs WHERE hash=?", (digest,)
        ).fetchone()
    finally:
        project.close()

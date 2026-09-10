from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.project import Project
from frisket.engine.store.sheet_lifecycle import SheetDeleteBlocked


def test_delete_sheet_physically_removes_rows_columns_and_blob_metadata(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "delete.frisket", "delete")
    keep_id = project.add_sheet("Keep")
    sheet_id = project.add_sheet("Scratch")
    column_id = project.add_column(sheet_id, "document", "file")
    digest = project.add_blob(
        b"sheet-only bytes", filename="scratch.txt", mime="text/plain"
    )
    project.add_rows(
        sheet_id,
        [{"document": media_cell(digest, filename="scratch.txt", mime="text/plain")}],
        {"document": column_id},
    )

    result = project.delete_sheet(sheet_id)

    assert result == {
        "ok": True,
        "deleted_sheet_id": sheet_id,
        "deleted_sheet_name": "Scratch",
    }
    assert [int(row["id"]) for row in project.sheets()] == [keep_id]
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (sheet_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone()
        is None
    )
    op = project.db.execute(
        "SELECT kind, barrier FROM ops ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (op["kind"], op["barrier"]) == ("sheet.delete", 1)


def test_delete_sheet_blocks_dependent_sheets(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "dependencies.frisket", "dependencies")
    source_id = project.add_sheet("Source")
    child_id = project.add_sheet("Derived", parent_sheet_id=source_id)

    with pytest.raises(SheetDeleteBlocked, match="used by Derived"):
        project.delete_sheet(source_id)

    assert project.dependent_sheets(source_id) == [{"id": child_id, "name": "Derived"}]
    assert {int(row["id"]) for row in project.sheets()} == {source_id, child_id}

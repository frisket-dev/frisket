"""Reversible containment deletion; permanent cleanup has its own purge command."""

from contextlib import closing
import sqlite3

from frisket.engine.store.project import Project
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.runs import RunResultStore


def test_delete_sheet_retains_rows_columns_blobs_and_restores_atomically(tmp_path):
    with closing(Project.create(tmp_path / "delete.frisket")) as project:
        keep = project.add_sheet("Keep")
        sheet = project.add_sheet("Scratch")
        column = project.add_column(sheet, "document", "file")
        digest = project.add_blob(
            b"sheet-only bytes", filename="scratch.txt", mime="text/plain"
        )
        row = project.add_rows(
            sheet,
            [
                {
                    "document": media_cell(
                        digest, filename="scratch.txt", mime="text/plain"
                    )
                }
            ],
            {"document": column},
        )[0]
        result = project.delete_sheet(sheet)
        deleted = project.op_cursor
        assert result == {
            "ok": True,
            "deleted_sheet_id": sheet,
            "deleted_sheet_name": "Scratch",
        }
        assert [s["id"] for s in project.sheets()] == [keep]
        assert project.get_column(column) is not None
        assert project.db.execute("SELECT 1 FROM rows WHERE id=?", (row,)).fetchone()
        assert project.db.execute(
            "SELECT 1 FROM blobs WHERE hash=?", (digest,)
        ).fetchone()
        with project.blob_store.materialize(digest) as path:
            assert path.read_bytes() == b"sheet-only bytes"
        assert project.undo() == deleted
        assert [s["id"] for s in project.sheets()] == [keep, sheet]
        assert project.get_values(sheet, column)[row]["blob"] == digest
        assert project.redo() == deleted
        assert [s["id"] for s in project.sheets()] == [keep]
        assert list(project.db.execute("PRAGMA foreign_key_check")) == []


def test_delete_sheet_never_cascades_to_materialized_children(tmp_path):
    with closing(Project.create(tmp_path / "derived.frisket")) as project:
        source = project.add_sheet("Source")
        derived = project.add_sheet("Derived", parent_sheet_id=source)
        column = project.add_column(derived, "note")
        row = project.add_rows(derived, [{"note": "retained"}], {"note": column})[0]
        project.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
        project.delete_sheet(source)
        assert [s["id"] for s in project.sheets()] == [derived]
        assert project.get_values(derived, column)[row] == "retained"
        assert project.dependent_sheets(source) == [{"id": derived, "name": "Derived"}]
        assert project.undo()
        assert project.get_values(derived, column)[row] == "retained"


def test_delete_sheet_retains_completed_and_running_execution_records(tmp_path):
    with closing(Project.create(tmp_path / "runs.frisket")) as project:
        sheet = project.add_sheet("Running")
        op = project.append_op("media.read_pdf_text")
        run = RunResultStore(project).start_run(
            op, sheet, "media.read_pdf_text", total_rows=0
        )
        project.delete_sheet(sheet)
        assert (
            project.db.execute("SELECT status FROM ops WHERE id=?", (op,)).fetchone()[0]
            == "applied"
        )
        assert (
            project.db.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[
                0
            ]
            == "running"
        )
        # Deletion is a separate Change, never accounting cancellation/history purge.
        assert project.undo()
        assert [s["id"] for s in project.sheets()] == [sheet]

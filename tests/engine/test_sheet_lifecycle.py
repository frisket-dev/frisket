from __future__ import annotations

import sqlite3
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


def test_delete_sheet_removes_cross_sheet_dependencies_with_low_variable_limit(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "bounded-delete.frisket", "bounded delete")
    try:
        doomed_sheet = project.add_sheet("Doomed")
        surviving_sheet = project.add_sheet("Surviving")
        doomed_column = project.add_column(doomed_sheet, "doomed output")
        dependent_column = project.add_column(surviving_sheet, "dependent output")
        surviving_column = project.add_column(surviving_sheet, "surviving output")

        def artifact(sheet_id: int, stable_id: str) -> int:
            return int(
                project.db.execute(
                    "INSERT INTO source_artifacts "
                    "(stable_id,artifact_kind,media_type,source_sheet_id) "
                    "VALUES (?,?,?,?)",
                    (stable_id, "document", "text/plain", sheet_id),
                ).lastrowid
            )

        doomed_artifacts = [
            artifact(doomed_sheet, f"doomed-{index}") for index in range(5)
        ]
        cross_sheet_derived = artifact(surviving_sheet, "cross-sheet-derived")
        retained_source = artifact(surviving_sheet, "retained-source")
        retained_derived = artifact(surviving_sheet, "retained-derived")
        for ordinal, source_artifact_id in enumerate(doomed_artifacts):
            project.db.execute(
                "INSERT INTO artifact_timeline_segments "
                "(derived_artifact_id,ordinal,derived_start_ms,derived_end_ms,"
                "source_artifact_id,source_start_ms,source_end_ms,precision) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    cross_sheet_derived,
                    ordinal,
                    ordinal * 10,
                    ordinal * 10 + 10,
                    source_artifact_id,
                    ordinal * 10,
                    ordinal * 10 + 10,
                    "exact",
                ),
            )
        retained_timeline = int(
            project.db.execute(
                "INSERT INTO artifact_timeline_segments "
                "(derived_artifact_id,ordinal,derived_start_ms,derived_end_ms,"
                "source_artifact_id,source_start_ms,source_end_ms,precision) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    retained_derived,
                    0,
                    0,
                    10,
                    retained_source,
                    0,
                    10,
                    "exact",
                ),
            ).lastrowid
        )

        def completed_run(sheet_id: int) -> int:
            op_id = int(
                project.db.execute(
                    "INSERT INTO ops (kind,spec,status) VALUES (?,?,?)",
                    ("map.test", "{}", "discarded"),
                ).lastrowid
            )
            return int(
                project.db.execute(
                    "INSERT INTO runs (op_id,sheet_id,action_kind,status) "
                    "VALUES (?,?,?,?)",
                    (op_id, sheet_id, "map.test", "completed"),
                ).lastrowid
            )

        doomed_run = completed_run(doomed_sheet)
        surviving_run = completed_run(surviving_sheet)
        project.db.execute(
            "INSERT INTO run_output_generations "
            "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                doomed_run,
                doomed_column,
                "doomed",
                "text",
                "create",
                "active",
                "doomed-claim",
            ),
        )
        project.db.execute(
            "INSERT INTO run_output_generations "
            "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token,"
            "expected_base_run_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                surviving_run,
                dependent_column,
                "dependent",
                "text",
                "create",
                "active",
                "dependent-claim",
                doomed_run,
            ),
        )
        project.db.execute(
            "INSERT INTO run_output_generations "
            "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                surviving_run,
                surviving_column,
                "surviving",
                "text",
                "create",
                "active",
                "surviving-claim",
            ),
        )
        project.db.commit()
        project.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)

        project.delete_sheet(doomed_sheet)

        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM source_artifacts WHERE source_sheet_id=?",
                (doomed_sheet,),
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM source_artifacts WHERE id IN (?,?)",
                (cross_sheet_derived, retained_source),
            ).fetchone()[0]
            == 2
        )
        assert [
            int(row["id"])
            for row in project.db.execute("SELECT id FROM artifact_timeline_segments")
        ] == [retained_timeline]
        assert (
            project.db.execute(
                "SELECT 1 FROM runs WHERE id=?", (doomed_run,)
            ).fetchone()
            is None
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM runs WHERE id=?", (surviving_run,)
            ).fetchone()
            is not None
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM run_output_generations WHERE run_id=? AND column_id=?",
                (surviving_run, dependent_column),
            ).fetchone()
            is None
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM run_output_generations WHERE run_id=? AND column_id=?",
                (surviving_run, surviving_column),
            ).fetchone()
            is not None
        )
    finally:
        project.close()

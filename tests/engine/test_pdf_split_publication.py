from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store import op_log
from frisket.engine.store.import_sessions import ImportInProgress, ImportSessionConflict
from frisket.engine.store.pdf_split import PdfSplitSource, PdfSplitWriter
from frisket.engine.executor.import_read_guard import require_action_sheet_readable
from frisket.actions.types import TableError


def _seed(project: Project):
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "file", "file")
    values = [{"blob": f"{i:064x}", "filename": f"{i}.pdf"} for i in range(4)]
    rows = project.add_rows(
        sheet, [{"file": value} for value in values], {"file": column}
    )
    return sheet, column, rows, values


def _start(project, sheet, column, rows, values, *, replace=True, session="split:one"):
    return PdfSplitWriter.start(
        project,
        session_id=session,
        writer_authority="claim:one",
        sheet_id=sheet if replace else None,
        replace=replace,
        sheet_name="Children",
        columns=[{"name": "file", "type": "file"}],
        project_id="project",
        idempotency_key=session,
        params_hash=f"hash:{session}",
        action_id=f"act:{session}",
        receipt_id=f"receipt:{session}",
        sources=[
            PdfSplitSource(
                source_id=f"source:{index}",
                child_count=count,
                sheet_id=sheet,
                row_id=rows[index],
                column_id=column,
                value=values[index],
            )
            for index, count in [(0, 2), (1, 3)]
        ],
    )


def _children(count):
    return [{"file": {"blob": f"{index + 10:064x}"}} for index in range(count)]


def test_partial_split_is_one_fenced_operation_and_one_undo(tmp_path: Path):
    project = Project.create(tmp_path / "partial.frisket", name="partial")
    try:
        sheet, column, rows, values = _seed(project)
        before = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        writer = _start(project, sheet, column, rows, values)
        with pytest.raises(ImportInProgress):
            project.add_rows(sheet, [{"file": values[2]}], {"file": column})
        with pytest.raises(ImportInProgress):
            project.undo()
        with pytest.raises(TableError, match="unavailable"):
            require_action_sheet_readable(project, sheet)
        children = writer.publish_source(_children(2), expected_cursor=0)
        assert project.visible_row_ids(sheet) == [*children, *rows[1:]]
        other = project.add_sheet("Other")
        other_column = project.add_column(other, "note")
        project.add_rows(other, [{"note": "independent"}], {"note": other_column})
        project.undo()  # newer operations remain undoable while split is active
        assert project.visible_row_ids(other) == []
        project.redo()
        cursor = project.op_cursor
        writer.fail_source(expected_cursor=1, error="unreadable PDF")
        publication = writer.finalize(expected_cursor=2)
        require_action_sheet_readable(project, sheet)
        assert publication.receipt.status == "partial"
        assert project.op_cursor == cursor
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM ops WHERE kind='pdf.split'"
            ).fetchone()[0]
            == 1
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == before + 2
        )
        project.undo()  # unrelated later append
        project.undo()  # completed subset of split
        assert project.visible_row_ids(sheet) == rows
        project.redo()
        assert project.visible_row_ids(sheet) == [*children, *rows[1:]]
        info = json.loads(
            project.db.execute(
                "SELECT undo_info FROM ops WHERE id=?", (publication.op_id,)
            ).fetchone()[0]
        )
        assert info["replaced_rows_import_session_id"] == "split:one"
        assert "deleted_rows" not in info
        assert op_log.validate_operation_undo_info(info) is None
        assert {
            "kind": "import_session_rows",
            "session_id": "split:one",
            "membership": "replaced",
        } in op_log.operation_affected_refs(info)
    finally:
        project.close()


def test_source_failure_rolls_back_all_children_and_original(
    tmp_path: Path, monkeypatch
):
    project = Project.create(tmp_path / "rollback.frisket", name="rollback")
    try:
        sheet, column, rows, values = _seed(project)
        writer = _start(project, sheet, column, rows, values)
        original = project.add_rows

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("disk full")

        monkeypatch.setattr(project, "add_rows", fail)
        with pytest.raises(RuntimeError, match="disk full"):
            writer.publish_source(_children(2), expected_cursor=0)
        assert project.visible_row_ids(sheet) == rows
        assert writer.session.cursor == 0
        assert writer.session.committed_rows == 0
        monkeypatch.setattr(project, "add_rows", original)
        writer.publish_source(_children(2), expected_cursor=0)
        with pytest.raises(ImportSessionConflict):
            writer.publish_source(_children(2), expected_cursor=0)
        publication = writer.finalize(expected_cursor=1, stopped=True)
        assert publication.receipt.status == "partial"
    finally:
        project.close()


def test_new_sheet_keeps_sources_and_revalidates_file_cell(tmp_path: Path):
    project = Project.create(tmp_path / "stale.frisket", name="stale")
    try:
        sheet, column, rows, values = _seed(project)
        writer = _start(project, sheet, column, rows, values, replace=False)
        project.apply_edits(
            [{"row_id": rows[0], "column_id": column, "value": {"blob": "f" * 64}}]
        )
        with pytest.raises(ImportSessionConflict, match="source"):
            writer.publish_source(_children(2), expected_cursor=0)
        writer.fail_source(expected_cursor=0, error="source changed")
        children = writer.publish_source(_children(3), expected_cursor=1)
        publication = writer.finalize(expected_cursor=2)
        assert publication.receipt.status == "partial"
        assert project.visible_row_ids(sheet) == rows
        assert project.visible_row_ids(writer.sheet_id) == children
    finally:
        project.close()


def test_reservation_preserves_ties_hidden_rows_and_nested_undo(tmp_path: Path):
    project = Project.create(tmp_path / "order.frisket", name="order")
    try:
        sheet, column, rows, values = _seed(project)
        project.db.execute("UPDATE rows SET position=1 WHERE sheet_id=?", (sheet,))
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[2],))
        project.db.commit()
        writer = _start(project, sheet, column, rows, values)
        first = writer.publish_source(_children(2), expected_cursor=0)
        second = writer.publish_source(_children(3), expected_cursor=1)
        writer.finalize(expected_cursor=2)
        assert project.visible_row_ids(sheet) == [*first, *second, rows[3]]
        nested = PdfSplitWriter.start(
            project,
            session_id="split:nested",
            writer_authority="claim:nested",
            sheet_id=sheet,
            replace=True,
            columns=[{"name": "file", "type": "file"}],
            project_id="project",
            idempotency_key="nested",
            params_hash="nested",
            action_id="act:nested",
            receipt_id="receipt:nested",
            sources=[
                PdfSplitSource(
                    source_id="nested",
                    child_count=2,
                    sheet_id=sheet,
                    row_id=first[0],
                    column_id=column,
                    value={"blob": f"{10:064x}"},
                )
            ],
        )
        grandchildren = nested.publish_source(_children(2), expected_cursor=0)
        nested.finalize(expected_cursor=1)
        assert project.visible_row_ids(sheet) == [
            *grandchildren,
            first[1],
            *second,
            rows[3],
        ]
        project.undo()
        project.undo()
        assert project.visible_row_ids(sheet) == [rows[0], rows[1], rows[3]]
        project.redo()
        project.redo()
        assert project.visible_row_ids(sheet) == [
            *grandchildren,
            first[1],
            *second,
            rows[3],
        ]
    finally:
        project.close()


def test_split_membership_survives_bundle_export_and_schema_upgrade(tmp_path: Path):
    path = tmp_path / "durable.frisket"
    project = Project.create(path, name="durable")
    try:
        sheet, column, rows, values = _seed(project)
        # The exact preceding release had all the current data and no split
        # ledger; reopening must add it without replacing any authority.
        project.db.execute("DROP TABLE import_session_pdf_sources")
        project.db.execute(
            "UPDATE meta SET value=? WHERE key='schema_digest'",
            ("frisket.schema.v1:887874773c58ac58982913e6604d9ec9",),
        )
        project.db.commit()
    finally:
        project.close()
    project = Project(path)
    try:
        assert project.get_values(sheet, column) == dict(zip(rows, values))
        writer = _start(project, sheet, column, rows, values)
        children = writer.publish_source(_children(2), expected_cursor=0)
        writer.finalize(expected_cursor=1, stopped=True)
        archive = project.export(tmp_path / "partial.tar.gz", include_media=False)
    finally:
        project.close()
    restored = Project.import_bundle(archive, tmp_path / "restored.frisket")
    try:
        assert restored.visible_row_ids(sheet) == [*children, *rows[1:]]
        restored.undo()
        assert restored.visible_row_ids(sheet) == rows
        restored.redo()
        assert restored.visible_row_ids(sheet) == [*children, *rows[1:]]
    finally:
        restored.close()


@pytest.mark.parametrize("stopped,status", [(True, "cancelled"), (False, "failed")])
def test_zero_published_sources_never_report_completed(tmp_path: Path, stopped, status):
    project = Project.create(tmp_path / "zero.frisket", name="zero")
    try:
        sheet, column, rows, values = _seed(project)
        writer = _start(project, sheet, column, rows, values, replace=False)
        if not stopped:
            writer.fail_source(expected_cursor=0, error="corrupt source")
            writer.fail_source(expected_cursor=1, error="corrupt source")
        publication = writer.finalize(
            expected_cursor=0 if stopped else 2, stopped=stopped
        )
        assert publication.receipt.status == status
        assert publication.row_count == 0
        assert project.visible_row_ids(sheet) == rows
        assert (
            project.db.execute(
                "SELECT barrier FROM ops WHERE id=?", (publication.op_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_upload_append_keeps_existing_rows_and_has_no_nested_row_parent(tmp_path: Path):
    project = Project.create(tmp_path / "append.frisket", name="append")
    try:
        sheet, column, rows, values = _seed(project)
        writer = PdfSplitWriter.start(
            project,
            session_id="split:upload",
            writer_authority="claim:upload",
            sheet_id=sheet,
            columns=[{"name": "file", "type": "file"}],
            project_id="project",
            idempotency_key="upload",
            params_hash="upload",
            action_id="act:upload",
            receipt_id="receipt:upload",
            sources=[PdfSplitSource(source_id="upload:one", child_count=2)],
        )
        children = writer.publish_source(_children(2), expected_cursor=0)
        writer.finalize(expected_cursor=1)
        assert project.visible_row_ids(sheet) == [*rows, *children]
        assert all(
            row[0] is None
            for row in project.db.execute(
                "SELECT parent_row_id FROM rows WHERE id IN (?,?)",
                children,
            )
        )
        project.undo()
        assert project.visible_row_ids(sheet) == rows
        project.redo()
        assert project.visible_row_ids(sheet) == [*rows, *children]
    finally:
        project.close()


def test_source_checkpoint_resumes_with_new_writer_authority(tmp_path: Path):
    path = tmp_path / "resume.frisket"
    project = Project.create(path, name="resume")
    try:
        sheet, column, rows, values = _seed(project)
        stale = _start(project, sheet, column, rows, values)
        first = stale.publish_source(_children(2), expected_cursor=0)
        resumed = PdfSplitWriter.resume_session(
            project,
            session_id="split:one",
            writer_authority="claim:two",
            expected_cursor=1,
        )
        with pytest.raises(ImportSessionConflict):
            stale.publish_source(_children(3), expected_cursor=1)
        second = resumed.publish_source(_children(3), expected_cursor=1)
        resumed.finalize(expected_cursor=2)
        assert project.visible_row_ids(sheet) == [*first, *second, *rows[2:]]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM ops WHERE kind='pdf.split'"
            ).fetchone()[0]
            == 1
        )
    finally:
        project.close()


def test_stop_after_last_source_preserves_completed_outcome(tmp_path: Path):
    project = Project.create(tmp_path / "stop-race.frisket", name="stop-race")
    try:
        sheet, column, rows, values = _seed(project)
        writer = _start(project, sheet, column, rows, values)
        writer.publish_source(_children(2), expected_cursor=0)
        writer.publish_source(_children(3), expected_cursor=1)
        publication = writer.finalize(expected_cursor=2, stopped=True)
        assert publication.receipt.status == "completed"
        assert not any(
            "stopped" in warning.lower() for warning in publication.receipt.warnings
        )
        assert (
            project.db.execute(
                "SELECT state FROM import_sessions WHERE id='split:one'"
            ).fetchone()[0]
            == "completed"
        )
    finally:
        project.close()


def test_undo_redo_invalidates_bound_split_preparation_generation(tmp_path: Path):
    from frisket.engine.store.import_sessions import (
        import_session_generation_is_applied,
    )

    project = Project.create(tmp_path / "generation.frisket", name="generation")
    try:
        sheet, column, rows, values = _seed(project)
        writer = _start(project, sheet, column, rows, values)
        bound = writer.session

        def is_current():
            return import_session_generation_is_applied(
                project.db,
                session_id=bound.id,
                writer_authority=bound.writer_authority,
                op_id=bound.op_id,
            )

        assert not is_current()  # automatic preparation waits for publication
        children = writer.publish_source(_children(2), expected_cursor=0)
        writer.finalize(expected_cursor=1, stopped=True)
        assert is_current()
        # An aborted history transaction must not invalidate admitted work.
        project.db.execute("BEGIN IMMEDIATE")
        op_log.step_operation(project, "undo", expected_op_id=bound.op_id)
        assert not is_current()
        project.db.rollback()
        assert is_current()
        project.undo()
        assert not is_current()
        rotated = project.db.execute(
            "SELECT writer_authority FROM import_sessions WHERE id=?",
            (bound.id,),
        ).fetchone()[0]
        assert rotated != bound.writer_authority
        project.redo()
        assert project.visible_row_ids(sheet) == [*children, *rows[1:]]
        assert not is_current()  # restoring rows never revives the old worker
        assert (
            project.db.execute(
                "SELECT writer_authority FROM import_sessions WHERE id=?",
                (bound.id,),
            ).fetchone()[0]
            == rotated
        )
    finally:
        project.close()


def test_reservation_large_unrelated_sheet_is_metadata_only(tmp_path: Path, capsys):
    project = Project.create(tmp_path / "scale.frisket", name="scale")
    try:
        sheet, column, rows, values = _seed(project)
        project.db.execute(
            "WITH RECURSIVE n(x) AS (VALUES(5) UNION ALL SELECT x+1 FROM n WHERE x<100004) "
            "INSERT INTO rows(sheet_id,position) SELECT ?,x FROM n",
            (sheet,),
        )
        project.db.commit()
        cells_before = project.db.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
        started = time.perf_counter()
        writer = _start(project, sheet, column, rows, values)
        elapsed = time.perf_counter() - started
        assert (
            project.db.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
            == cells_before
        )
        assert (
            project.db.execute(
                "SELECT MAX(position) FROM rows WHERE sheet_id=?", (sheet,)
            ).fetchone()[0]
            == 100009
        )
        assert project.visible_row_ids(sheet)[:4] == rows
        writer.finalize(expected_cursor=0, stopped=True)
        with capsys.disabled():
            print(
                f"\nPDF split reservation: 100,004 rows / 2 sources in {elapsed:.3f}s"
            )
    finally:
        project.close()


@pytest.mark.parametrize("replace", [True, False])
def test_split_source_sheet_remains_deletable(tmp_path: Path, replace):
    project = Project.create(tmp_path / "delete.frisket", name="delete")
    try:
        sheet, column, rows, values = _seed(project)
        writer = _start(project, sheet, column, rows, values, replace=replace)
        writer.publish_source(_children(2), expected_cursor=0)
        writer.finalize(expected_cursor=1, stopped=True)
        project.delete_sheet(sheet)
        assert all(entry["id"] != sheet for entry in project.sheets())
    finally:
        project.close()


@pytest.mark.parametrize("replace", [True, False])
def test_stale_setup_never_adds_history_or_empty_sheet(tmp_path: Path, replace):
    project = Project.create(tmp_path / "setup.frisket", name="setup")
    try:
        sheet, column, rows, values = _seed(project)
        before_ops = project.db.execute(
            "SELECT COUNT(*) FROM ops WHERE status='applied'"
        ).fetchone()[0]
        stale = [*values]
        stale[0] = {"blob": "f" * 64}
        with pytest.raises(ImportSessionConflict):
            _start(project, sheet, column, rows, stale, replace=replace)
        assert [entry["id"] for entry in project.sheets()] == [sheet]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM ops WHERE status='applied'"
            ).fetchone()[0]
            == before_ops
        )
        assert project.visible_row_ids(sheet) == rows
    finally:
        project.close()


def test_setup_recheck_race_discards_only_split_and_preserves_newer_edit(
    tmp_path: Path, monkeypatch
):
    project = Project.create(tmp_path / "setup-race.frisket", name="setup-race")
    try:
        sheet, column, rows, values = _seed(project)
        reserve = PdfSplitWriter._reserve_sources
        later_cursor = None

        def edit_before_recheck(writer, sources, *, replace):
            nonlocal later_cursor
            project.apply_edits(
                [{"row_id": rows[0], "column_id": column, "value": {"blob": "f" * 64}}]
            )
            later_cursor = project.op_cursor
            return reserve(writer, sources, replace=replace)

        monkeypatch.setattr(PdfSplitWriter, "_reserve_sources", edit_before_recheck)
        with pytest.raises(ImportSessionConflict):
            _start(project, sheet, column, rows, values, replace=False)
        assert project.op_cursor == later_cursor
        assert [entry["id"] for entry in project.sheets()] == [sheet]
        assert project.get_values(sheet, column, [rows[0]])[rows[0]] == {
            "blob": "f" * 64
        }
        assert (
            project.db.execute(
                "SELECT status FROM ops WHERE kind='pdf.split'"
            ).fetchone()[0]
            == "discarded"
        )
        project.undo()
        assert project.get_values(sheet, column, [rows[0]])[rows[0]] == values[0]
    finally:
        project.close()


@pytest.mark.parametrize("corruption", ["missing_source", "not_replaced"])
def test_undo_refuses_incomplete_replacement_membership(tmp_path: Path, corruption):
    project = Project.create(tmp_path / "membership.frisket", name="membership")
    try:
        sheet, column, rows, values = _seed(project)
        writer = _start(project, sheet, column, rows, values)
        children = writer.publish_source(_children(2), expected_cursor=0)
        publication = writer.finalize(expected_cursor=1, stopped=True)
        if corruption == "missing_source":
            project.db.execute(
                "DELETE FROM import_session_pdf_sources WHERE session_id='split:one' AND ordinal=0"
            )
        else:
            project.db.execute(
                "UPDATE import_session_pdf_sources SET replaced=0 WHERE session_id='split:one' AND ordinal=0"
            )
        project.db.commit()
        with pytest.raises(ValueError, match="membership"):
            project.undo()
        assert project.visible_row_ids(sheet) == [*children, *rows[1:]]
        assert (
            project.db.execute(
                "SELECT status FROM ops WHERE id=?", (publication.op_id,)
            ).fetchone()[0]
            == "applied"
        )
    finally:
        project.close()


def test_split_refuses_ordinary_import_finalize_and_removal(tmp_path: Path):
    project = Project.create(tmp_path / "lifecycle.frisket", name="lifecycle")
    try:
        sheet, column, rows, values = _seed(project)
        writer = _start(project, sheet, column, rows, values)
        with pytest.raises(RuntimeError, match="split"):
            writer.finalize_session(expected_cursor=0)
        writer.cancel(expected_cursor=0)
        with pytest.raises(RuntimeError, match="split"):
            writer.remove_session(expected_cursor=0)
        publication = writer.finalize(expected_cursor=0, stopped=True)
        assert publication.receipt.status == "cancelled"
        assert not any(
            "completed sources were kept" in warning
            for warning in publication.receipt.warnings
        )
    finally:
        project.close()


def test_active_new_sheet_split_survives_source_sheet_deletion(tmp_path: Path):
    project = Project.create(tmp_path / "deleted-source.frisket", name="deleted-source")
    try:
        sheet, column, rows, values = _seed(project)
        writer = _start(project, sheet, column, rows, values, replace=False)
        destination = writer.sheet_id
        children = writer.publish_source(_children(2), expected_cursor=0)
        project.delete_sheet(sheet)
        with pytest.raises(ImportSessionConflict, match="source"):
            writer.publish_source(_children(3), expected_cursor=1)
        writer.fail_source(expected_cursor=1, error="The source sheet was deleted.")
        publication = writer.finalize(expected_cursor=2)
        assert publication.receipt.status == "partial"
        assert publication.row_count == 2
        assert project.visible_row_ids(destination) == children
        facts = project.db.execute(
            "SELECT sheet_id,row_id,column_id,state FROM import_session_pdf_sources WHERE session_id='split:one' ORDER BY ordinal"
        ).fetchall()
        assert [tuple(row) for row in facts] == [
            (sheet, rows[0], column, "published"),
            (sheet, rows[1], column, "failed"),
        ]
        # sheet.delete is a newer irreversible history barrier. Its existence
        # does not change the split primitive's durable saved-output behavior.
        operation = project.db.execute(
            "SELECT * FROM ops WHERE id=?", (publication.op_id,)
        ).fetchone()
        project.db.execute("BEGIN IMMEDIATE")
        project._unapply(operation)
        project.db.commit()
        assert destination not in [row["id"] for row in project.sheets()]
        project.db.execute("BEGIN IMMEDIATE")
        project._reapply(operation)
        project.db.commit()
        assert project.visible_row_ids(destination) == children
    finally:
        project.close()

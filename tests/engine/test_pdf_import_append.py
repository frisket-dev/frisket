from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import Receipt
from frisket.engine.executor.file_inventory_read import FileInventoryAdmission
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor.table_action import run_inventory_table_page
from frisket.engine.store import Project
from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.import_sessions import (
    ImportSessionConflict,
    ImportSessionStore,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from test_result_generation_store import (
    _declare,
    _release,
    _seal,
    _start_claimed_run,
    _write,
)


def _existing_sheet(project: Project) -> tuple[int, dict[str, int]]:
    sheet_id = project.add_sheet("Existing evidence")
    columns = {
        "filename": project.add_column(sheet_id, "filename"),
        "media": project.add_column(sheet_id, "media", type="file"),
        "size": project.add_column(sheet_id, "size", type="integer"),
        "text": project.add_column(sheet_id, "text"),
    }
    project.add_rows(
        sheet_id,
        [{"filename": "before.pdf", "size": 7, "text": "existing"}],
        columns,
    )
    return sheet_id, columns


def _start_append(project: Project, sheet_id: int) -> StreamingSheetWriter:
    return StreamingSheetWriter.start_append_session(
        project,
        session_id="pdf-commit:one",
        writer_authority="claim:one",
        sheet_id=sheet_id,
        columns=[
            {"name": "filename", "type": "text"},
            {"name": "media", "type": "file"},
            {"name": "size", "type": "integer"},
        ],
        project_id="pdf-project",
        action_kind="import.files",
        idempotency_key="pdf-commit:one",
        params_hash="sha256:pdf-commit-one",
        action_id="act:pdf-commit-one",
        receipt_id="receipt:pdf-commit-one",
        source_ref={"kind": "pdf_import_draft", "draft_id": "draft:one"},
    )


def _visible_names(project: Project, sheet_id: int, filename_column: int) -> list[str]:
    rows = project.db.execute(
        "SELECT r.id FROM rows r WHERE r.sheet_id=? AND r.hidden=0 ORDER BY r.position,r.id",
        (sheet_id,),
    ).fetchall()
    values, _refs = project.get_values_with_refs(
        sheet_id, filename_column, [int(row["id"]) for row in rows]
    )
    return [str(values[int(row["id"])]) for row in rows]


def test_append_session_resumes_once_and_undo_owns_only_imported_rows(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "append.frisket", name="append")
    try:
        sheet_id, columns = _existing_sheet(project)
        writer = _start_append(project, sheet_id)
        first_ids = writer.append_page(
            [{"filename": "one.pdf", "size": 11, "text": "one"}],
            expected_cursor=0,
            next_cursor=1,
        )
        writer.abort()

        resumed = StreamingSheetWriter.resume_session(
            project,
            session_id="pdf-commit:one",
            writer_authority="claim:two",
            expected_cursor=1,
        )
        with pytest.raises(ImportSessionConflict):
            resumed.append_page(
                [{"filename": "duplicate.pdf", "size": 11}],
                expected_cursor=0,
                next_cursor=1,
            )
        second_ids = resumed.append_page(
            [{"filename": "two.pdf", "size": 12, "text": "two"}],
            expected_cursor=1,
            next_cursor=2,
        )
        publication = resumed.finalize_session(expected_cursor=2)

        assert publication.sheet_id == sheet_id
        assert _visible_names(project, sheet_id, columns["filename"]) == [
            "before.pdf",
            "one.pdf",
            "two.pdf",
        ]
        op = project.db.execute(
            "SELECT undo_info FROM ops WHERE id=?", (publication.op_id,)
        ).fetchone()
        assert json.loads(op["undo_info"]) == {
            "created_rows": [*first_ids, *second_ids]
        }
        assert project.undo() == publication.op_id
        assert _visible_names(project, sheet_id, columns["filename"]) == ["before.pdf"]
    finally:
        project.close()


def test_cancelled_append_is_kept_then_removed_by_normal_undo(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "remove-append.frisket", name="append")
    try:
        sheet_id, columns = _existing_sheet(project)
        writer = _start_append(project, sheet_id)
        writer.append_page(
            [{"filename": "remove.pdf", "size": 13}],
            expected_cursor=0,
            next_cursor=1,
        )
        writer.cancel(expected_cursor=1)
        with pytest.raises(ImportSessionConflict, match="kept.*Undo"):
            writer.remove_session(expected_cursor=1)
        publication = writer.finalize_session(expected_cursor=1, keep_cancelled=True)

        assert _visible_names(project, sheet_id, columns["filename"]) == [
            "before.pdf",
            "remove.pdf",
        ]
        session = ImportSessionStore(project).get("pdf-commit:one")
        assert session is not None and session.state == "kept"
        assert project.undo() == publication.op_id
        assert _visible_names(project, sheet_id, columns["filename"]) == ["before.pdf"]
    finally:
        project.close()


def test_append_adds_missing_import_columns_without_touching_existing_data(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "append-columns.frisket", name="append")
    try:
        sheet_id = project.add_sheet("Notes")
        notes = project.add_column(sheet_id, "notes")
        old_row = project.add_rows(sheet_id, [{"notes": "keep"}], {"notes": notes})[0]
        writer = _start_append(project, sheet_id)
        writer.append_page(
            [{"filename": "new.pdf", "size": 8}],
            expected_cursor=0,
            next_cursor=1,
        )
        publication = writer.finalize_session(expected_cursor=1)

        columns = {
            str(row["name"]): int(row["id"]) for row in project.columns(sheet_id)
        }
        assert {"notes", "filename", "media", "size"} <= set(columns)
        assert project.get_values(sheet_id, notes)[old_row] == "keep"
        assert project.undo() == publication.op_id
        assert project.get_values(sheet_id, notes)[old_row] == "keep"
        assert {str(row["name"]) for row in project.columns(sheet_id)} >= {
            "notes",
            "filename",
            "media",
            "size",
        }
    finally:
        project.close()


def test_unrelated_write_during_append_keeps_normal_history_order(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "interleaved.frisket", name="append")
    try:
        sheet_id, columns = _existing_sheet(project)
        writer = _start_append(project, sheet_id)
        writer.append_page(
            [{"filename": "imported.pdf", "size": 13}],
            expected_cursor=0,
            next_cursor=1,
        )
        unrelated_id = project.add_rows(
            sheet_id,
            [{"filename": "user-added.pdf", "size": 2}],
            columns,
        )[0]
        unrelated_op = project.op_cursor
        publication = writer.finalize_session(expected_cursor=1)

        assert project.undo() == unrelated_op
        assert _visible_names(project, sheet_id, columns["filename"]) == [
            "before.pdf",
            "imported.pdf",
        ]
        assert project.undo() == publication.op_id
        assert _visible_names(project, sheet_id, columns["filename"]) == ["before.pdf"]
        assert (
            project.db.execute(
                "SELECT hidden FROM rows WHERE id=?", (unrelated_id,)
            ).fetchone()["hidden"]
            == 1
        )
    finally:
        project.close()


def test_import_undo_after_derived_text_result_preserves_existing_rows(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "derived-result.frisket", name="append")
    try:
        sheet_id, columns = _existing_sheet(project)
        writer = _start_append(project, sheet_id)
        [imported_row] = writer.append_page(
            [{"filename": "generated.pdf", "size": 13}],
            expected_cursor=0,
            next_cursor=1,
        )
        publication = writer.finalize_session(expected_cursor=1)

        derived = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=columns["text"],
            row_ids=[imported_row],
            label="PDF native text",
            replace_existing=True,
        )
        generations = ResultGenerationStore(project)
        _declare(
            generations,
            derived,
            columns["text"],
            write_mode="replace_scope",
        )
        _write(
            project,
            derived,
            [
                {
                    "row_id": imported_row,
                    "column_id": columns["text"],
                    "value": "generated document text",
                    "publication_effect": "publish_value",
                }
            ],
        )
        _seal(generations, derived, columns["text"])
        _release(project, derived)

        assert project.get_values(sheet_id, columns["text"])[imported_row] == (
            "generated document text"
        )
        assert project.undo() == derived.op_id
        assert project.undo() == publication.op_id
        assert _visible_names(project, sheet_id, columns["filename"]) == ["before.pdf"]
        existing_row = project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        assert project.get_values(sheet_id, columns["text"])[
            int(existing_row["id"])
        ] == "existing"
    finally:
        project.close()


def test_unreadable_import_row_uses_standard_run_error_and_null_text(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "unreadable-result.frisket", name="append")
    try:
        sheet_id, columns = _existing_sheet(project)
        writer = _start_append(project, sheet_id)
        [imported_row] = writer.append_page(
            [{"filename": "broken.pdf", "size": 13}],
            expected_cursor=0,
            next_cursor=1,
        )
        writer.finalize_session(expected_cursor=1)
        derived = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=columns["text"],
            row_ids=[imported_row],
            label="PDF unreadable text",
            replace_existing=True,
        )
        generations = ResultGenerationStore(project)
        _declare(
            generations,
            derived,
            columns["text"],
            write_mode="replace_scope",
        )
        _write(
            project,
            derived,
            [
                {
                    "row_id": imported_row,
                    "column_id": columns["text"],
                    "value": None,
                    "error": "PDF xref could not be read",
                    "error_code": "row_error",
                    "outcome": "row_error",
                    "publication_effect": "publish_error",
                }
            ],
        )
        _seal(generations, derived, columns["text"])
        _release(project, derived)

        assert project.get_values(sheet_id, columns["text"]).get(imported_row) is None
        assert RunResultStore(project).row_error_summary(derived.run_id) == {
            "total_failed_rows": 1,
            "groups": [
                {
                    "message": "PDF xref could not be read",
                    "count": 1,
                    "code": "row_error",
                    "outcome": "row_error",
                    "terminal": False,
                    "row_ids": [imported_row],
                }
            ],
        }
    finally:
        project.close()


def test_existing_import_files_page_adapter_uses_append_writer(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "adapter.frisket", name="append")
    inventory = ImportInventory(tmp_path / "commit-inventory.db")
    try:
        sheet_id, columns = _existing_sheet(project)
        digest = project.blob_store.put(b"pdf bytes")
        inventory.append(
            [
                {
                    "logical_path": "new.pdf",
                    "mime": "application/pdf",
                    "sha256": digest,
                    "size": 9,
                    "kind": "files",
                }
            ]
        )
        inventory.seal()
        bound = typed_action_for_request(
            {
                "action_id": "import.files",
                "scope": {"kind": "project"},
                "sheet_name": "ignored-for-existing",
                "params": {"inventory_ref": "pdf-commit"},
                "idempotency_key": "pdf-adapter",
            }
        )
        ReceiptStore(project).insert_running(
            Receipt(
                receipt_id="receipt:pdf-adapter",
                project_id="pdf-project",
                action_id="act:pdf-adapter",
                action_kind="import.files",
                idempotency_key="pdf-adapter",
                params_hash=typed_request_hash(bound),
                status="running",
            )
        )

        session = run_inventory_table_page(
            project,
            "pdf-project",
            bound,
            admission=FileInventoryAdmission(
                "pdf-commit", inventory, project.blob_store, after=0, limit=2
            ),
            session_id="pdf-adapter",
            writer_authority="claim:adapter",
            reserved_action_id="act:pdf-adapter",
            reserved_receipt_id="receipt:pdf-adapter",
            destination_sheet_id=sheet_id,
        )

        assert session.state == "completed"
        assert session.sheet_id == sheet_id
        assert _visible_names(project, sheet_id, columns["filename"]) == [
            "before.pdf",
            "new.pdf",
        ]
        assert project.undo() == session.op_id
        assert _visible_names(project, sheet_id, columns["filename"]) == ["before.pdf"]
    finally:
        inventory.close()
        project.close()

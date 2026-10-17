from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.document_text import (
    DocumentTextRoleError,
    DocumentTextUnavailable,
    document_text_role,
    ensure_document_text_role,
    resolve_document_text_selection,
)
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import PreparedContentStore, PreparedPageDraft
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.value_codec import PreparedContentRef
from helpers import run_writer_authority_fixture


_BLOB = "sha256:" + "1" * 64
_OTHER_BLOB = "sha256:" + "2" * 64


def _project_with_selection(
    tmp_path: Path, *, source_type: str = "file", page: int | None = None
) -> tuple[Project, int, int, int, int]:
    project = Project.create(tmp_path / "document-text.frisket")
    sheet_id = project.add_sheet("Documents")
    source_column_id = project.add_column(sheet_id, "File", type=source_type)
    text_column_id = project.add_column(sheet_id, "Text", type="text")
    source_value: dict[str, object] = {
        "blob": _BLOB,
        "mime": "application/pdf" if source_type == "file" else "image/png",
    }
    if page is not None:
        source_value["page"] = page
    row_id = project.add_rows(
        sheet_id, [{"File": source_value}], {"File": source_column_id}
    )[0]

    project.db.execute("BEGIN IMMEDIATE")
    ensure_document_text_role(
        project,
        source_column_id=source_column_id,
        text_column_id=text_column_id,
    )
    op_id = project.append_op("document.prepare", commit=False)
    artifact = record_source_artifact(
        project,
        artifact_kind="document",
        media_type=str(source_value["mime"]),
        blob_hash=_BLOB,
        page_count=max(1, page or 1),
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=source_column_id,
    )
    store = PreparedContentStore(project)
    ref = store.stage_reference(
        source_artifact_id=int(artifact["id"]),
        producing_op_id=op_id,
        pages=[
            PreparedPageDraft(number, f"page {number}")
            for number in range(1, max(1, page or 1) + 1)
        ],
        page_number=page,
    )
    insert_edits(
        project.db,
        op_id=op_id,
        edits=[EditCellWrite(row_id, text_column_id, ref)],
    )
    project.db.commit()
    return project, sheet_id, row_id, source_column_id, text_column_id


@pytest.mark.parametrize("source_type", ["file", "image"])
def test_resolves_exact_fresh_prepared_selection(
    tmp_path: Path, source_type: str
) -> None:
    project, sheet_id, row_id, source_column_id, text_column_id = (
        _project_with_selection(tmp_path, source_type=source_type)
    )
    try:
        selection = resolve_document_text_selection(
            project, (sheet_id, row_id, source_column_id)
        )
        assert selection is not None
        assert selection.text_cell == (sheet_id, row_id, text_column_id)
        assert selection.prepared_ref_id == selection.provenance_ref_id
        assert selection.source_blob_hash == _BLOB
        assert selection.source_page_number is None
    finally:
        project.close()


def test_flat_manual_edit_inherits_pin_and_source_change_makes_it_stale(
    tmp_path: Path,
) -> None:
    project, sheet_id, row_id, source_column_id, text_column_id = (
        _project_with_selection(tmp_path)
    )
    try:
        original = resolve_document_text_selection(
            project, (sheet_id, row_id, source_column_id)
        )
        assert original is not None

        project.db.execute("BEGIN IMMEDIATE")
        edit_op_id = project.append_op("edit", commit=False)
        insert_edits(
            project.db,
            op_id=edit_op_id,
            edits=[EditCellWrite(row_id, text_column_id, "human correction")],
        )
        project.db.commit()
        corrected = resolve_document_text_selection(
            project, (sheet_id, row_id, source_column_id)
        )
        assert corrected is not None
        assert corrected.prepared_ref_id is None
        assert corrected.provenance_ref_id == original.provenance_ref_id

        project.db.execute("BEGIN IMMEDIATE")
        source_edit_op_id = project.append_op("edit", commit=False)
        insert_edits(
            project.db,
            op_id=source_edit_op_id,
            edits=[
                EditCellWrite(
                    row_id,
                    source_column_id,
                    {"blob": _OTHER_BLOB, "mime": "application/pdf"},
                )
            ],
        )
        project.db.commit()
        with pytest.raises(DocumentTextUnavailable) as exc:
            resolve_document_text_selection(
                project, (sheet_id, row_id, source_column_id)
            )
        assert exc.value.reason == "selection_stale_or_missing"
    finally:
        project.close()


def test_role_lookup_precedes_source_validation_and_hidden_never_falls_back(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "roles.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        source_column_id = project.add_column(sheet_id, "File", type="file")
        text_column_id = project.add_column(sheet_id, "Text", type="text")
        row_id = project.add_rows(sheet_id, [{}], {})[0]
        assert (
            resolve_document_text_selection(
                project, (sheet_id, row_id, source_column_id)
            )
            is None
        )

        project.db.execute("BEGIN IMMEDIATE")
        role = ensure_document_text_role(
            project,
            source_column_id=source_column_id,
            text_column_id=text_column_id,
        )
        project.db.commit()
        assert document_text_role(project, source_column_id=source_column_id) == role
        with pytest.raises(DocumentTextUnavailable):
            resolve_document_text_selection(
                project, (sheet_id, row_id, source_column_id)
            )

        project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (text_column_id,))
        project.db.commit()
        with pytest.raises(DocumentTextUnavailable) as exc:
            resolve_document_text_selection(
                project, (sheet_id, row_id, source_column_id)
            )
        assert exc.value.reason == "text_column_hidden"
    finally:
        project.close()


def test_role_rejects_cross_sheet_and_conflicting_pairs(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "conflicts.frisket")
    try:
        first = project.add_sheet("First")
        second = project.add_sheet("Second")
        source = project.add_column(first, "File", type="file")
        text = project.add_column(first, "Text")
        other_text = project.add_column(second, "Other Text")
        project.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(DocumentTextRoleError, match="share a sheet"):
            ensure_document_text_role(
                project, source_column_id=source, text_column_id=other_text
            )
        ensure_document_text_role(
            project, source_column_id=source, text_column_id=text
        )
        assert (
            ensure_document_text_role(
                project, source_column_id=source, text_column_id=text
            ).text_column_id
            == text
        )
        project.db.rollback()
    finally:
        project.close()


def test_generation_snapshots_role_and_failed_replacement_preserves_success(
    tmp_path: Path,
) -> None:
    project, sheet_id, row_id, source_column_id, text_column_id = (
        _project_with_selection(tmp_path)
    )
    try:
        # Remove the setup edit so generated heads are the selected authority.
        project.db.execute("DELETE FROM edits WHERE column_id=?", (text_column_id,))
        project.db.execute(
            "DELETE FROM document_text_manual_pins WHERE text_column_id=?",
            (text_column_id,),
        )
        project.db.commit()
        ref_id = int(
            project.db.execute("SELECT id FROM prepared_content_refs").fetchone()[0]
        )

        def start(label: str):
            op_id = project.append_op("media.ocr", {"output": "Text"}, label=label)
            run_id = RunResultStore(project).start_run(
                op_id,
                sheet_id,
                "media.ocr",
                row_ids=[row_id],
                total_rows=1,
            )
            authority = run_writer_authority_fixture(
                project, run_id, output_column_ids={text_column_id}
            )
            return op_id, run_id, authority

        generations = ResultGenerationStore(project)
        first_op, first_run, first_authority = start("first")
        assert first_authority.claim_token is not None
        first_binding = generations.declare(
            first_run,
            text_column_id,
            output_role="text",
            compatibility_key="sha256:document-text-v1",
            write_mode="create",
            claim_token=first_authority.claim_token,
        )
        assert first_binding.document_text_authority is True
        RunResultStore(project).write_results(
            first_run,
            [
                {
                    "row_id": row_id,
                    "column_id": text_column_id,
                    "value": PreparedContentRef(ref_id),
                    "publication_effect": "publish_value",
                }
            ],
            **first_authority.kwargs(),
        )
        generations.seal(
            first_run,
            [text_column_id],
            claim_token=first_authority.claim_token,
            terminal_disposition="completed",
        )
        RunResultStore(project).point_column_at_run(
            first_op, text_column_id, first_run
        )
        OutputColumnClaimStore(project).release(
            claim_token=first_authority.claim_token
        )

        _second_op, second_run, second_authority = start("failed replacement")
        assert second_authority.claim_token is not None
        generations.declare(
            second_run,
            text_column_id,
            output_role="text",
            compatibility_key="sha256:document-text-v1",
            write_mode="replace_scope",
            claim_token=second_authority.claim_token,
        )
        RunResultStore(project).write_results(
            second_run,
            [
                {
                    "row_id": row_id,
                    "column_id": text_column_id,
                    "error": "OCR failed",
                    "error_code": "model_error",
                    "outcome": "model_error",
                    "publication_effect": "publish_error",
                }
            ],
            **second_authority.kwargs(),
        )
        generations.seal(
            second_run,
            [text_column_id],
            claim_token=second_authority.claim_token,
            terminal_disposition="failed",
        )
        head = project.db.execute(
            "SELECT run_id FROM cell_result_heads WHERE row_id=? AND column_id=?",
            (row_id, text_column_id),
        ).fetchone()
        assert head is not None and int(head[0]) == first_run

        # Role metadata can change only by raw/migration maintenance today;
        # projection repair still follows each generation's immutable policy.
        project.db.execute(
            "DELETE FROM document_text_column_roles WHERE source_column_id=?",
            (source_column_id,),
        )
        project.db.commit()
        generations.rebuild_heads([text_column_id])
        rebuilt = project.db.execute(
            "SELECT run_id FROM cell_result_heads WHERE row_id=? AND column_id=?",
            (row_id, text_column_id),
        ).fetchone()
        assert rebuilt is not None and int(rebuilt[0]) == first_run
    finally:
        project.close()

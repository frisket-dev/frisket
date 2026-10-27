"""Merge finished original pages into split-owned child Text base cells."""

from __future__ import annotations

from typing import Any, Protocol

from .cell_writes import BaseCellWrite, replace_base_cells
from .document_text import document_text_role
from .import_sessions import ImportSessionStore, import_session_generation_is_applied
from .prepared_content import PreparedContentStore, PreparedPageDraft
from .value_codec import decode_stored_value


class PdfSplitTextBinding(Protocol):
    operation_id: int
    generation: str
    session_id: str
    row_id: int
    artifact_id: int
    source_column_id: int
    text_column_id: int
    page_number: int
    expected_source_value: Any
    expected_text_value: Any


def _identity(db: Any, row_id: int, column_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT value.value_kind,value.value,head.retrieval_revision,head.origin_kind,"
        "head.base_producer_id FROM current_cells head LEFT JOIN cells value "
        "ON value.row_id=head.row_id AND value.column_id=head.column_id "
        "AND value.producer_id IS head.base_producer_id "
        "WHERE head.row_id=? AND head.column_id=?",
        (row_id, column_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "value_kind": row[0],
        "stored_value": row[1],
        "revision": row[2],
        "origin_kind": row[3],
        "producer_id": row[4],
    }


def capture_pdf_split_text_identities(
    project: Any,
    *,
    row_id: int,
    source_column_id: int,
    text_column_id: int,
    original_artifact_id: int,
    original_page_number: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Snapshot compact runtime identities, including equal-value user edits."""
    source = _identity(project.db, row_id, source_column_id)
    text = _identity(project.db, row_id, text_column_id)
    if source is None or text is None:
        raise ValueError("split child source and Text cells must exist")
    source.update(
        original_artifact_id=original_artifact_id,
        original_page_number=original_page_number,
    )
    return source, text


def publish_pdf_split_page(
    project: Any, binding: PdfSplitTextBinding, prepared_ref_id: int
) -> bool:
    """Publish only to a current child, merging against its latest immutable set.

    This post-publication writer never appends an operation or modifies history
    cursors. Undo/Redo saves the split producer's base values through row history.
    """
    db = project.db
    db.execute("BEGIN IMMEDIATE")
    try:
        if not import_session_generation_is_applied(
            db,
            session_id=binding.session_id,
            writer_authority=binding.generation,
            op_id=binding.operation_id,
        ):
            db.rollback()
            return False
        session = ImportSessionStore(project).get(binding.session_id)
        child = db.execute(
            "SELECT row.sheet_id,artifact.blob_hash,artifact.page_count FROM rows row "
            "JOIN sheets sheet ON sheet.id=row.sheet_id "
            "JOIN import_session_rows member ON member.row_id=row.id AND member.session_id=? "
            "JOIN source_artifacts artifact ON artifact.id=? "
            "AND artifact.source_row_id=row.id AND artifact.source_sheet_id=row.sheet_id "
            "AND artifact.source_column_id=? WHERE row.id=? AND row.hidden=0 AND sheet.hidden=0",
            (
                binding.session_id,
                binding.artifact_id,
                binding.source_column_id,
                binding.row_id,
            ),
        ).fetchone()
        role = document_text_role(db, source_column_id=binding.source_column_id)
        if (
            session is None
            or child is None
            or role is None
            or role.text_column_id != binding.text_column_id
            or not role.source_active
            or role.source_hidden
            or not role.text_active
            or role.text_hidden
            or not 1 <= binding.page_number <= int(child[2])
        ):
            db.rollback()
            return False
        source = _identity(db, binding.row_id, binding.source_column_id)
        text = _identity(db, binding.row_id, binding.text_column_id)
        expected = dict(binding.expected_source_value)
        original_artifact_id = expected.pop("original_artifact_id")
        original_page = expected.pop("original_page_number")
        # Text refresh also touches the File/Text pair's retrieval revision.
        # Compare the actual source authority/value, and separately retain the
        # historical edit barrier so Undo of a newer edit cannot revive work.
        if source is not None:
            source = {key: value for key, value in source.items() if key != "revision"}
        expected.pop("revision", None)
        edited = db.execute(
            "SELECT 1 FROM edits WHERE row_id=? AND column_id IN (?,?) AND op_id>? LIMIT 1",
            (
                binding.row_id,
                binding.source_column_id,
                binding.text_column_id,
                binding.operation_id,
            ),
        ).fetchone()
        if (
            source != expected
            or edited is not None
            or text is None
            or source["origin_kind"] != "source_cell"
            or text["origin_kind"] != "source_cell"
            or source["producer_id"] != session.producer_id
            or text["producer_id"] != session.producer_id
        ):
            db.rollback()
            return False
        source_value = decode_stored_value(source["value_kind"], source["stored_value"])
        if not isinstance(source_value, dict) or str(
            source_value.get("blob", "")
        ).removeprefix("sha256:") != str(child[1]).removeprefix("sha256:"):
            db.rollback()
            return False
        store = PreparedContentStore(project)
        original = store.resolve(prepared_ref_id)
        if len(original.pins) != 1 or original.pins[0].page_number != original_page:
            raise ValueError("finished source page does not match the child binding")
        artifacts = db.execute(
            "SELECT id,blob_hash FROM source_artifacts WHERE id IN (?,?)",
            (original_artifact_id, original.source_artifact_id),
        ).fetchall()
        hashes = {int(row[0]): str(row[1]).removeprefix("sha256:") for row in artifacts}
        if hashes.get(original_artifact_id) is None or hashes.get(
            original_artifact_id
        ) != hashes.get(original.source_artifact_id):
            raise ValueError("finished page belongs to another original PDF")
        pin = original.pins[0]
        positions = dict(pin.positions or {})
        positions.update(
            source_artifact_id=original_artifact_id,
            source_page_number=original_page,
            page_number=binding.page_number,
        )
        draft = PreparedPageDraft(binding.page_number, original.text, positions)
        if text["value_kind"] == "prepared_content_ref":
            current_ref = int(text["stored_value"])
            owner = db.execute(
                "SELECT content.source_artifact_id,content.producing_op_id,ref.page_number "
                "FROM prepared_content_refs ref JOIN prepared_content_sets content ON content.id=ref.set_id WHERE ref.id=?",
                (current_ref,),
            ).fetchone()
            if owner is None or tuple(owner) != (
                binding.artifact_id,
                binding.operation_id,
                None,
            ):
                db.rollback()
                return False
            ref = store.stage_replacement(
                base_ref_id=current_ref,
                producing_op_id=binding.operation_id,
                replacements=[draft],
                allow_partial=True,
            )
        else:
            if text != dict(binding.expected_text_value):
                db.rollback()
                return False
            ref = store.stage_reference(
                source_artifact_id=binding.artifact_id,
                producing_op_id=binding.operation_id,
                pages=[draft],
                allow_partial=True,
            )
        replace_base_cells(
            db,
            producer_id=int(session.producer_id),
            column_ids=[binding.text_column_id],
            row_ids=[binding.row_id],
            cells=[BaseCellWrite(binding.row_id, binding.text_column_id, ref)],
        )
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise

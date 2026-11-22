"""Publish finished split-page Text through separate preparation Changes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .cell_writes import BaseCellWrite, create_base_cell_producer, replace_base_cells
from .changes import close_change
from .document_text import document_text_role
from .import_sessions import ImportSessionStore, import_session_generation_is_applied
from .prepared_content import PreparedContentStore, PreparedPageDraft
from .value_codec import decode_stored_value


class PdfSplitTextBinding(Protocol):
    @property
    def operation_id(self) -> int: ...
    @property
    def generation(self) -> str: ...
    @property
    def session_id(self) -> str: ...
    @property
    def row_id(self) -> int: ...
    @property
    def artifact_id(self) -> int: ...
    @property
    def source_column_id(self) -> int: ...
    @property
    def text_column_id(self) -> int: ...
    @property
    def page_number(self) -> int: ...
    @property
    def expected_source_value(self) -> Any: ...
    @property
    def expected_text_value(self) -> Any: ...


def _identity(db: Any, row_id: int, column_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT value.value_kind,value.value,head.retrieval_revision,head.origin_kind,"
        "head.base_producer_id FROM effective_current_cells head LEFT JOIN cells value "
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


def _operation_is_text_owner(db: Any, change_id: int, parent_change_id: int) -> bool:
    return (
        change_id == parent_change_id
        or db.execute(
            "SELECT 1 FROM changes WHERE id=? AND kind='pdf.prepare_text' "
            "AND json_extract(spec,'$.import_change_id')=?",
            (change_id, parent_change_id),
        ).fetchone()
        is not None
    )


def _text_owner_matches(db: Any, producer_id: int, parent_change_id: int) -> bool:
    owner = db.execute(
        "SELECT change_id FROM base_cell_producers WHERE id=?", (producer_id,)
    ).fetchone()
    return owner is not None and _operation_is_text_owner(
        db, owner[0], parent_change_id
    )


def _start_preparation_change(
    project: Any, binding: PdfSplitTextBinding, prepared_ref_id: int
) -> tuple[int, int]:
    change_id = project.append_change_in_transaction(
        "pdf.prepare_text",
        {
            "import_change_id": binding.operation_id,
            "import_session_id": binding.session_id,
            "prepared_ref_id": prepared_ref_id,
        },
        label="Prepare PDF text",
    )
    producer = create_base_cell_producer(
        project.db, stage_id=f"op:{change_id}", change_id=change_id
    )
    return change_id, producer


def publish_pdf_split_pages(
    project: Any, bindings: Sequence[PdfSplitTextBinding], prepared_ref_id: int
) -> list[bool]:
    """Publish a bounded fanout under its own undoable preparation Change."""
    if len(bindings) > 256:
        raise ValueError("PDF preparation publication requires a bounded page")
    if bindings and any(
        binding.operation_id != bindings[0].operation_id for binding in bindings
    ):
        raise ValueError("PDF preparation batch has different parent imports")
    with project.transaction_scope():
        change: list[int] = []
        applied = [
            _publish_pdf_split_page(project, binding, prepared_ref_id, change)
            for binding in bindings
        ]
        if change:
            close_change(project.db, change[0])
        return applied


def publish_pdf_split_page(
    project: Any, binding: PdfSplitTextBinding, prepared_ref_id: int
) -> bool:
    return publish_pdf_split_pages(project, [binding], prepared_ref_id)[0]


def _publish_pdf_split_page(
    project: Any, binding: PdfSplitTextBinding, prepared_ref_id: int, change: list[int]
) -> bool:
    db = project.db
    if not import_session_generation_is_applied(
        db,
        session_id=binding.session_id,
        writer_authority=binding.generation,
        change_id=binding.operation_id,
    ):
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
        "SELECT 1 FROM edits WHERE row_id=? AND column_id IN (?,?) AND change_id>? LIMIT 1",
        (
            binding.row_id,
            binding.source_column_id,
            binding.text_column_id,
            binding.operation_id,
        ),
    ).fetchone()
    if (
        source is None
        or text is None
        or source != expected
        or edited is not None
        or source["origin_kind"] != "source_cell"
        or text["origin_kind"] != "source_cell"
        or source["producer_id"] != session.producer_id
        or not _text_owner_matches(db, text["producer_id"], binding.operation_id)
    ):
        return False
    source_value = decode_stored_value(source["value_kind"], source["stored_value"])
    if not isinstance(source_value, dict) or str(
        source_value.get("blob", "")
    ).removeprefix("sha256:") != str(child[1]).removeprefix("sha256:"):
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
    if (
        original.source_artifact_id is None
        or hashes.get(original_artifact_id) is None
        or hashes.get(original_artifact_id) != hashes.get(original.source_artifact_id)
    ):
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
            "SELECT content.source_artifact_id,content.producing_change_id,ref.page_number "
            "FROM prepared_content_refs ref JOIN prepared_content_sets content ON content.id=ref.set_id WHERE ref.id=?",
            (current_ref,),
        ).fetchone()
        if (
            owner is None
            or owner[0] != binding.artifact_id
            or owner[2] is not None
            or not _operation_is_text_owner(db, owner[1], binding.operation_id)
        ):
            return False
        current = store.resolve(current_ref)
        if any(
            pin.page_number == binding.page_number
            and current.text[pin.start : pin.end] == draft.text
            and pin.positions == draft.positions
            for pin in current.pins
        ):
            return True  # Recovery after commit, before the inventory acknowledgement.
        if not change:
            change.extend(_start_preparation_change(project, binding, prepared_ref_id))
        ref = store.stage_replacement(
            base_ref_id=current_ref,
            producing_change_id=change[0],
            replacements=[draft],
            allow_partial=True,
        )
    else:
        if text != dict(binding.expected_text_value):
            return False
        if not change:
            change.extend(_start_preparation_change(project, binding, prepared_ref_id))
        ref = store.stage_reference(
            source_artifact_id=binding.artifact_id,
            producing_change_id=change[0],
            pages=[draft],
            allow_partial=True,
        )
    replace_base_cells(
        db,
        producer_id=change[1],
        column_ids=[binding.text_column_id],
        row_ids=[binding.row_id],
        cells=[BaseCellWrite(binding.row_id, binding.text_column_id, ref)],
    )
    return True

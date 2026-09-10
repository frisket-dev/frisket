"""Concrete staged import publication inside the table writer's transaction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.project_blobs import add_blob_from_path


@dataclass(frozen=True)
class ImportBlob:
    occurrence_id: int
    path: Path
    digest: str
    size: int
    filename: str
    mime: str
    metadata: dict[str, Any]
    role: Literal["attachment", "document", "page"]
    source_url: str | None = None
    provider: str | None = None
    document_id: int | None = None
    page: int | None = None


@dataclass(frozen=True)
class ImportBlobCell:
    occurrence_id: int
    row_id: int
    column_name: str


@dataclass(frozen=True)
class ImportBlobPlan:
    blobs: tuple[ImportBlob, ...] = ()
    cells: tuple[ImportBlobCell, ...] = ()

    def bind_row_ordinals(self, row_ids: Sequence[int]) -> ImportBlobPlan:
        """Bind explicitly host-generated zero-based ordinals, never digest order."""
        cells = []
        for cell in self.cells:
            if type(cell.row_id) is not int or not 0 <= cell.row_id < len(row_ids):
                raise ValueError("invalid staged import row ordinal")
            cells.append(replace(cell, row_id=row_ids[cell.row_id]))
        return replace(self, cells=tuple(cells))


def publish_import_blobs(
    project: Any,
    plan: ImportBlobPlan,
    *,
    sheet_id: int,
    column_ids: Mapping[str, int],
    op_id: int,
    receipt_id: str,
) -> tuple[dict[str, Any], ...]:
    """Promote bytes and bind evidence without committing the caller's transaction.

    Rollback removes metadata, not canonical objects: those bytes may already
    belong to another cell. Only invocation-owned scratch paths are removed.
    """
    if not project.db.in_transaction:
        raise ValueError("import blobs require a publication transaction")
    records = {blob.occurrence_id: blob for blob in plan.blobs}
    if len(records) != len(plan.blobs):
        raise ValueError("duplicate staged import identity")
    positions: dict[int, int] = {}
    for cell in plan.cells:
        if cell.occurrence_id not in records or cell.column_name not in column_ids:
            raise ValueError("invalid staged import cell association")
        row = project.db.execute(
            "SELECT position FROM rows WHERE id=? AND sheet_id=?",
            (cell.row_id, sheet_id),
        ).fetchone()
        if type(cell.row_id) is not int or row is None:
            raise ValueError("staged import row is not in the materialized sheet")
        positions[cell.row_id] = row["position"]
    for blob in plan.blobs:
        if blob.role == "page":
            document = records.get(blob.document_id)
            if (
                document is None
                or document.role != "document"
                or type(blob.page) is not int
                or blob.page < 1
            ):
                raise ValueError("invalid staged PDF page association")
        add_blob_from_path(
            project,
            blob.path,
            filename=blob.filename,
            mime=blob.mime,
            metadata=blob.metadata,
            source_url=blob.source_url,
            commit=False,
            expected_digest=blob.digest,
        )

    artifacts: dict[int, dict[str, Any]] = {}
    # Documents are roots even if rendering failed and no cell contains them.
    for blob in plan.blobs:
        if blob.role == "document":
            artifacts[blob.occurrence_id] = record_source_artifact(
                project,
                artifact_kind="file",
                media_type=blob.mime,
                blob_hash=blob.digest,
                filename=blob.filename,
                source_sheet_id=sheet_id,
            )

    refs: list[dict[str, Any]] = []
    for blob in plan.blobs:
        if blob.role == "document":
            refs.append(
                {
                    "kind": "imported_pdf_document",
                    "hash": blob.digest,
                    "filename": blob.filename,
                    "mime": blob.mime,
                    "size": blob.size,
                    "sheet_id": sheet_id,
                    "op_id": op_id,
                    "artifact_id": artifacts[blob.occurrence_id]["id"],
                }
            )
    for cell in plan.cells:
        blob = records[cell.occurrence_id]
        column_id = column_ids[cell.column_name]
        ref = {
            "kind": "imported_pdf_page_image"
            if blob.role == "page"
            else "imported_blob",
            "hash": blob.digest,
            "filename": blob.filename,
            "mime": blob.mime,
            "size": blob.size,
            "sheet_id": sheet_id,
            "row_id": cell.row_id,
            "row_index": positions[cell.row_id],
            "column_id": column_id,
            "op_id": op_id,
        }
        if blob.source_url is not None:
            ref.update(source_url=blob.source_url, provider=blob.provider)
        if blob.role == "page":
            artifact = artifacts[blob.document_id]
            span = record_source_span(
                project,
                artifact_id=artifact["id"],
                span_kind="page",
                page_start=blob.page,
                page_end=blob.page,
                preview={"blob_hash": blob.digest, "mime": blob.mime},
            )
            link = record_evidence_link(
                project,
                subject_kind="cell_value",
                subject_ref={
                    "kind": "source_cell",
                    "sheet_id": sheet_id,
                    "row_id": cell.row_id,
                    "column_id": column_id,
                },
                spans=[{"span_id": span["id"]}],
                sheet_id=sheet_id,
                row_id=cell.row_id,
                column_id=column_id,
                op_id=op_id,
                receipt_id=receipt_id,
                link_role="primary_support",
            )
            ref.update(
                page=blob.page, artifact_id=artifact["id"], evidence_link_id=link["id"]
            )
        refs.append(ref)
    # This cleanup remains abortable. Outer stager cleanup handles leftovers
    # after an earlier failure; canonical blob paths are never part of this plan.
    for blob in plan.blobs:
        blob.path.unlink(missing_ok=True)
    return tuple(refs)

"""Concrete staged import publication inside the table writer's transaction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.blob_backend import BlobIntegrityError, ProjectBlobStore
from frisket.engine.store.project_blobs import publish_prepared_blob


@dataclass(frozen=True)
class ImportBlob:
    occurrence_id: int
    path: Path | None
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
    # Host-admitted inventory may already own canonical bytes. Never deserialize
    # this authority from action params or infer it from a digest supplied there.
    owner: ProjectBlobStore | None = field(default=None, repr=False, compare=False)
    occurrence_ref: dict[str, Any] | None = None


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


@dataclass(frozen=True)
class PreparedImportBlobPlan:
    """Invocation-owned occurrences whose bytes are durable in the blob backend."""

    plan: ImportBlobPlan

    def bind_row_ordinals(self, row_ids: Sequence[int]) -> PreparedImportBlobPlan:
        return replace(self, plan=self.plan.bind_row_ordinals(row_ids))


def _blob_records(plan: ImportBlobPlan) -> dict[int, ImportBlob]:
    records = {blob.occurrence_id: blob for blob in plan.blobs}
    if len(records) != len(plan.blobs):
        raise ValueError("duplicate staged import identity")
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
    return records


def prepare_import_blobs(project: Any, plan: ImportBlobPlan) -> PreparedImportBlobPlan:
    """Verify and durably store staged bytes without holding a database lock.

    The backend remains the integrity authority.  Canonical bytes may outlive a
    failed publication; until metadata and evidence commit they are harmless
    unreferenced objects, matching the existing rollback contract. Future
    byte-deleting GC must fence the interval from this preparation through
    publication so it cannot remove an object while its reference is in flight.
    """

    if project.db.in_transaction:
        raise ValueError("import blob preparation requires no active transaction")
    _blob_records(plan)
    project._assert_blob_write_open()
    for blob in plan.blobs:
        if blob.owner is not None:
            if blob.owner is not project.blob_store or blob.path is not None:
                raise ValueError("owned import blob belongs to another blob store")
            continue
        if blob.path is None:
            raise ValueError("import blob has neither staged bytes nor an owner")
        digest = project.blob_store.put_path(
            blob.path,
            expected_digest=blob.digest,
        )
        if digest != blob.digest:
            raise BlobIntegrityError(
                "blob store returned a digest that does not match the prepared file"
            )
    return PreparedImportBlobPlan(plan)


def publish_import_blobs(
    project: Any,
    prepared: PreparedImportBlobPlan,
    *,
    sheet_id: int,
    column_ids: Mapping[str, int],
    op_id: int,
    receipt_id: str,
) -> tuple[dict[str, Any], ...]:
    """Bind prepared bytes and evidence without committing the caller transaction.

    Rollback removes metadata, not canonical objects: those bytes may already
    belong to another cell.
    """
    if not project.db.in_transaction:
        raise ValueError("import blobs require a publication transaction")
    if not isinstance(prepared, PreparedImportBlobPlan):
        raise TypeError("import blobs must be prepared before publication")
    plan = prepared.plan
    records = _blob_records(plan)
    if any(
        blob.owner is not None and blob.owner is not project.blob_store
        for blob in plan.blobs
    ):
        raise ValueError("owned import blob belongs to another blob store")
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
        publish_prepared_blob(
            project,
            digest=blob.digest,
            size=blob.size,
            filename=blob.filename,
            mime=blob.mime,
            source_url=blob.source_url,
            metadata=blob.metadata,
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
        if blob.occurrence_ref is not None:
            # Paged imports keep occurrence provenance in project data, not an
            # ever-growing receipt or an expiring upload inventory.
            artifact = record_source_artifact(
                project,
                artifact_kind="file",
                media_type=blob.mime,
                blob_hash=blob.digest,
                filename=blob.filename,
                source_sheet_id=sheet_id,
                source_row_id=cell.row_id,
                source_column_id=column_id,
                external_ref=blob.occurrence_ref,
            )
            ref["artifact_id"] = artifact["id"]
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
    return tuple(refs)

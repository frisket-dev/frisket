"""Freeze an ordered, visible sheet scope for PDF packet review."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from frisket.actions.types import TableError
from frisket.engine.store.blob_backend import BlobStoreError

from frisket.contracts.http.pdf_packet_splits import (
    PdfPacketSheetCreateRequest,
    PdfPacketSheetScopeRequest,
)
from frisket.engine.executor.document_extraction_read import document_cell
from frisket.engine.store.document_text import (
    DocumentTextUnavailable,
    resolve_document_text_selection,
)
from frisket.querysets import execute_sheet_row_page_without_total, sheet_row_scope_plan
from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
from frisket.server.workspace import Workspace


@dataclass(frozen=True)
class PacketScopeInventory:
    sources: tuple[AdmittedPacketSource, ...]
    excluded: tuple[dict[str, Any], ...]
    excluded_count: int
    preview: tuple[dict[str, Any], ...]
    order_hash: str


class PdfPacketScopeService:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    @staticmethod
    def _ordered_scope(
        snapshot: Any, body: PdfPacketSheetCreateRequest | PdfPacketSheetScopeRequest
    ) -> list[int]:
        if body.ranked_row_ids is not None:
            visible = set(snapshot.visible_row_ids(body.sheet_id, body.ranked_row_ids))
            ordered = [row for row in body.ranked_row_ids if row in visible]
        else:
            plan = sheet_row_scope_plan(
                snapshot,
                body.sheet_id,
                parent_row_id=body.parent_row_id,
                filter_=body.filter_ if body.scope != "all" else None,
                sort=body.sort,
                row_ids=body.scope_row_ids,
            )
            ordered = []
            while True:
                page = execute_sheet_row_page_without_total(
                    snapshot, plan, limit=500, offset=len(ordered)
                )
                ordered.extend(int(row["id"]) for row in page)
                if len(page) < 500:
                    break
        return ordered

    def order_token(
        self, project_id: str, body: PdfPacketSheetScopeRequest
    ) -> dict[str, str]:
        project = self._workspace.get(project_id)
        with project.read_snapshot() as snapshot:
            if (
                snapshot.db.execute(
                    "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (body.sheet_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("sheet is unavailable")
            ordered = self._ordered_scope(snapshot, body)
            digest = hashlib.sha256(
                json.dumps(ordered, separators=(",", ":")).encode()
            ).hexdigest()
        return {"schema_version": "frisket.pdf_packet_split.v1", "order_hash": digest}

    def admit(
        self, project_id: str, body: PdfPacketSheetCreateRequest
    ) -> PacketScopeInventory:
        project = self._workspace.get(project_id)
        with project.read_snapshot() as snapshot:
            sheet = snapshot.db.execute(
                "SELECT id FROM sheets WHERE id=? AND hidden=0", (body.sheet_id,)
            ).fetchone()
            column = snapshot.db.execute(
                "SELECT id FROM columns WHERE id=? AND sheet_id=? AND type='file' AND active=1 AND hidden=0",
                (body.file_column_id, body.sheet_id),
            ).fetchone()
            if sheet is None or column is None:
                raise ValueError("sheet or file column is unavailable")
            ordered = self._ordered_scope(snapshot, body)
            order_hash = hashlib.sha256(
                json.dumps(ordered, separators=(",", ":")).encode()
            ).hexdigest()
            if (
                body.expected_order_hash is not None
                and body.expected_order_hash != order_hash
            ):
                raise ValueError("sheet selection changed; refresh and select again")
            for anchor in body.cached_anchors:
                if (
                    anchor.index >= len(ordered)
                    or ordered[anchor.index] != anchor.row_id
                ):
                    raise ValueError(
                        "sheet selection changed; refresh and select again"
                    )
            if body.scope == "selected":
                if body.selected_row_ids:
                    selected = set(body.selected_row_ids)
                    if not selected <= set(ordered):
                        raise ValueError(
                            "sheet selection changed; refresh and select again"
                        )
                    ordered = [row for row in ordered if row in selected]
                else:
                    indices = set()
                    for span in body.selected_index_ranges or ():
                        if span.end >= len(ordered):
                            raise ValueError(
                                "sheet selection changed; refresh and select again"
                            )
                        indices.update(range(span.start, span.end + 1))
                    ordered = [
                        row for index, row in enumerate(ordered) if index in indices
                    ]
            values = snapshot.get_values(
                body.sheet_id, body.file_column_id, row_ids=ordered
            )
            sources = []
            excluded = []
            excluded_count = 0
            valid_preview = []
            excluded_preview = []
            counts: dict[str, int] = {}
            for row_id in ordered:
                value = values.get(row_id)
                reason = None
                try:
                    cell = document_cell(
                        snapshot,
                        sheet_id=body.sheet_id,
                        column_id=body.file_column_id,
                        row_id=row_id,
                    )
                    blob = snapshot.db.execute(
                        "SELECT hash, filename, mime, size, source_url FROM blobs WHERE hash=?",
                        (cell.blob_id,),
                    ).fetchone()
                    if blob is None or blob["mime"] != "application/pdf":
                        reason = "not_pdf"
                    else:
                        if cell.blob_id not in counts:
                            with project.blob_store.materialize(cell.blob_id) as path:
                                counts[cell.blob_id] = len(
                                    PdfReader(path, strict=False).pages
                                )
                        count = counts[cell.blob_id]
                        if count < 1 or (cell.page is not None and cell.page > count):
                            reason = "invalid_page"
                except (TableError, BlobStoreError, PdfReadError, OSError, ValueError):
                    reason = "not_pdf"
                if reason:
                    excluded_count += 1
                    if len(excluded) < 9:
                        excluded.append({"row_id": row_id, "reason": reason})
                    if len(excluded_preview) < 9:
                        excluded_preview.append(
                            {"row_id": row_id, "filename": None, "reason": reason}
                        )
                    continue
                prepared_ref = None
                prepared_pages: tuple[int, ...] = ()
                try:
                    selection = resolve_document_text_selection(
                        snapshot, (body.sheet_id, row_id, body.file_column_id)
                    )
                    if selection is not None and selection.prepared_ref_id is not None:
                        prepared_ref = selection.prepared_ref_id
                        prepared_pages = tuple(
                            int(r["page_number"])
                            for r in snapshot.db.execute(
                                "SELECT page_number FROM prepared_content_set_pages WHERE set_id=? AND page_number BETWEEN ? AND ? ORDER BY page_number",
                                (selection.set_id, cell.page or 1, cell.page or count),
                            )
                        )
                except DocumentTextUnavailable:
                    pass
                filename = str(
                    value.get("filename") or blob["filename"] or "document.pdf"
                )
                source = AdmittedPacketSource(
                    key=f"sheet:{body.sheet_id}:row:{row_id}:column:{body.file_column_id}",
                    sha256=cell.blob_id,
                    filename=filename,
                    size=int(blob["size"]),
                    page_count=count,
                    page_start=cell.page or 1,
                    page_end=cell.page or count,
                    open_source=lambda digest=cell.blob_id: (
                        project.blob_store.materialize(digest)
                    ),
                    source_url=blob["source_url"],
                    source_sheet_id=body.sheet_id,
                    source_row_id=row_id,
                    source_column_id=body.file_column_id,
                    frozen_value=value,
                    prepared_ref_id=prepared_ref,
                    prepared_pages=prepared_pages,
                )
                sources.append(source)
                if len(valid_preview) < 9:
                    valid_preview.append(
                        {"row_id": row_id, "filename": filename, "reason": None}
                    )
        return PacketScopeInventory(
            tuple(sources),
            tuple(excluded),
            excluded_count,
            tuple((valid_preview + excluded_preview)[:9]),
            order_hash,
        )

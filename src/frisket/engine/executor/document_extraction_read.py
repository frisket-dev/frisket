"""Admitted document geometry, extraction, and ordinary table evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from frisket.actions.document_extract import extraction_fields
from frisket.actions.document_extraction_types import (
    Box,
    DocumentExtraction,
    PositionedDocument,
    PositionedPage,
    PositionedToken,
)
from frisket.actions.types import (
    DynamicOutput,
    SheetRows,
    TableError,
    TableResult,
    TableRow,
)
from frisket.contracts.action import ReceiptEvidence
from frisket.engine.executor.sheet_rows_read import AdmittedSheetRowsReader
from frisket.engine.sandbox.media_sync import run_media_sync
from frisket.engine.store.blob_backend import BlobStoreError, validate_blob_digest
from frisket.engine.store.ocr_word_stream import iter_ocr_word_streams
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class LoadedDocument:
    blob_id: str
    filename: str
    mime: str
    document: PositionedDocument
    artifact_id: int | None = None


@dataclass(frozen=True)
class DocumentIdentity:
    blob_id: str
    filename: str
    mime: str
    fingerprint: str
    page_count: int
    artifact_id: int | None

    @classmethod
    def from_loaded(cls, loaded):
        return cls(
            loaded.blob_id,
            loaded.filename,
            loaded.mime,
            loaded.document.source_fingerprint,
            len(loaded.document.pages),
            loaded.artifact_id,
        )


def _ocr_pages(project, blob_id):
    """Keep actual OCR geometry, including line/block granularity and blank pages."""
    for resolved in iter_ocr_word_streams(project, blob_id):
        tokens = {}
        for token in resolved.stream.tokens:
            if not token.text or token.page is None or token.page < 1:
                continue
            try:
                region = Box(
                    **dict(zip(("x0", "y0", "x1", "y1"), token.box, strict=True))
                )
            except (KeyError, ValueError, TypeError):
                continue
            granularity = "word" if resolved.engine == "tesseract" else "block"
            if resolved.engine in {"rapidocr", "pp-ocrv6", "paddleocr", "datalab"}:
                granularity = "line"
            tokens.setdefault(token.page, []).append(
                PositionedToken(text=token.text, box=region, granularity=granularity)
            )
        if not tokens:
            continue
        pages = []
        try:
            for number, image in sorted(
                resolved.page_images.items(), key=lambda pair: int(pair[0])
            ):
                if not isinstance(image, dict):
                    raise ValueError("Invalid page geometry")
                width = image.get("source_width") or image.get("width")
                height = image.get("source_height") or image.get("height")
                pages.append(
                    PositionedPage(
                        page=int(number),
                        width=width,
                        height=height,
                        tokens=tokens.get(int(number), []),
                    )
                )
        except (ValueError, TypeError):
            continue
        if (
            pages
            and any(page.tokens for page in pages)
            and set(tokens) <= {page.page for page in pages}
        ):
            return pages, resolved.artifact_id
    return [], None


def load_positioned_document(
    project, blob_id: str, *, cancelled=None
) -> LoadedDocument:
    if cancelled is not None and cancelled():
        raise TableError("action_cancelled", "Document reading was cancelled")
    try:
        validate_blob_digest(blob_id)
    except ValueError as exc:
        raise TableError("invalid_input_ref", "Document blob is invalid") from exc
    blob = project.db.execute(
        "SELECT filename,mime FROM blobs WHERE hash=?", (blob_id,)
    ).fetchone()
    if blob is None:
        raise TableError("invalid_input_ref", "Document is not present in this project")
    filename = str(blob["filename"] or "Document")
    mime = str(blob["mime"] or "application/octet-stream")
    pages, artifact_id = _ocr_pages(project, blob_id)
    if not pages and (mime == "application/pdf" or filename.lower().endswith(".pdf")):
        from frisket.engine.pdf_text import (
            extract_pdf_text,
            PdfTextError,
            PdfTextCancelled,
        )

        try:
            with project.materialize_blob(blob_id) as path:
                pages = run_media_sync(
                    lambda should_cancel: extract_pdf_text(
                        Path(path), should_cancel=should_cancel
                    ),
                    cancelled=cancelled,
                )
        except PdfTextCancelled as exc:
            raise TableError(
                "action_cancelled", "Document reading was cancelled"
            ) from exc
        except PdfTextError as exc:
            raise TableError("document_geometry_unavailable", str(exc)) from exc
        except (BlobStoreError, OSError) as exc:
            raise TableError(
                "document_geometry_unavailable", "Document bytes could not be read"
            ) from exc
    if not pages or not any(page.tokens for page in pages):
        raise TableError(
            "document_geometry_unavailable",
            "This document needs positioned text. Run OCR with a geometry-bearing engine first.",
        )
    fingerprint = _hash(
        {
            "blob": blob_id,
            "artifact": artifact_id,
            "pages": [page.model_dump(mode="json") for page in pages],
        }
    )
    return LoadedDocument(
        blob_id,
        filename,
        mime,
        PositionedDocument(source_fingerprint=fingerprint, pages=pages),
        artifact_id,
    )


def document_cell(project, *, sheet_id, column_id, row_id):
    column = project.db.execute(
        "SELECT name,type FROM columns WHERE id=? AND sheet_id=? AND active=1",
        (column_id, sheet_id),
    ).fetchone()
    if (
        column is None
        or column["type"] not in {"file", "image"}
        or row_id not in project.visible_row_ids(sheet_id, [row_id])
    ):
        raise TableError("invalid_input_ref", "Choose a visible document cell")
    value = project.get_values(sheet_id, column_id, row_ids=[row_id]).get(row_id)
    if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
        raise TableError("invalid_input_ref", "The selected cell has no document")
    return value["blob"]


class AdmittedPositionedDocumentReader:
    def __init__(self, project, *, scope, params, row_limit=None, cancelled=None):
        self.project, self.params = project, params
        selection = params.extraction_scope
        if params.layout_id is not None or selection is not None:
            from frisket.engine.store.extraction_layouts import (
                resolve_document_scope,
                validate_layout_scope,
            )

            try:
                with project.read_snapshot() as snapshot:
                    if params.layout_id is not None:
                        validate_layout_scope(
                            snapshot,
                            params.layout_id,
                            scope.sheet_id,
                            params.source.name,
                        )
                    if selection is not None:
                        selection = selection.model_copy(
                            update={"layout_id": params.layout_id}
                        )
                        if scope.row_ids is not None:
                            raise ValueError("Use one document scope selection")
                        row_ids = resolve_document_scope(
                            snapshot,
                            sheet_id=scope.sheet_id,
                            source=params.source.name,
                            scope=selection,
                        )
                        if not row_ids:
                            raise TableError(
                                "invalid_input_ref",
                                "The selected document scope is empty",
                            )
                        scope = SheetRows(
                            sheet_id=scope.sheet_id, row_ids=tuple(row_ids)
                        )
            except ValueError as exc:
                raise TableError("invalid_input_ref", str(exc)) from exc
        self.scope = scope
        self.rows = AdmittedSheetRowsReader(project, scope=scope, params=params)
        self.parent_sheet_id = scope.sheet_id
        self.sources = self.rows.sources
        self.facts = self.rows.facts
        if selection is not None:
            self.facts.append(
                {
                    "kind": "document_extraction_scope",
                    "sheet_id": scope.sheet_id,
                    "source": params.source.name,
                    "layout_id": params.layout_id,
                    "selection": selection.model_dump(mode="json"),
                    "row_ids": list(scope.row_ids),
                }
            )
        self.cancelled = cancelled or (lambda: False)
        self.document_limit = 12 if row_limit is not None else None
        self.occurrences = []
        self.documents = []
        self.warnings = []
        self.outcome_counts = {
            "extracted": 0,
            "zero_records": 0,
            "alignment_failed": 0,
            "error": 0,
        }
        self.successful_row_ids = []
        self.unresolved_fields = 0
        self.field_warnings = 0
        self.identities = {}
        self.source_blobs = {}
        self.reference = None
        self.final_names = {}

    def close(self):
        self.rows.close()

    def _load(self, blob_id):
        if self.reference is not None and blob_id == self.reference.blob_id:
            return self.reference
        return load_positioned_document(self.project, blob_id, cancelled=self.cancelled)

    def document_results(self, params):
        from frisket.engine.document_extraction import (
            compile_template,
            extract_document,
        )

        reference = self._load(params.template.reference_blob_id)
        self.reference = reference
        self.identities[reference.blob_id] = DocumentIdentity.from_loaded(reference)
        try:
            compiled = compile_template(params.template, reference.document)
        except ValueError as exc:
            raise TableError("invalid_params", str(exc)) from exc
        fields = extraction_fields(params)
        if not fields:
            raise TableError("invalid_params", "Select at least one field")
        inputs = self.rows.read()
        if self.document_limit is not None:
            inputs = islice(inputs, self.document_limit)
        for item in inputs:
            if self.cancelled():
                raise TableError(
                    "action_cancelled", "Document extraction was cancelled"
                )
            value = params.source.read(item.row)
            blob_id = value.get("blob") if isinstance(value, dict) else None
            try:
                if not isinstance(blob_id, str):
                    raise TableError("invalid_input_ref", "Source cell has no document")
                loaded = self._load(blob_id)
                result = extract_document(
                    compiled, loaded.document, params.repeat_group_id
                )
                loaded = DocumentIdentity.from_loaded(loaded)
                self.identities[blob_id] = loaded
                self.source_blobs[item.source.row_id] = blob_id
            except TableError as exc:
                if exc.code == "action_cancelled":
                    raise
                loaded = None
                result = DocumentExtraction(
                    records=[],
                    diagnostics=[str(exc)],
                    outcome="error",
                    error_code=exc.code,
                )
            if result.outcome in {"extracted", "zero_records"}:
                self.successful_row_ids.append(item.source.row_id)
            outcome = {
                "row_id": item.source.row_id,
                "blob_id": blob_id or "",
                "filename": loaded.filename if loaded else "Document",
                "result": result,
            }
            # The editor consumes this immediately. Full runs retain only the
            # output occurrences, not a duplicate corpus of positioned text.
            self.documents[:] = [outcome]
            self.outcome_counts[result.outcome] += 1
            unresolved = 0
            field_warning_count = 0
            field_diagnostics = []
            field_names = {field.id: field.name for field in fields}
            for record_index, record in enumerate(result.records, 1):
                for field_id, cell in record.cells.items():
                    if cell.status != "not_found" and not cell.diagnostic:
                        continue
                    unresolved += cell.status == "not_found"
                    field_warning_count += 1
                    if len(field_diagnostics) < 8:
                        field_diagnostics.append(
                            f"record {record_index}, {field_names[field_id]}: "
                            f"{cell.diagnostic or 'field could not be located'}"
                        )
            self.unresolved_fields += unresolved
            self.field_warnings += field_warning_count
            if field_warning_count > len(field_diagnostics):
                field_diagnostics.append(
                    f"{field_warning_count - len(field_diagnostics)} additional field warnings"
                )
            self.facts.append(
                {
                    "kind": "document_extraction_input",
                    "row_id": item.source.row_id,
                    "blob": blob_id,
                    "fingerprint": loaded.fingerprint if loaded else None,
                    "artifact_id": loaded.artifact_id if loaded else None,
                    "outcome": result.outcome,
                    "error_code": result.error_code,
                    "record_count": len(result.records),
                    "unresolved_fields": unresolved,
                    "field_warnings": field_warning_count,
                }
            )
            if (
                result.outcome != "extracted"
                or result.diagnostics
                or field_warning_count
            ) and len(self.warnings) < 99:
                diagnostics = [*result.diagnostics, *field_diagnostics]
                self.warnings.append(
                    f"Document row {item.source.row_id}: {result.outcome}. {'; '.join(diagnostics)}"
                )
            yield item.source, loaded, result

    def read(self, params):
        def rows():
            fields = extraction_fields(params)
            try:
                for source, loaded, result in self.document_results(params):
                    for record in result.records:
                        self.occurrences.append((source, loaded, record))
                        values = {
                            field.name: record.cells[field.id].text for field in fields
                        }
                        yield TableRow(
                            output=DynamicOutput(values),
                            sources=(source,),
                            parent=source,
                        )
            finally:
                if (
                    self.outcome_counts["zero_records"]
                    or self.outcome_counts["alignment_failed"]
                    or self.outcome_counts["error"]
                    or self.unresolved_fields
                    or self.field_warnings
                ):
                    self.warnings.append(
                        "Document extraction: "
                        + ", ".join(
                            f"{count} {outcome.replace('_', ' ')}"
                            for outcome, count in self.outcome_counts.items()
                        )
                        + f", {self.unresolved_fields} unresolved fields"
                        + f", {self.field_warnings} field warnings"
                    )

        return TableResult(rows=rows(), warnings=self.warnings)

    def lower_row(self, values, fields, sources, parent, output_names):
        self.final_names = {
            field.key: output_names.get(field.key, field.key) for field in fields
        }
        return values

    def revalidate(self):
        if self.params.layout_id is not None:
            from frisket.engine.store.extraction_layouts import validate_layout_scope

            try:
                validate_layout_scope(
                    self.project,
                    self.params.layout_id,
                    self.scope.sheet_id,
                    self.params.source.name,
                )
            except ValueError as exc:
                raise TableError(
                    "stale_input", "The selected layout changed during extraction"
                ) from exc
        # Blob bytes are content-addressed; OCR artifact identity must still be the
        # one selected for this run. Never silently publish against a later OCR.
        for blob_id, loaded in self.identities.items():
            if loaded.artifact_id is not None:
                # Use the same admissibility predicate as the original read:
                # a newer unusable artifact does not make an older one stale.
                _pages, artifact_id = _ocr_pages(self.project, blob_id)
                if artifact_id != loaded.artifact_id:
                    raise TableError(
                        "stale_input",
                        "Document text changed during extraction; preview again",
                    )
        column_id = self.rows._column_ids[self.params.source.name]
        ids = list(self.source_blobs)
        for offset in range(0, len(ids), 500):
            values = self.project.get_values(
                self.scope.sheet_id, column_id, row_ids=ids[offset : offset + 500]
            )
            for row_id in ids[offset : offset + 500]:
                value = values.get(row_id)
                if (
                    not isinstance(value, dict)
                    or value.get("blob") != self.source_blobs[row_id]
                ):
                    raise TableError(
                        "stale_input",
                        "Source document changed during extraction; preview again",
                    )

    def initial_cell_values(self, row, ordinal):
        return row

    def generated_columns(self):
        return set()

    def publication_evidence(self, write, rows, *, receipt_id):
        evidence = []
        artifacts = {}
        fields = extraction_fields(self.params)
        for (source, loaded, record), output_row_id in zip(
            self.occurrences, write.row_ids, strict=True
        ):
            if loaded.blob_id not in artifacts:
                artifacts[loaded.blob_id] = record_source_artifact(
                    self.project,
                    artifact_kind="file",
                    media_type=loaded.mime,
                    blob_hash=loaded.blob_id,
                    filename=loaded.filename,
                    source_sheet_id=source.sheet_id,
                    source_row_id=source.row_id,
                    page_count=loaded.page_count,
                    metadata={
                        "positioned_source_fingerprint": loaded.fingerprint,
                        "positioned_source_artifact_id": loaded.artifact_id,
                    },
                )
            artifact = artifacts[loaded.blob_id]
            for field in fields:
                cell = record.cells[field.id]
                if not cell.regions:
                    continue
                column_id = write.column_ids[
                    self.final_names.get(field.name, field.name)
                ]
                spans = []
                for region in cell.regions:
                    span = record_source_span(
                        self.project,
                        artifact_id=artifact["id"],
                        span_kind="region",
                        page_start=region.page,
                        page_end=region.page,
                        bbox=[{**region.box.model_dump(), "space": "page_normalized"}],
                        quote=(cell.text or None) if len(cell.regions) == 1 else None,
                        metadata={"field_id": field.id, "status": cell.status},
                    )
                    spans.append({"span_id": span["id"], "rank": len(spans)})
                _values, refs = self.project.get_values_with_refs(
                    write.sheet_id, column_id, row_ids=[output_row_id]
                )
                link = record_evidence_link(
                    self.project,
                    subject_kind="cell",
                    subject_ref=refs[output_row_id],
                    spans=spans,
                    sheet_id=write.sheet_id,
                    row_id=output_row_id,
                    column_id=column_id,
                    op_id=write.op_id,
                    receipt_id=receipt_id,
                    link_role="document_extraction",
                    producer={"action_kind": "media.extract_document"},
                )
                evidence.append(
                    ReceiptEvidence(
                        ref={
                            "kind": "evidence_link",
                            "id": link["id"],
                            "stable_id": link["stable_id"],
                        }
                    )
                )
        if self.params.layout_id is not None and self.successful_row_ids:
            from frisket.engine.store.extraction_layouts import remember_success

            remember_success(
                self.project,
                self.params.layout_id,
                self.successful_row_ids,
                commit=False,
            )
        return evidence

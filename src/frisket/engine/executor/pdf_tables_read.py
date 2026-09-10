"""Row-owned PDF acquisition, result association, and durable read evidence."""

from __future__ import annotations

import asyncio
import copy
import mimetypes
from pathlib import Path

from frisket.actions.pdf_table_types import PdfTableOptions, PdfTableRows
from frisket.actions.types import ColumnRef, DynamicOutput, Outcome, RowError
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.execution.provider import ExecutionLimitExceeded, enforce_pdf_page_limit
from frisket.ops.integrations import natural_pdf
from frisket.ops.pdf_tables import _pdf_table_extract_options, _pdf_table_records
from frisket.sdk.media import media_text_hash


def pdf_source_ref(project, *, sheet_id, row_id, column_id, name, value):
    if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
        raise RowError("invalid_pdf_cell", "PDF source must be a blob-backed cell.")
    digest = value["blob"]
    blob = MediaBlobStore(project).blob_row(digest)
    if blob is None:
        raise RowError("invalid_pdf_cell", "PDF source blob is missing.")
    filename = str(value.get("filename") or blob["filename"] or f"{digest}.pdf")
    mime = str(
        value.get("mime")
        or blob["mime"]
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    if mime != "application/pdf" and not filename.lower().endswith(".pdf"):
        raise RowError("invalid_pdf_cell", "Source blob is not a PDF.")
    source_url = str(blob["source_url"] or "")
    return {
        "kind": "media_extract_pdf_tables_blob_input",
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": column_id,
        "source_column": name,
        "blob_hash": digest,
        "filename": filename,
        "mime": mime,
        "size": blob["size"],
        "source_url_hash": media_text_hash(source_url) if source_url else None,
    }


class AdmittedPdfTablesReader:
    def __init__(self, project, *, cancelled=None, execution_limits=None):
        self._project = project
        self._cancelled = cancelled
        self._limits = execution_limits
        self._closed = False
        self._reads = set()

    def _check_open(self):
        if self._closed:
            raise RuntimeError("PDF tables reader is closed")
        if self._cancelled is not None and self._cancelled():
            raise asyncio.CancelledError

    def bind_row(self, row, *, sheet_id, row_id, sources):
        self._check_open()
        if type(row_id) is not int or type(sheet_id) is not int:
            raise RowError("invalid_input_ref", "PDF reader requires its admitted row.")
        return _BoundPdfTablesReader(
            self, row, sheet_id, row_id, copy.deepcopy(dict(sources or {}))
        )

    async def aclose(self):
        self._closed = True
        for task in tuple(self._reads):
            await _settle(task)


class _BoundPdfTablesReader:
    def __init__(self, owner, row, sheet_id, row_id, sources):
        self._owner = owner
        self._row = row
        self._sheet_id = sheet_id
        self._row_id = row_id
        self._sources = sources
        self._results = []

    async def read(
        self,
        row,
        source,
        *,
        mode="extract_table",
        table_mode="auto",
        extract_table=None,
    ):
        self._owner._check_open()
        if row is not self._row or not isinstance(source, ColumnRef):
            raise RowError(
                "invalid_input_ref", "PDF reader requires its admitted source."
            )
        captured = self._sources.get(source.name)
        if (
            not isinstance(captured, dict)
            or source.name not in row.values
            or canonical_json_hash(source.read(row))
            != canonical_json_hash(captured["value"])
        ):
            raise RowError("stale_input", "PDF source differs from its admitted cell.")
        try:
            options = PdfTableOptions(
                mode=mode,
                table_mode=table_mode,
                extract_table={} if extract_table is None else extract_table,
            )
        except ValueError:
            raise RowError(
                "invalid_params", "Invalid PDF extraction options."
            ) from None
        ref = pdf_source_ref(
            self._owner._project,
            sheet_id=self._sheet_id,
            row_id=self._row_id,
            column_id=captured["column_id"],
            name=source.name,
            value=captured["value"],
        )

        def extract():
            project = self._owner._project
            with project.materialize_blob(ref["blob_hash"]) as path:
                with open(path, "rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        raise RowError("invalid_pdf_cell", "Source has no PDF header.")
                metadata = MediaBlobStore(project).probe_metadata(ref["blob_hash"])
                pages = metadata.get("pages")
                if type(pages) is not int or pages <= 0:
                    pages = None
                if self._owner._limits is not None:
                    enforce_pdf_page_limit(self._owner._limits.max_pdf_pages, pages)
                tables = natural_pdf.extract_pdf_tables(
                    natural_pdf.PdfTableExtractRequest(
                        path=Path(path),
                        filename=ref["filename"],
                        source_row_id=self._row_id,
                        source_blob_hash=ref["blob_hash"],
                        mode=options.mode,
                        options=_pdf_table_extract_options(options.model_dump()),
                    )
                )
                return _pdf_table_records(tables, source_row=ref)

        task = asyncio.create_task(asyncio.to_thread(extract))
        self._owner._reads.add(task)
        try:
            try:
                while not task.done():
                    self._owner._check_open()
                    await asyncio.wait({task}, timeout=0.05)
                records, columns = await asyncio.shield(task)
                self._owner._check_open()
            except BaseException:
                await _settle(task)
                raise
        except BlobNotFoundError:
            raise RowError("invalid_pdf_cell", "PDF blob bytes are missing.") from None
        except natural_pdf.NaturalPdfUnavailable:
            raise RowError(
                "pdf_table_adapter_unavailable", "Natural PDF adapter is unavailable."
            ) from None
        except natural_pdf.NaturalPdfUnsupportedMode:
            raise RowError(
                "unsupported_pdf_table_mode", "PDF extraction mode is unsupported."
            ) from None
        except natural_pdf.NaturalPdfError:
            raise RowError(
                "pdf_table_extract_failed", "Natural PDF table extraction failed."
            ) from None
        except ExecutionLimitExceeded:
            raise
        except ValueError as error:
            code, _, message = str(error).partition(":")
            if code != "pdf_table_shape_mismatch":
                code, message = (
                    "pdf_table_extract_failed",
                    "Invalid extracted PDF table cells.",
                )
            raise RowError(code, message.strip()) from None
        finally:
            self._owner._reads.discard(task)
        value = PdfTableRows(records)
        self._results.append(
            (
                value,
                {
                    "input": ref,
                    "options": options.model_dump(mode="json"),
                    "columns": columns,
                    "value_hash": canonical_json_hash(value.root),
                },
            )
        )
        return value

    def publication_facts(self, output, fields, names, output_columns):
        self._owner._check_open()
        facts = []
        for field in fields:
            from frisket.engine.executor.pdf_tables_receipts import pdf_table_output

            if not pdf_table_output(field):
                continue
            value = (
                output.root[field.key]
                if isinstance(output, DynamicOutput)
                else getattr(output, field.key)
            )
            if isinstance(value, Outcome):
                value = value.value if value.status != "failed" else None
            if not isinstance(value, PdfTableRows):
                continue
            issued = next(
                (fact for returned, fact in self._results if returned is value), None
            )
            if (
                issued is None
                or canonical_json_hash(value.root) != issued["value_hash"]
            ):
                raise RowError(
                    "invalid_output",
                    "PDF rows must retain their admitted extraction result.",
                )
            facts.append(
                {
                    **copy.deepcopy(issued),
                    "row_id": self._row_id,
                    "output_key": field.key,
                    "column_id": output_columns.get(names[field.key]),
                }
            )
        return facts


class PdfResultEvidence:
    """Invocation-local associations handed to the existing result savepoint."""

    def __init__(self):
        self._pending = {}

    def capture(self, ctx, facts):
        if ctx.extras.get("preview") is True:
            return
        from frisket.execution.attempt import attempt_in_scope

        attempt = attempt_in_scope(ctx.extras)
        if attempt is None:
            raise RuntimeError("PDF publication requires its admitted writer")
        self._pending[ctx.extras["row_id"]] = (attempt.attempt_id, facts)

    def write(self, project, spec, *, batch, run_id, claim_token, **kwargs):
        del spec, kwargs
        from frisket.engine.store.receipts import ReceiptStore

        rows = {item["row_id"] for item in batch}
        for row_id in rows:
            pending = self._pending.get(row_id)
            if pending is None:
                continue
            writer, facts = pending
            for fact in facts:
                result = next(
                    (
                        item
                        for item in batch
                        if item["row_id"] == row_id
                        and item["column_id"] == fact["column_id"]
                    ),
                    None,
                )
                if result is None or result.get("error") is not None:
                    continue
                ReceiptStore(project).record_pdf_table_read(
                    fact,
                    run_id=run_id,
                    writer_attempt_id=writer,
                    claim_token=claim_token,
                )
        for row_id in rows:
            self._pending.pop(row_id, None)

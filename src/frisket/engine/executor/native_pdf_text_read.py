"""Admitted native-PDF reader over the fenced positioned-text extractor."""

from __future__ import annotations

import copy
import tempfile
import uuid
from pathlib import Path

from pypdf import PdfReader

from frisket.actions.media_types import OcrText
from frisket.actions.types import ColumnRef, Row, RowError
from frisket.engine.pdf_page_source import PdfPageSourceError, materialize_pdf_page
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.execution.provider import enforce_pdf_page_limit


def _page_text(page) -> str:
    parts: list[str] = []
    previous = None
    for token in page.tokens:
        if not token.text.strip():
            continue
        if previous is not None:
            same_line = (
                abs(token.box.y0 - previous.box.y0)
                < max(
                    token.box.y1 - token.box.y0,
                    previous.box.y1 - previous.box.y0,
                )
                * 0.6
            )
            parts.append(" " if same_line else "\n")
        parts.append(token.text)
        previous = token
    return "".join(parts).strip()


class AdmittedNativePdfTextReader:
    def __init__(self, project, *, cancelled=None, execution_limits=None):
        self._project = project
        self._cancelled = cancelled
        self._execution_limits = execution_limits
        self.calls_by_row: dict[int, list[dict]] = {}

    async def aclose(self) -> None:
        return None

    def bind_row(self, row, *, sheet_id, row_id, sources):
        if (
            type(sheet_id) is not int
            or type(row_id) is not int
            or min(sheet_id, row_id) <= 0
        ):
            raise RowError(
                "invalid_input_ref", "Native PDF text requires an admitted row"
            )
        return _BoundNativePdfTextReader(
            self,
            row,
            sheet_id,
            row_id,
            copy.deepcopy(dict(sources or {})),
        )


class _BoundNativePdfTextReader:
    def __init__(self, owner, row, sheet_id, row_id, sources):
        self._owner = owner
        self._row = row
        self._sheet_id = sheet_id
        self._row_id = row_id
        self._sources = sources

    def _source(self, row: Row, source: ColumnRef):
        if row is not self._row or not isinstance(source, ColumnRef):
            raise RowError(
                "invalid_input_ref", "Native PDF text requires its admitted source"
            )
        captured = self._sources.get(source.name)
        if (
            not isinstance(captured, dict)
            or source.name not in row.values
            or canonical_json_hash(source.read(row))
            != canonical_json_hash(captured.get("value"))
        ):
            raise RowError(
                "stale_input", "Native PDF source differs from its admitted cell"
            )
        project = self._owner._project
        column = project.db.execute(
            "SELECT sheet_id,type,hidden FROM columns WHERE id=?",
            (captured.get("column_id"),),
        ).fetchone()
        if (
            column is None
            or column["sheet_id"] != self._sheet_id
            or column["hidden"]
            or column["type"] != "file"
            or captured.get("column_type") != "file"
        ):
            raise RowError(
                "invalid_input_ref",
                "Native PDF source column is unavailable or incompatible",
            )
        value = captured["value"]
        if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
            raise RowError(
                "invalid_media_cell", "Native PDF text requires a blob-backed PDF"
            )
        blob = MediaBlobStore(project).blob_row(value["blob"])
        if blob is None:
            raise RowError("missing_blob", "Native PDF source blob is missing")
        filename = str(value.get("filename") or blob["filename"] or "document")
        mime = str(value.get("mime") or blob["mime"] or "application/octet-stream")
        if mime != "application/pdf" and not filename.lower().endswith(".pdf"):
            raise RowError("invalid_media_cell", "Native PDF text requires a PDF")
        page = value.get("page")
        if page is not None and (type(page) is not int or page < 1):
            raise RowError(
                "invalid_media_cell", "PDF page selector must be a positive integer"
            )
        result = {
            "sheet_id": self._sheet_id,
            "row_id": self._row_id,
            "column_id": int(captured["column_id"]),
            "column_type": "file",
            "source_column": source.name,
            "blob_hash": value["blob"],
            "value_hash": canonical_json_hash(value),
            "filename": filename,
            "mime": mime,
            "size": int(blob["size"]),
        }
        if page is not None:
            result["page"] = page
        return result

    async def read(self, row: Row, source: ColumnRef) -> OcrText:
        source_ref = self._source(row, source)
        if callable(self._owner._cancelled) and self._owner._cancelled():
            raise RowError("action_cancelled", "PDF text reading was cancelled")
        from frisket.engine.pdf_text import (
            PdfTextCancelled,
            PdfTextError,
            extract_pdf_text,
        )

        selected_page = source_ref.get("page")
        try:
            with self._owner._project.materialize_blob(source_ref["blob_hash"]) as raw:
                original = Path(raw)
                try:
                    page_count = len(PdfReader(original).pages)
                except Exception as exc:  # noqa: BLE001 - hostile PDF boundary
                    raise RowError(
                        "invalid_media_cell", "PDF page count could not be read"
                    ) from exc
                if page_count < 1:
                    raise RowError("invalid_media_cell", "PDF contains no pages")
                if self._owner._execution_limits is not None:
                    enforce_pdf_page_limit(
                        self._owner._execution_limits.max_pdf_pages,
                        1 if selected_page is not None else page_count,
                    )
                with tempfile.TemporaryDirectory(prefix="frisket-native-pdf-") as td:
                    extraction_source = original
                    if selected_page is not None:
                        extraction_source = materialize_pdf_page(
                            original,
                            page=selected_page,
                            destination=Path(td) / "selected-page.pdf",
                        )
                    pages = await extract_pdf_text(
                        extraction_source,
                        should_cancel=self._owner._cancelled,
                    )
        except PdfPageSourceError as exc:
            raise RowError("invalid_media_cell", str(exc)) from exc
        except PdfTextCancelled as exc:
            raise RowError(
                "action_cancelled", "PDF text reading was cancelled"
            ) from exc
        except PdfTextError as exc:
            raise RowError("pdf_text_failed", str(exc)) from exc
        except OSError as exc:
            raise RowError(
                "missing_blob", "Native PDF bytes could not be read"
            ) from exc

        if selected_page is not None:
            if len(pages) != 1:
                raise RowError("pdf_text_failed", "Selected PDF page could not be read")
            pages = [pages[0].model_copy(update={"page": selected_page})]
        elif [page.page for page in pages] != list(range(1, page_count + 1)):
            raise RowError("pdf_text_failed", "PDF text extraction was incomplete")

        prepared_pages = []
        blocks = []
        page_images = {}
        for page in pages:
            text = _page_text(page)
            if not text:
                raise RowError("needs_ocr", f"PDF page {page.page} has no native text")
            page_blocks = [
                {
                    "text": token.text,
                    "bbox": {
                        **token.box.model_dump(mode="json"),
                        "space": "page_normalized",
                    },
                }
                for token in page.tokens
                if token.text.strip()
            ]
            prepared_pages.append(
                {"page_number": page.page, "text": text, "blocks": page_blocks}
            )
            blocks.append(
                {"page": page.page, "engine": "native_pdf_text", "blocks": page_blocks}
            )
            page_images[str(page.page)] = {
                "mime": "application/pdf",
                "source_width": page.width,
                "source_height": page.height,
            }

        text = "\n\n".join(page["text"] for page in prepared_pages)
        source_ref["page_count"] = page_count
        call = {
            # Reuse the exact-text prepared-content publication seam.
            "kind": "ocr_read",
            "call_id": uuid.uuid4().hex,
            "engine": "native_pdf_text",
            "ocr_token_granularity": "word",
            "options": {},
            "source": source_ref,
            "text": text,
            "blocks": blocks,
            "pages": prepared_pages,
            "page_images": page_images,
        }
        self._owner.calls_by_row[self._row_id] = [copy.deepcopy(call)]
        return OcrText(text)


__all__ = ["AdmittedNativePdfTextReader"]

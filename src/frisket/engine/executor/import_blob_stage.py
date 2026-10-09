"""Invocation-owned streaming files; only the table host can publish them."""

from __future__ import annotations

import hashlib
import logging
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO, cast

from frisket.actions.types import PdfDocument, PdfPage, StagedFile, TableError
from frisket.engine.executor.local_file_read import _ReadOnlyBinary
from frisket.engine.store.blob_backend import ProjectBlobStore, validate_blob_digest
from frisket.engine.store.import_blobs import ImportBlob, ImportBlobCell, ImportBlobPlan
from frisket.engine.store.prepared_content import PreparedPageDraft
from frisket.engine.store.disk_capacity import require_disk_headroom
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.ops.media_probe import probe_for_ingest


_CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger(__name__)


class AdmittedImportBlobStager:
    def __init__(
        self,
        *,
        cancelled: Callable[[], bool] | None = None,
        probe_metadata: bool = True,
    ) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="frisket-import-")
        self._manifest: dict[StagedFile, ImportBlob] = {}
        self._readers = ExitStack()
        self._closed = False
        self._cancelled = cancelled
        self._probe_metadata = probe_metadata

    def __enter__(self) -> AdmittedImportBlobStager:
        self._require_open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise ValueError("import blob stager is closed")

    def _admitted(self, file: StagedFile) -> ImportBlob:
        self._require_open()
        if not isinstance(file, StagedFile) or file not in self._manifest:
            raise ValueError("staged file was not admitted by this invocation")
        return self._manifest[file]

    def _check_cancelled(self) -> None:
        if self._cancelled is not None and self._cancelled():
            raise TableError("action_cancelled", "File staging was cancelled.")

    def stage(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        mime: str,
        role: PdfDocument | PdfPage | None = None,
    ) -> StagedFile:
        self._require_open()
        self._check_cancelled()
        if (
            not isinstance(filename, str)
            or not filename
            or not isinstance(mime, str)
            or not mime
        ):
            raise ValueError("staged files require a filename and MIME type")
        document_id = None
        if isinstance(role, PdfPage):
            document = self._admitted(role.document)
            if (
                document.role != "document"
                or type(role.page) is not int
                or role.page < 1
            ):
                raise ValueError(
                    "PDF page requires an admitted document and positive page"
                )
            document_id = document.occurrence_id
        elif role is not None and not isinstance(role, PdfDocument):
            raise ValueError("invalid staged import role")
        occurrence_id = len(self._manifest)
        path = Path(self._directory.name) / str(occurrence_id)
        digest, size = hashlib.sha256(), 0
        try:
            with path.open("xb") as sink:
                while True:
                    self._check_cancelled()
                    chunk = stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    require_disk_headroom(path.parent, len(chunk))
                    sink.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            self._check_cancelled()
            observed = digest.hexdigest()
            metadata = (
                owned_media_metadata_document(
                    probe=probe_for_ingest(
                        path, filename=filename, mime=mime, digest=observed
                    )
                )
                if self._probe_metadata
                else {}
            )
            self._check_cancelled()
            file = StagedFile(size=size)
            self._manifest[file] = ImportBlob(
                occurrence_id=occurrence_id,
                path=path,
                digest=observed,
                size=size,
                filename=filename,
                mime=mime,
                metadata=metadata,
                role="page"
                if isinstance(role, PdfPage)
                else "document"
                if role is not None
                else "attachment",
                document_id=document_id,
                page=role.page if isinstance(role, PdfPage) else None,
            )
            return file
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def lower(self, file: StagedFile) -> dict[str, Any]:
        blob = self._admitted(file)
        cell = media_cell(blob.digest, mime=blob.mime, filename=blob.filename)
        if blob.role == "document_page":
            cell["page"] = blob.page
        return cell

    def reference_pdf_page(
        self,
        document: StagedFile,
        *,
        page: int,
        text: str | None = None,
    ) -> StagedFile:
        """Create a page-scoped cell backed by the original admitted PDF."""
        source = self._admitted(document)
        if source.role != "document" or type(page) is not int or page < 1:
            raise ValueError("PDF page requires an admitted document and positive page")
        if text is not None and not isinstance(text, str):
            raise ValueError("prepared PDF page text must be a string or None")
        file = StagedFile(size=source.size)
        self._manifest[file] = ImportBlob(
            occurrence_id=len(self._manifest),
            path=None,
            digest=source.digest,
            size=source.size,
            filename=source.filename,
            mime=source.mime,
            metadata={},
            role="document_page",
            document_id=source.occurrence_id,
            page=page,
            prepared_text=text,
            prepared_column_name="text",
        )
        return file

    def admit_owned(
        self,
        owner: ProjectBlobStore,
        *,
        digest: str,
        size: int,
        filename: str,
        mime: str,
        occurrence_ref: dict[str, Any] | None = None,
    ) -> StagedFile:
        """Reuse host inventory facts after verified canonical-byte admission.

        This is not an authored action capability. The import host must obtain
        these facts from its durable inventory, written only after put_path
        succeeds, and bind the same project's backend. It avoids downloading or
        copying originals again merely to publish their row references.
        """
        self._require_open()
        self._check_cancelled()
        validate_blob_digest(digest)
        if type(size) is not int or size < 0:
            raise ValueError("owned import blob size must be nonnegative")
        if (
            not isinstance(filename, str)
            or not filename
            or not isinstance(mime, str)
            or not mime
        ):
            raise ValueError("owned import blobs require a filename and MIME type")
        file = StagedFile(size=size)
        self._manifest[file] = ImportBlob(
            occurrence_id=len(self._manifest),
            path=None,
            digest=digest,
            size=size,
            filename=filename,
            mime=mime,
            metadata={},
            role="attachment",
            owner=owner,
            occurrence_ref=dict(occurrence_ref) if occurrence_ref is not None else None,
        )
        return file

    def describe(self, file: StagedFile) -> ImportBlob:
        """Host-only metadata for an admitted scratch file; never publish a path."""
        return self._admitted(file)

    def associate_packet_split(
        self,
        document: StagedFile,
        child: StagedFile,
        *,
        source_page_count: int,
        sibling_count: int,
        page_start: int,
        page_end: int,
        ocr_pages,
        prepared_column_name: str,
    ) -> None:
        """Attach trusted packet lineage to two invocation-owned staged files."""
        source = self._admitted(document)
        derived = self._admitted(child)
        if source.role != "document" or derived.role != "attachment":
            raise ValueError("packet split requires one document and one child")
        if (
            type(source_page_count) is not int
            or type(sibling_count) is not int
            or type(page_start) is not int
            or type(page_end) is not int
            or source_page_count < 1
            or sibling_count < 1
            or not 1 <= page_start <= page_end <= source_page_count
        ):
            raise ValueError("invalid packet page range")
        page_total = page_end - page_start + 1
        drafts = []
        engine = None
        token_granularity = None
        for expected, page in enumerate(ocr_pages, 1):
            if (
                type(getattr(page, "page", None)) is not int
                or page.page != expected
                or not isinstance(getattr(page, "text", None), str)
                or not isinstance(getattr(page, "blocks", None), tuple)
                or not isinstance(getattr(page, "engine", None), str)
                or not page.engine
                or getattr(page, "token_granularity", None)
                not in {"word", "line", "block"}
            ):
                raise ValueError("invalid prepared packet OCR page")
            if engine is not None and page.engine != engine:
                raise ValueError("packet OCR pages must use one engine")
            if (
                token_granularity is not None
                and page.token_granularity != token_granularity
            ):
                raise ValueError("packet OCR pages must use one token granularity")
            engine = page.engine
            token_granularity = page.token_granularity
            drafts.append(
                PreparedPageDraft(
                    page_number=page.page,
                    text=page.text,
                    positions={"engine": page.engine, "blocks": list(page.blocks)},
                )
            )
        if drafts and len(drafts) != page_total:
            raise ValueError("packet OCR must cover every child page")
        if not isinstance(prepared_column_name, str) or not prepared_column_name:
            raise ValueError("packet OCR requires a prepared output column")
        self._manifest[document] = replace(
            source,
            page_count=source_page_count,
            occurrence_ref={
                "kind": "pdf_packet_split_source",
                "sibling_count": sibling_count,
            },
        )
        self._manifest[child] = replace(
            derived,
            packet_source_id=source.occurrence_id,
            packet_page_start=page_start,
            packet_page_end=page_end,
            packet_sibling_count=sibling_count,
            prepared_pages=tuple(drafts),
            prepared_engine=engine,
            prepared_token_granularity=token_granularity,
            prepared_column_name=prepared_column_name if drafts else None,
        )

    def stage_acquired_url(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        mime: str,
        source_url: str,
        provider: str,
        acquisition: dict[str, Any],
    ) -> StagedFile:
        """Host-only acquisition facts; not part of the authored stager protocol."""
        file = self.stage(stream, filename=filename, mime=mime)
        blob = self._admitted(file)
        metadata = dict(blob.metadata)
        if acquisition:
            metadata.update(owned_media_metadata_document(acquisition=acquisition))
        self._manifest[file] = replace(
            blob, metadata=metadata, source_url=source_url, provider=provider
        )
        return file

    @contextmanager
    def open_binary(self, file: StagedFile) -> Iterator[BinaryIO]:
        blob = self._admitted(file)
        with ExitStack() as opened:
            self._readers.callback(opened.close)
            path = (
                opened.enter_context(blob.owner.materialize(blob.digest))
                if blob.owner is not None
                else blob.path
            )
            if path is None:
                raise ValueError("import blob has no readable bytes")
            stream = opened.enter_context(path.open("rb"))
            reader = opened.enter_context(_ReadOnlyBinary(stream))
            yield cast(BinaryIO, reader)

    def publication_plan(
        self,
        occurrences: Iterable[tuple[int, str, StagedFile]],
        *,
        output_names: Mapping[str, str] | None = None,
    ) -> ImportBlobPlan:
        self._require_open()
        cells = []
        included = {
            blob.occurrence_id
            for blob in self._manifest.values()
            if blob.role == "document"
        }
        for row_id, column_name, file in occurrences:
            blob = self._admitted(file)
            if (
                type(row_id) is not int
                or row_id < 0
                or not isinstance(column_name, str)
                or not column_name
            ):
                raise ValueError("invalid staged file occurrence")
            cells.append(ImportBlobCell(blob.occurrence_id, row_id, column_name))
            included.add(blob.occurrence_id)
            if blob.document_id is not None:
                included.add(blob.document_id)
        blobs = []
        for blob in self._manifest.values():
            if blob.occurrence_id not in included:
                continue
            prepared_column_name = blob.prepared_column_name
            if prepared_column_name is not None and output_names is not None:
                if prepared_column_name not in output_names:
                    raise ValueError(
                        "prepared import output has no resolved column name"
                    )
                blob = replace(
                    blob,
                    prepared_column_name=output_names[prepared_column_name],
                )
            blobs.append(blob)
        return ImportBlobPlan(blobs=tuple(blobs), cells=tuple(cells))

    def finish_reads(self) -> None:
        """Close scratch readers before publication may unlink their files."""

        self._require_open()
        self._readers.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._readers.close()
        except Exception:
            logger.warning("import staging reader cleanup failed", exc_info=True)
        finally:
            try:
                self._directory.cleanup()
            except OSError:
                # A scratch cleanup failure cannot reverse committed success.
                logger.warning("import staging cleanup failed", exc_info=True)

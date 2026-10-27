"""Process-local orchestration for guided PDF packet splitting.

Admitted sources remain in their upload or project owner during review.
Analysis files and preview job handles remain process-local; durable apply
freezes its inputs into the existing import and queue owners.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Sequence
from urllib.parse import quote

from PIL import Image
from pypdf import PdfReader

from frisket.contracts.http.selector_choices import EngineSelection
from frisket.contracts.http.pdf_packet_splits import (
    PDF_PACKET_SPLIT_SCHEMA_VERSION,
    PdfPacketCandidatesRequest,
    PdfPacketBatchOcrEstimateRequest,
    PdfPacketCommitRequest,
    PdfPacketOcrEstimateRequest,
    PdfPacketOcrJobRequest,
    PdfPacketSheetCreateRequest,
    PdfPacketSheetScopeRequest,
    PdfPacketTextSourceRequest,
)
from frisket.engine.pdf_render import render_pdf_pages
from frisket.engine.pdf_text import (
    PdfTextError,
    extract_pdf_text,
    extract_pdf_text_batch,
)
from frisket.engine.executor.table_preview import TablePreviewResult
from frisket.pdf_packets.names import child_filename
from frisket.pdf_packets import (
    PhraseRule,
    SourceMap,
    SourceOccurrence,
    match_packet_pages,
    page_perceptual_signature,
)
from frisket.server.route_errors import RouteError
from frisket.server.import_admission import ImportAdmissionLease
from frisket.server.services.action_preview_jobs import (
    ActionPreviewJobRegistry,
    PreviewJob,
)
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.services.pdf_packet_commit import PdfPacketCommitter
from frisket.server.services.pdf_packet_sources import (
    AdmittedPacketSource,
    MAX_INTERACTIVE_PDF_PAGES,
)
from frisket.server.services.pdf_packet_text import (
    PacketTextInspection,
    PacketTextPage,
    inspect_packet_source,
    sample_packet_page,
    read_prepared_page,
    refresh_packet_source,
)
from frisket.server.workspace import Workspace


_SESSION_TTL_SECONDS = 60 * 60
_PREPARE_DPI = 96
_THUMBNAIL_MAX_EDGE = 1400


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _page_signature(path: Path) -> int:
    with Image.open(path) as image:
        return page_perceptual_signature(image)


class PdfPacketSplitRouteError(RouteError):
    pass


@dataclass
class _PacketJob:
    id: str
    kind: str
    pages: tuple[int, ...] = ()
    engine: str | None = None
    copied: bool = False
    page_map: dict[int, int] | None = None
    sample_target: Any = None


@dataclass
class _PacketSession:
    id: str
    project_id: str
    source_hash: str
    filename: str
    size: int
    directory: Path
    source_path: Path
    page_count: int
    expires_at: datetime
    sources: tuple[AdmittedPacketSource, ...]
    source_map: SourceMap
    organization: Literal["pages", "split_documents"]
    prepare_job_id: str = ""
    status: str = "preparing"
    pages_ready: set[int] = field(default_factory=set)
    native_text: dict[int, str] = field(default_factory=dict)
    native_pages: dict[int, dict[str, Any]] = field(default_factory=dict)
    signatures: dict[int, int] = field(default_factory=dict)
    text_inspections: dict[str, PacketTextInspection] = field(default_factory=dict)
    text_identities: dict[str, tuple[int | None, int | None]] = field(
        default_factory=dict
    )
    jobs: dict[str, _PacketJob] = field(default_factory=dict)
    ocr: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)
    text_source: str = "unconfirmed"
    ocr_engine: str | None = None
    analysis_revision: int = 0
    example_pages: tuple[int, ...] = ()
    sheet_scope: dict[str, Any] | None = None
    ocr_quote: Any = None
    sample_quote: Any = None
    commit_result: dict[str, Any] | None = None
    commit_keys: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    admission_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    close_event: threading.Event = field(default_factory=threading.Event, repr=False)


@dataclass
class _ThumbnailLease:
    path: Path

    def close(self) -> None:
        self.path.unlink(missing_ok=True)


class PdfPacketSplitService:
    """Own packet sessions and reuse the normal preview/import hosts."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        registry: ActionPreviewJobRegistry,
        preview_runs: ActionPreviewRunService,
        page_signature: Callable[[Path], int] | None = None,
        ttl_seconds: float = _SESSION_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        search_preparation: Any = None,
        sheet_sources: Callable[[str, PdfPacketSheetCreateRequest], Any] | None = None,
        sheet_scope_token: Callable[[str, PdfPacketSheetScopeRequest], dict[str, str]]
        | None = None,
        reviewed_sources: Callable[[str, str, int, str], Sequence[AdmittedPacketSource]]
        | None = None,
    ) -> None:
        self._workspace = workspace
        self._registry = registry
        self._preview_runs = preview_runs
        self._committer = PdfPacketCommitter(workspace)
        self._page_signature = page_signature or _page_signature
        self._ttl_seconds = ttl_seconds
        self._clock = clock or _utc_now
        self._search_preparation = search_preparation
        self._reviewed_sources = reviewed_sources
        self._sheet_sources = sheet_sources
        self._sheet_scope_token = sheet_scope_token
        self._sessions: dict[str, _PacketSession] = {}
        self._lock = threading.RLock()

    def create(
        self,
        project_id: str,
        upload: AdmittedUpload,
        *,
        request_id: str | None = None,
        admission_lease: ImportAdmissionLease | None = None,
    ) -> dict[str, Any]:
        del request_id
        self._reap_expired_sessions()
        self._workspace.get(project_id)
        if upload.mime not in ("application/pdf", "application/octet-stream"):
            raise PdfPacketSplitRouteError(422, "packet upload must be a PDF")
        directory = Path(tempfile.mkdtemp(prefix="frisket-pdf-packet-"))
        source_path = directory / "source.pdf"
        try:
            digest = hashlib.sha256()
            size = 0
            with self._upload_source(upload) as source, source_path.open("xb") as sink:
                while chunk := source.read(1024 * 1024):
                    sink.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if digest.hexdigest() != upload.sha256 or size != upload.size:
                raise PdfPacketSplitRouteError(
                    409, "uploaded PDF changed after admission"
                )
            with source_path.open("rb") as source:
                header = source.read(5)
            if header != b"%PDF-":
                raise PdfPacketSplitRouteError(422, "packet upload is not a PDF")
            try:
                page_count = len(PdfReader(source_path, strict=False).pages)
            except Exception as exc:
                raise PdfPacketSplitRouteError(422, "PDF could not be parsed") from exc
            if not 1 <= page_count <= MAX_INTERACTIVE_PDF_PAGES:
                raise PdfPacketSplitRouteError(
                    422, f"PDF must contain 1 to {MAX_INTERACTIVE_PDF_PAGES} pages"
                )
            admitted = AdmittedPacketSource(
                key=uuid.uuid4().hex,
                sha256=upload.sha256,
                filename=upload.filename,
                size=size,
                page_count=page_count,
                page_start=1,
                page_end=page_count,
                open_source=lambda: contextlib.nullcontext(source_path),
            )
            return self.create_from_sources(
                project_id,
                [admitted],
                organization="split_documents",
                admission_lease=admission_lease,
                _directory=directory,
            )
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def sheet_scope_token(
        self, project_id: str, body: PdfPacketSheetScopeRequest
    ) -> dict[str, str]:
        if self._sheet_scope_token is None:
            raise PdfPacketSplitRouteError(503, "sheet scope admission is unavailable")
        try:
            return self._sheet_scope_token(project_id, body)
        except ValueError as exc:
            raise PdfPacketSplitRouteError(422, str(exc)) from exc

    def create_from_sheet(
        self,
        project_id: str,
        body: PdfPacketSheetCreateRequest,
        *,
        admission_lease: ImportAdmissionLease | None = None,
    ) -> dict[str, Any]:
        if self._sheet_sources is None:
            raise PdfPacketSplitRouteError(503, "sheet source admission is unavailable")
        try:
            inventory = self._sheet_sources(project_id, body)
        except ValueError as exc:
            raise PdfPacketSplitRouteError(409, str(exc)) from exc
        if not inventory.sources:
            raise PdfPacketSplitRouteError(422, "sheet scope has no eligible PDFs")
        created = self.create_from_sources(
            project_id,
            inventory.sources,
            organization=body.organization,
            admission_lease=admission_lease,
        )
        session = self._require(split_id=created["split_id"])
        with self._lock:
            session.sheet_scope = {
                "excluded": list(inventory.excluded),
                "excluded_count": inventory.excluded_count,
                "preview": list(inventory.preview),
                "order_hash": inventory.order_hash,
            }
        return self._snapshot(session)

    def create_from_draft(
        self,
        project_id: str,
        draft_id: str,
        expected_revision: int,
        *,
        organization: Literal["pages", "split_documents"],
        admission_lease: ImportAdmissionLease | None = None,
    ) -> dict[str, Any]:
        if self._reviewed_sources is None:
            raise PdfPacketSplitRouteError(
                503, "PDF draft source admission is unavailable"
            )
        sources = self._reviewed_sources(
            project_id, draft_id, expected_revision, organization
        )
        return self.create_from_sources(
            project_id,
            sources,
            organization=organization,
            admission_lease=admission_lease,
        )

    def create_from_sources(
        self,
        project_id: str,
        sources: Sequence[AdmittedPacketSource],
        *,
        organization: Literal["pages", "split_documents"],
        admission_lease: ImportAdmissionLease | None = None,
        _directory: Path | None = None,
    ) -> dict[str, Any]:
        """Create from server-resolved sources, never client-supplied paths."""
        self._reap_expired_sessions()
        self._workspace.get(project_id)
        if organization not in ("pages", "split_documents") or not sources:
            raise PdfPacketSplitRouteError(422, "invalid packet source selection")
        sources = tuple(sources)
        try:
            source_map = SourceMap(
                tuple(
                    SourceOccurrence(s.key, s.sha256, s.page_start, s.page_end)
                    for s in sources
                )
            )
        except ValueError as exc:
            raise PdfPacketSplitRouteError(422, str(exc)) from exc
        if (
            organization == "split_documents"
            and source_map.page_count > MAX_INTERACTIVE_PDF_PAGES
        ):
            raise PdfPacketSplitRouteError(
                422, f"review supports up to {MAX_INTERACTIVE_PDF_PAGES} pages"
            )
        for source in sources:
            with source.open_source() as path:
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                if digest.hexdigest() != source.sha256 or size != source.size:
                    raise PdfPacketSplitRouteError(409, "admitted PDF changed")
                try:
                    count = len(PdfReader(path, strict=False).pages)
                except Exception as exc:
                    raise PdfPacketSplitRouteError(
                        422, "PDF could not be parsed"
                    ) from exc
                if count != source.page_count or source.page_end > count:
                    raise PdfPacketSplitRouteError(409, "admitted PDF extent changed")
        directory = _directory or Path(tempfile.mkdtemp(prefix="frisket-pdf-packet-"))
        first = sources[0]
        session_id = uuid.uuid4().hex
        session = _PacketSession(
            id=session_id,
            project_id=project_id,
            source_hash=first.sha256,
            filename=first.filename,
            size=first.size,
            directory=directory,
            source_path=directory / "source.pdf",
            page_count=source_map.page_count,
            expires_at=self._expiry(),
            sources=sources,
            source_map=source_map,
            organization=organization,
            example_pages=(1,)
            if len(sources) == 1 and organization == "split_documents"
            else (),
        )
        try:
            with self._lock:
                self._sessions[session_id] = session
            with self._admit_session(session):
                job = self._start_background_job(
                    admission_lease,
                    lambda on_finished: self._registry.start(
                        project_id,
                        session.page_count,
                        lambda progress, cancelled: self._prepare(
                            session_id, progress=progress, cancelled=cancelled
                        ),
                        replacement_key=f"pdf-packet:{session_id}:prepare",
                        on_finished=on_finished,
                    ),
                )
                with self._lock:
                    session.prepare_job_id = job.id
                    session.jobs[job.id] = _PacketJob(job.id, "prepare")
            return self._snapshot(session)
        except BaseException:
            with self._lock:
                self._sessions.pop(session_id, None)
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @contextlib.contextmanager
    def _upload_source(self, upload: AdmittedUpload) -> Iterator[Any]:
        if upload.open_source is not None:
            with upload.open_source() as source:
                yield source
            return
        assert upload.source is not None
        upload.source.seek(0)
        try:
            yield upload.source
        finally:
            upload.source.seek(0)

    def _prepare(self, split_id: str, *, progress, cancelled) -> None:
        session = self._require(split_id=split_id)
        thumbs = session.directory / "pages"
        thumbs.mkdir()
        try:
            for source_index, source in enumerate(session.sources):
                if source.source_sheet_id is not None:
                    source, identity = refresh_packet_source(
                        self._workspace.get(session.project_id), source
                    )
                    with self._lock:
                        updated = list(session.sources)
                        updated[source_index] = source
                        session.sources = tuple(updated)
                        session.text_identities[source.key] = identity
                    if cancelled.is_set():
                        raise RuntimeError("packet preparation was cancelled")
                    if session.organization == "pages":
                        inspected = inspect_packet_source(
                            self._workspace.get(session.project_id),
                            source,
                            should_cancel=cancelled.is_set,
                        )
                        with self._lock:
                            session.text_inspections[source.key] = inspected
                            session.analysis_revision += 1
            if session.organization == "pages":
                with self._lock:
                    session.status = "ready"
                    session.analysis_revision += 1
                progress(session.page_count, session.page_count)
                return
            for source, offset in zip(
                session.sources, session.source_map.fixed_starts, strict=True
            ):
                source_thumbs = thumbs / str(offset)
                source_thumbs.mkdir()
                with source.open_source() as source_path:
                    if source.prepared_ref_id is not None:
                        project = self._workspace.get(session.project_id)
                        cached_pages = set(source.prepared_pages)
                        for local_page in sorted(cached_pages):
                            cached = read_prepared_page(
                                project, source.prepared_ref_id, local_page
                            )
                            if cached is not None:
                                with self._lock:
                                    session.native_text[
                                        offset + local_page - source.page_start
                                    ] = cached.text
                        missing = [
                            page
                            for page in range(source.page_start, source.page_end + 1)
                            if page not in cached_pages
                        ]
                        while missing:
                            first = missing.pop(0)
                            length = 1
                            while (
                                missing and missing[0] == first + length and length < 64
                            ):
                                missing.pop(0)
                                length += 1
                            try:
                                batch = asyncio.run(
                                    extract_pdf_text_batch(
                                        source_path,
                                        start_page=first,
                                        max_pages=length,
                                        should_cancel=cancelled.is_set,
                                    )
                                )
                            except PdfTextError:
                                continue
                            with self._lock:
                                for page in batch.pages:
                                    self._cache_native_page(
                                        session,
                                        offset + page.page - source.page_start,
                                        page,
                                    )
                    else:
                        try:
                            positioned = []
                            if (
                                source.page_count <= 64
                                and source.page_start == 1
                                and source.page_end == source.page_count
                            ):
                                positioned = asyncio.run(
                                    extract_pdf_text(
                                        source_path,
                                        should_cancel=cancelled.is_set,
                                        max_pages=64,
                                    )
                                )
                            else:
                                next_page = source.page_start
                                while next_page <= source.page_end:
                                    batch = asyncio.run(
                                        extract_pdf_text_batch(
                                            source_path,
                                            start_page=next_page,
                                            max_pages=min(
                                                64, source.page_end - next_page + 1
                                            ),
                                            should_cancel=cancelled.is_set,
                                        )
                                    )
                                    positioned.extend(batch.pages)
                                    if batch.next_page is None:
                                        break
                                    next_page = batch.next_page
                        except PdfTextError:
                            positioned = []
                        with self._lock:
                            for page in positioned:
                                if source.page_start <= page.page <= source.page_end:
                                    self._cache_native_page(
                                        session,
                                        offset + page.page - source.page_start,
                                        page,
                                    )
                    if source.source_sheet_id is not None:
                        known_pages = {
                            page - offset + source.page_start: PacketTextPage(
                                value["text"], "native", False
                            )
                            for page, value in session.native_pages.items()
                            if offset
                            <= page
                            < offset + source.page_end - source.page_start + 1
                        }
                        inspected = inspect_packet_source(
                            self._workspace.get(session.project_id),
                            source,
                            should_cancel=cancelled.is_set,
                            known_pages=known_pages,
                        )
                        with self._lock:
                            session.text_inspections[source.key] = inspected
                            session.analysis_revision += 1
                    local_pages = list(range(source.page_start, source.page_end + 1))
                    sample = [
                        source.page_start + page - 1
                        for page in self._sample_pages(len(local_pages))
                    ]
                    ordered_pages = sample + [
                        page for page in local_pages if page not in sample
                    ]
                    for batch_start in range(0, len(ordered_pages), 64):
                        pages = ordered_pages[batch_start : batch_start + 64]
                        rendered = asyncio.run(
                            render_pdf_pages(
                                source_path,
                                source_thumbs,
                                dpi=_PREPARE_DPI,
                                pages=pages,
                                should_cancel=cancelled.is_set,
                                max_edge=_THUMBNAIL_MAX_EDGE,
                                timeout_seconds=max(30, len(pages) * 5),
                            )
                        )
                        signatures = [
                            self._page_signature(path) for _page, path in rendered.pages
                        ]
                        with self._lock:
                            for (local_page, _path), signature in zip(
                                rendered.pages, signatures, strict=True
                            ):
                                page = offset + local_page - source.page_start
                                session.pages_ready.add(page)
                                session.signatures[page] = signature
                            session.analysis_revision += 1
                            done = len(session.pages_ready)
                        progress(done, session.page_count)
            with self._lock:
                session.status = "ready"
                session.analysis_revision += 1
        except BaseException:
            with self._lock:
                if cancelled.is_set():
                    session.status = "cancelled"
                else:
                    session.status = "error"
            raise

    @staticmethod
    def _cache_native_page(
        session: _PacketSession, global_page: int, page: Any
    ) -> None:
        text = " ".join(token.text for token in page.tokens).strip()
        session.native_text[global_page] = text
        if (
            not all(hasattr(token, "box") for token in page.tokens)
            or not hasattr(page, "width")
            or not hasattr(page, "height")
        ):
            return
        blocks = [
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
        positions = {
            "engine": "native",
            "width": page.width,
            "height": page.height,
            "blocks": blocks,
        }
        session.native_pages[global_page] = {
            "text": text,
            "engine": "native",
            "positions": positions,
            "blocks": blocks,
            "width": page.width,
            "height": page.height,
        }

    @staticmethod
    def _sample_pages(page_count: int) -> list[int]:
        count = min(6, page_count)
        if count == 1:
            return [1]
        return sorted(
            {
                1 + round(index * (page_count - 1) / (count - 1))
                for index in range(count)
            }
        )

    def status(self, project_id: str, split_id: str) -> dict[str, Any]:
        session = self._session(project_id, split_id)
        self._sync_jobs(session)
        return self._snapshot(session)

    def page(self, project_id: str, split_id: str, page: int) -> dict[str, Any]:
        session = self._session(project_id, split_id)
        self._validate_pages(session, [page])
        self._sync_jobs(session)
        ocr_engine = session.ocr_engine
        if ocr_engine is None or page not in session.ocr.get(ocr_engine, {}):
            ocr_engine = next(
                (
                    engine
                    for engine, pages in reversed(tuple(session.ocr.items()))
                    if page in pages
                ),
                None,
            )
        ocr = session.ocr.get(ocr_engine or "", {}).get(page)
        address = session.source_map.resolve(page)
        source = next(
            item for item in session.sources if item.key == address.occurrence.key
        )
        inspection = session.text_inspections.get(source.key)
        sample = inspection.pages.get(address.local_page) if inspection else None
        if inspection is not None and sample is None:
            try:
                sample = sample_packet_page(
                    self._workspace.get(project_id), source, address.local_page
                )
            except Exception:
                sample = None
        return {
            "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
            "split_id": split_id,
            "page": page,
            "source_key": address.occurrence.key,
            "local_page": address.local_page,
            "preview_text": sample.text if sample else None,
            "preview_source": sample.source if sample else None,
            "preview_text_kind": ("ocr" if sample.completed_ocr else "native")
            if sample
            else None,
            "preview_truncated": sample.truncated if sample else False,
            "thumbnail_ready": page in session.pages_ready,
            "thumbnail_url": (
                f"/api/projects/{quote(project_id, safe='')}/import/pdf-packet-splits/"
                f"{quote(split_id, safe='')}/pages/{page}/thumbnail"
            ),
            "native_text": session.native_text.get(page),
            "ocr_text": ocr.get("text") if ocr else None,
            "ocr_blocks": list(ocr.get("blocks") or []) if ocr else [],
            "ocr_engine": ocr_engine if ocr is not None else None,
        }

    def thumbnail(self, project_id: str, split_id: str, page: int) -> _ThumbnailLease:
        session = self._session(project_id, split_id)
        with self._admit_session(session):
            self._validate_pages(session, [page])
            address = session.source_map.resolve(page)
            index = session.source_map.occurrences.index(address.occurrence)
            offset = session.source_map.fixed_starts[index]
            source = (
                session.directory
                / "pages"
                / str(offset)
                / f"page-{address.local_page}.png"
            )
            if not source.is_file() and session.organization == "pages":
                admitted = session.sources[index]
                source.parent.mkdir(parents=True, exist_ok=True)
                with admitted.open_source() as path:
                    asyncio.run(
                        render_pdf_pages(
                            path,
                            source.parent,
                            dpi=_PREPARE_DPI,
                            pages=[address.local_page],
                            should_cancel=session.close_event.is_set,
                            max_edge=_THUMBNAIL_MAX_EDGE,
                            timeout_seconds=30,
                        )
                    )
                if source.is_file():
                    with self._lock:
                        session.pages_ready.add(page)
            if not source.is_file():
                raise PdfPacketSplitRouteError(409, "page thumbnail is not ready")
            fd, name = tempfile.mkstemp(prefix="frisket-packet-thumb-", suffix=".png")
            os.close(fd)
            target = Path(name)
            shutil.copyfile(source, target)
        return _ThumbnailLease(target)

    @staticmethod
    def _sample_request(
        body: PdfPacketOcrEstimateRequest,
    ) -> PdfPacketOcrEstimateRequest:
        selection = body.ocr_selection
        if selection is None:
            engine = (body.engine or "").strip()
            if not engine or engine == "llm":
                raise PdfPacketSplitRouteError(
                    422, "choose a concrete OCR engine or model"
                )
            selection = EngineSelection(kind="engine", engine=engine)
        if (
            selection.kind == "engine_model"
            and selection.engine == "llm"
            and not selection.model
        ):
            raise PdfPacketSplitRouteError(422, "choose a concrete OCR model")
        if selection.kind == "engine" and selection.engine == "llm":
            raise PdfPacketSplitRouteError(422, "choose a concrete OCR model")
        from frisket.server.services.pdf_packet_ocr import packet_ocr_params

        try:
            packet_ocr_params(selection.model_dump(mode="json"))
        except ValueError as exc:
            raise PdfPacketSplitRouteError(422, "invalid OCR engine selection") from exc
        return body.model_copy(update={"ocr_selection": selection})

    def estimate_ocr(
        self,
        project_id: str,
        split_id: str,
        body: PdfPacketOcrEstimateRequest,
        *,
        request_context: Any = None,
    ) -> dict[str, Any]:
        session = self._session(project_id, split_id, ready=True)
        return self._estimate_source_sample(
            session, self._sample_request(body), request_context=request_context
        )

    @staticmethod
    def _ocr_inventory(
        session: _PacketSession, source: AdmittedPacketSource | None = None
    ) -> Path:
        if source is not None and source.preview_inventory_path is not None:
            return source.preview_inventory_path
        paths = {item.preview_inventory_path for item in session.sources}
        if len(paths) == 1 and None not in paths:
            return next(iter(paths))
        return session.directory / "ocr-inventory.sqlite"

    def _estimate_source_sample(
        self,
        session: _PacketSession,
        body: PdfPacketOcrEstimateRequest,
        *,
        request_context: Any = None,
    ) -> dict[str, Any]:
        from frisket.server.services.pdf_packet_ocr import (
            PdfPacketOcrCoordinator,
            packet_ocr_params,
        )

        if body.scope != "sample" or body.pages is None or len(body.pages) != 1:
            raise PdfPacketSplitRouteError(422, "choose one page for an OCR sample")
        with self._admit_session(session, ready=True):
            self._refresh_text_sources(session)
            page = body.pages[0]
            self._validate_pages(session, [page])
            address = session.source_map.resolve(page)
            source = next(
                item for item in session.sources if item.key == address.occurrence.key
            )
            params = packet_ocr_params(body.ocr_selection.model_dump(mode="json"))
            coordinator = PdfPacketOcrCoordinator(
                self._workspace,
                preview_runs=self._preview_runs,
                inventory_path=self._ocr_inventory(session, source),
            )
            quoted = coordinator.estimate_sample(
                project_id=session.project_id,
                sources=session.sources,
                source_key=source.key,
                page=address.local_page,
                revision=session.analysis_revision,
                ocr_selection=body.ocr_selection.model_dump(mode="json"),
                force_rescan=body.force_rescan,
                request_context=request_context,
            )
            target = quoted["target"]
            token = quoted["plan_token"]
            if quoted["pages"] == 0:
                cached = self._cached_sample_payload(session, target)
                if cached is not None:
                    self._record_cached_sample(
                        session.id, params["engine"], {**cached, "page": page}
                    )
            session.sample_quote = (
                token,
                target,
                page,
                quoted,
                session.analysis_revision,
                body.force_rescan,
            )
            return {
                "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
                "engine": params["engine"],
                "scope": "sample",
                "pages": [page],
                "cached_pages": [page] if quoted["pages"] == 0 else [],
                "estimate": quoted.get("estimate") if quoted["pages"] else None,
            }

    def _cached_sample_payload(
        self, session: _PacketSession, target: Any
    ) -> dict[str, Any] | None:
        from frisket.server.services.pdf_split_preparation import (
            PdfSplitPreparationAdapter,
            PdfSplitPreparationStore,
        )

        source = next(
            item for item in session.sources if item.key == target.occurrence_key
        )
        inventory = self._ocr_inventory(session, source)
        with PdfSplitPreparationStore(inventory) as store:
            cached = store.cached_preview(target)
            ref = store.cached_ref(target)
        if cached is not None:
            return dict(cached)
        adapter = PdfSplitPreparationAdapter(
            self._workspace,
            preview_runs=self._preview_runs,
            inventory_path=inventory,
            project_id=session.project_id,
            admitted_sources={item.key: item for item in session.sources},
        )
        ref = ref or adapter.cached_reference(target)
        if ref is None:
            return None
        project = self._workspace.get(session.project_id)
        page = read_prepared_page(project, ref, target.page_number)
        row = project.db.execute(
            "SELECT version.positions_json FROM prepared_content_refs ref JOIN prepared_content_set_pages member ON member.set_id=ref.set_id AND member.page_number=? JOIN prepared_page_versions version ON version.id=member.version_id WHERE ref.id=?",
            (target.page_number, ref),
        ).fetchone()
        geometry = json.loads(row[0]) if row and row[0] else {}
        if page is None:
            return None
        return {
            "text": page.text,
            "blocks": geometry.get("blocks", []),
            "width": geometry.get("width"),
            "height": geometry.get("height"),
        }

    def _refresh_text_sources(self, session: _PacketSession) -> None:
        project = self._workspace.get(session.project_id)
        changed = []
        updated = []
        try:
            for source in session.sources:
                fresh, identity = refresh_packet_source(project, source)
                updated.append(fresh)
                if (
                    source.source_sheet_id is not None
                    and session.text_identities.get(source.key) != identity
                ):
                    changed.append((fresh, identity))
        except ValueError as exc:
            raise PdfPacketSplitRouteError(409, str(exc)) from exc
        if not changed:
            return
        inspections = {
            source.key: inspect_packet_source(project, source) for source, _ in changed
        }
        with self._lock:
            session.sources = tuple(updated)
            for source, identity in changed:
                session.text_identities[source.key] = identity
                session.text_inspections[source.key] = inspections[source.key]
                for page in range(1, session.page_count + 1):
                    address = session.source_map.resolve(page)
                    if address.occurrence.key == source.key:
                        session.native_text.pop(page, None)
                        if page in session.native_pages:
                            session.native_text[page] = session.native_pages[page][
                                "text"
                            ]
            session.ocr_quote = None
            session.analysis_revision += 1
        raise PdfPacketSplitRouteError(409, "document text changed; refresh and retry")

    def estimate_batch_ocr(
        self,
        project_id: str,
        split_id: str,
        body: PdfPacketBatchOcrEstimateRequest,
        *,
        request_context: Any = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        from frisket.server.services.pdf_packet_ocr import PdfPacketOcrCoordinator

        session = self._session(project_id, split_id, ready=True)
        with self._admit_session(session, ready=True):
            self._sync_jobs(session)
            self._refresh_text_sources(session)
            with self._lock:
                if body.expected_analysis_revision != session.analysis_revision:
                    raise PdfPacketSplitRouteError(
                        409, "packet analysis changed; refresh and retry"
                    )
                revision = session.analysis_revision
                page_text = {}
                accepted = set()
                for page, value in session.native_pages.items():
                    address = session.source_map.resolve(page)
                    page_text[(address.occurrence.key, address.local_page)] = value[
                        "text"
                    ]
                for source in session.sources:
                    if source.prepared_ref_id is not None:
                        project = self._workspace.get(project_id)
                        for local_page in source.prepared_pages:
                            sample = read_prepared_page(
                                project, source.prepared_ref_id, local_page
                            )
                            if sample is not None:
                                page_text[(source.key, local_page)] = sample.text
                                if sample.completed_ocr:
                                    accepted.add((source.key, local_page))
                for pages in session.ocr.values():
                    for page, value in pages.items():
                        address = session.source_map.resolve(page)
                        key = (address.occurrence.key, address.local_page)
                        accepted.add(key)
                        page_text[key] = str(value.get("text") or "")
                manual_sources = {
                    key
                    for key, item in session.text_inspections.items()
                    if item.manual_text is not None
                }
            coordinator = PdfPacketOcrCoordinator(
                self._workspace,
                preview_runs=self._preview_runs,
                inventory_path=self._ocr_inventory(session),
            )
            holder = coordinator.quote(
                project_id=project_id,
                sources=session.sources,
                revision=revision,
                ocr_policy=body.ocr_policy,
                ocr_selection=body.ocr_selection.model_dump(mode="json"),
                page_text=page_text,
                accepted_ocr_pages=accepted,
                manual_text_sources=manual_sources,
                request_context=request_context,
                should_cancel=lambda: (
                    session.close_event.is_set()
                    or (cancel_event is not None and cancel_event.is_set())
                ),
            )
            with self._lock:
                if session.analysis_revision != revision:
                    raise PdfPacketSplitRouteError(
                        409, "packet analysis changed; refresh and retry"
                    )
                session.ocr_quote = holder
            return {
                "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
                "split_id": split_id,
                "analysis_revision": revision,
                "pages": holder.public["pages"],
                "estimate": holder.public.get("estimate"),
            }

    def start_ocr(
        self,
        project_id: str,
        split_id: str,
        body: PdfPacketOcrJobRequest,
        *,
        request_context: Any = None,
    ) -> dict[str, Any]:
        session = self._session(project_id, split_id, ready=True)
        return self._start_source_sample(
            session, self._sample_request(body), request_context=request_context
        )

    def _start_source_sample(
        self,
        session: _PacketSession,
        body: PdfPacketOcrJobRequest,
        *,
        request_context: Any = None,
    ) -> dict[str, Any]:
        from frisket.server.services.pdf_packet_ocr import (
            PdfPacketOcrCoordinator,
            packet_ocr_params,
        )

        if body.scope != "sample" or body.pages is None or len(body.pages) != 1:
            raise PdfPacketSplitRouteError(422, "choose one page for an OCR sample")
        with self._admit_session(session, ready=True):
            self._sync_jobs(session)
            self._refresh_text_sources(session)
            page = body.pages[0]
            self._validate_pages(session, [page])
            address = session.source_map.resolve(page)
            source = next(
                item for item in session.sources if item.key == address.occurrence.key
            )
            params = packet_ocr_params(body.ocr_selection.model_dump(mode="json"))
            self._refuse_running_ocr(session, params["engine"])
            saved = session.sample_quote
            if (
                saved is None
                or saved[2] != page
                or saved[4] != session.analysis_revision
                or saved[5] != body.force_rescan
                or dict(saved[1].ocr_action_params) != params
            ):
                raise PdfPacketSplitRouteError(
                    409, "estimate this OCR sample before starting"
                )
            coordinator = PdfPacketOcrCoordinator(
                self._workspace,
                preview_runs=self._preview_runs,
                inventory_path=self._ocr_inventory(session, source),
            )
            try:
                started = coordinator.start_sample(
                    project_id=session.project_id,
                    sources=session.sources,
                    source_key=source.key,
                    page=address.local_page,
                    revision=session.analysis_revision,
                    ocr_selection=body.ocr_selection.model_dump(mode="json"),
                    force_rescan=body.force_rescan,
                    confirmation=body.confirmation,
                    request_context=request_context,
                )
            except ValueError as exc:
                raise PdfPacketSplitRouteError(409, str(exc)) from exc
            if not started["jobs"]:
                if not started.get("cached"):
                    raise PdfPacketSplitRouteError(
                        409, "sample OCR could not be started"
                    )
                try:
                    self._refresh_text_sources(session)
                except PdfPacketSplitRouteError as exc:
                    if str(exc) != "document text changed; refresh and retry":
                        raise
                return {
                    "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
                    "split_id": session.id,
                    "job_id": None,
                    "status": "cached",
                    "kind": "ocr_sample",
                    "total": 0,
                    "receipt_id": started.get("receipt_id"),
                }
            job_id = str(started["jobs"][0]["preview_id"])
            with self._lock:
                session.jobs[job_id] = _PacketJob(
                    job_id,
                    "ocr_sample",
                    (page,),
                    engine=params["engine"],
                    page_map={address.local_page: page},
                    sample_target=started.get("target", saved[1]),
                )
                session.expires_at = self._expiry()
            job = self._registry.get(session.project_id, job_id)
            return {
                "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
                "split_id": session.id,
                "job_id": job_id,
                "kind": "ocr_sample",
                "total": 1,
                "receipt_id": job.receipt.receipt_id if job and job.receipt else None,
            }

    def _refuse_running_ocr(self, session: _PacketSession, engine: str) -> None:
        with self._lock:
            tracked_jobs = tuple(
                tracked
                for tracked in session.jobs.values()
                if tracked.kind.startswith("ocr_") and tracked.engine == engine
            )
        for tracked in tracked_jobs:
            job = self._registry.get(session.project_id, tracked.id)
            if job is not None and job.finished_at is None:
                raise PdfPacketSplitRouteError(
                    409, "OCR is already running for this engine"
                )

    def _record_cached_sample(
        self, split_id: str, engine: str, page: dict[str, Any]
    ) -> None:
        number = page.get("page")
        if type(number) is not int:
            return
        with self._lock:
            session = self._sessions.get(split_id)
            if session is None or not 1 <= number <= session.page_count:
                return
            session.ocr.setdefault(engine, {})[number] = {
                "text": str(page.get("text") or ""),
                "blocks": list(page.get("blocks") or []),
                "width": page.get("width"),
                "height": page.get("height"),
            }
            session.analysis_revision += 1

    def cancel_job(self, project_id: str, split_id: str, job_id: str) -> dict[str, Any]:
        session = self._session(project_id, split_id)
        if job_id not in session.jobs:
            raise PdfPacketSplitRouteError(404, "packet job was not found")
        self._registry.cancel(project_id, job_id)
        return self.status(project_id, split_id)

    def select_text_source(
        self, project_id: str, split_id: str, body: PdfPacketTextSourceRequest
    ) -> dict[str, Any]:
        session = self._session(project_id, split_id, ready=True)
        self._sync_jobs(session)
        if body.kind == "ocr" and not session.ocr.get(body.engine or ""):
            raise PdfPacketSplitRouteError(
                409, "selected OCR engine has no completed pages"
            )
        with self._lock:
            session.text_source = body.kind
            session.ocr_engine = body.engine
            session.analysis_revision += 1
        return self._snapshot(session)

    def candidates(
        self, project_id: str, split_id: str, body: PdfPacketCandidatesRequest
    ) -> dict[str, Any]:
        session = self._session(project_id, split_id, ready=True)
        self._sync_jobs(session)
        if session.text_source == "unconfirmed":
            raise PdfPacketSplitRouteError(
                409, "choose native text or OCR before matching"
            )
        self._validate_labels(session, body.confirmed_starts, body.rejected)
        examples = body.example_pages
        if examples is None and len(session.sources) > 1:
            raise PdfPacketSplitRouteError(
                422, "multi-source matching requires explicit examples"
            )
        if examples is not None:
            if len(examples) != len(set(examples)):
                raise PdfPacketSplitRouteError(422, "examples must be unique")
            if examples:
                self._validate_pages(session, examples)
            if not set(examples) <= set(body.confirmed_starts) | set(
                session.source_map.fixed_starts
            ):
                raise PdfPacketSplitRouteError(
                    422, "examples must be confirmed or fixed starts"
                )
        with self._lock:
            if body.expected_analysis_revision != session.analysis_revision:
                raise PdfPacketSplitRouteError(
                    409, "packet analysis changed; refresh and retry"
                )
            revision = session.analysis_revision
            signatures = dict(session.signatures)
            texts = dict(session.native_text)
            for ocr_pages in session.ocr.values():
                for page, value in ocr_pages.items():
                    texts[page] = str(value.get("text") or "")
        phrase_ids: dict[str, str] = {}
        for phrase in body.phrases:
            key = phrase.text.casefold().strip()
            if key in phrase_ids:
                raise PdfPacketSplitRouteError(422, "phrase texts must be unique")
            phrase_ids[key] = phrase.id
        matched = match_packet_pages(
            page_count=session.page_count,
            signatures=signatures,
            confirmed=body.confirmed_starts,
            rejected=body.rejected,
            examples=examples,
            fixed_starts=session.source_map.fixed_starts,
            threshold=body.threshold_pct,
            ocr_text=texts,
            phrases=[
                PhraseRule(p.text, enabled=p.enabled, fuzzy=p.fuzzy)
                for p in body.phrases
            ],
        )
        pages = []
        counts = {phrase.id: 0 for phrase in body.phrases}
        for page in matched.pages:
            ids = [phrase_ids[text.casefold().strip()] for text in page.matched_phrases]
            for phrase_id in ids:
                counts[phrase_id] += 1
            pages.append(
                {
                    "page": page.page,
                    "visual_score": page.visual_score,
                    "closest_confirmed_page": page.closest_confirmed_page,
                    "kind_id": page.kind_id,
                    "matched_phrase_ids": ids,
                    "suggested": page.suggested,
                }
            )
        return {
            "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
            "split_id": split_id,
            "analysis_revision": revision,
            "clusters": [
                {"id": kind.id, "confirmed_pages": list(kind.confirmed_pages)}
                for kind in matched.kinds
            ],
            "pages": pages,
            "suggested_pages": list(matched.suggested_pages),
            "unsure_pages": list(matched.unsure_pages),
            "phrase_counts": counts,
            "accept_all_scope": "packet",
        }

    def commit(
        self,
        project_id: str,
        split_id: str,
        body: PdfPacketCommitRequest,
        *,
        request_context: Any = None,
        admission_lease: ImportAdmissionLease | None = None,
    ) -> dict[str, Any]:
        del admission_lease  # Durable publication owns its own worker admission.
        session = self._session(project_id, split_id)
        request_fingerprint = hashlib.sha256(
            body.model_dump_json().encode("utf-8")
        ).hexdigest()
        with self._admit_session(session):
            existing = session.commit_keys.get(body.idempotency_key)
            if existing is not None:
                if existing[0] != request_fingerprint:
                    raise PdfPacketSplitRouteError(
                        409, "idempotency key was already used for another commit"
                    )
                return dict(existing[1])
            if session.status != "ready":
                raise PdfPacketSplitRouteError(409, "packet preparation is not ready")
            self._sync_jobs(session)
            for tracked in session.jobs.values():
                if tracked.kind.startswith("ocr_"):
                    job = self._registry.get(project_id, tracked.id)
                    if job is not None and job.finished_at is None:
                        raise PdfPacketSplitRouteError(
                            409, "finish or cancel the OCR sample before import"
                        )
            self._refresh_text_sources(session)
            if session.organization == "split_documents":
                if (
                    body.analysis_revision is None
                    or body.analysis_revision != session.analysis_revision
                ):
                    raise PdfPacketSplitRouteError(
                        409, "packet analysis changed; refresh and retry"
                    )
                self._validate_labels(session, body.confirmed_starts, [])
                ranges = session.source_map.ranges(body.confirmed_starts)
            else:
                if (
                    body.analysis_revision is not None
                    and body.analysis_revision != session.analysis_revision
                ):
                    raise PdfPacketSplitRouteError(
                        409, "packet analysis changed; refresh and retry"
                    )
                if body.confirmed_starts:
                    raise PdfPacketSplitRouteError(
                        422, "page organization does not accept document starts"
                    )
                ranges = session.source_map.page_ranges()
            local_indices: dict[str, int] = {}
            for item in ranges:
                key = item.occurrence.key
                local_indices[key] = local_indices.get(key, 0) + 1
                source = next(value for value in session.sources if value.key == key)
                self._child_name(
                    body.name_pattern,
                    source.filename,
                    local_indices[key],
                    item.start,
                    item.end,
                )
            cached_pages: dict[str, dict[int, dict[str, Any]]] = {}
            for page, value in session.native_pages.items():
                address = session.source_map.resolve(page)
                cached_pages.setdefault(address.occurrence.key, {})[
                    address.local_page
                ] = dict(value)
            for engine, pages in session.ocr.items():
                for page, value in pages.items():
                    address = session.source_map.resolve(page)
                    cached_pages.setdefault(address.occurrence.key, {})[
                        address.local_page
                    ] = {**value, "engine": engine}
            preparation = None
            native_pages = {}
            if body.ocr_policy != "none":
                from frisket.server.services.pdf_packet_ocr import (
                    PdfPacketOcrCoordinator,
                )

                if session.ocr_quote is None or body.ocr_selection is None:
                    raise PdfPacketSplitRouteError(409, "quote OCR before import")
                coordinator = PdfPacketOcrCoordinator(
                    self._workspace,
                    preview_runs=self._preview_runs,
                    inventory_path=self._ocr_inventory(session),
                )
                try:
                    preparation = coordinator.approve(
                        session.ocr_quote,
                        project_id=project_id,
                        sources=session.sources,
                        revision=session.analysis_revision,
                        ocr_policy=body.ocr_policy,
                        ocr_selection=body.ocr_selection.model_dump(mode="json"),
                        confirmation=body.ocr_confirmation,
                    )
                except ValueError as exc:
                    raise PdfPacketSplitRouteError(409, str(exc)) from exc
                native_pages = session.ocr_quote.native_files
            try:
                committed = self._committer.start(
                    project_id,
                    sources=session.sources,
                    source_map=session.source_map,
                    ranges=ranges,
                    destination=body.destination,
                    carried_column_ids=body.carried_column_ids,
                    name_pattern=body.name_pattern,
                    organization=session.organization,
                    idempotency_key=body.idempotency_key,
                    request_context=request_context,
                    cached_pages=cached_pages,
                    pdf_preparation=preparation,
                    native_pages=native_pages,
                )
            except ValueError as exc:
                message = str(exc)
                status = (
                    409
                    if any(
                        word in message.lower()
                        for word in ("changed", "stale", "idempotency")
                    )
                    else 422
                )
                raise PdfPacketSplitRouteError(status, message) from exc
            payload = {
                "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
                "split_id": split_id,
                "job_id": committed["job_id"],
                "kind": "commit",
                "total": len(ranges),
                "receipt_id": committed.get("receipt_id"),
                "import_ref": committed["import_ref"],
                "sheet_id": committed["sheet_id"],
                "source_count": committed["source_count"],
            }
            with self._lock:
                session.status = "committing"
                session.commit_result = dict(committed)
                session.commit_keys[body.idempotency_key] = (
                    request_fingerprint,
                    payload,
                )
            return dict(payload)

    def _start_background_job(
        self,
        admission_lease: ImportAdmissionLease | None,
        starter: Callable[[Callable[[], None] | None], PreviewJob],
    ) -> PreviewJob:
        if admission_lease is None:
            return starter(None)
        return admission_lease.start_background(starter)

    @staticmethod
    def _child_name(pattern: str, packet: str, index: int, start: int, end: int) -> str:
        try:
            return child_filename(pattern, packet, index, start, end)
        except ValueError as exc:
            raise PdfPacketSplitRouteError(422, str(exc)) from exc

    def close(self, project_id: str, split_id: str) -> None:
        session = self._session(project_id, split_id)
        session.close_event.set()
        detached = self._detach_session(session)
        if detached is not None:
            self._cleanup_sessions([detached])

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        detached = []
        for session in sessions:
            session.close_event.set()
            removed = self._detach_session(session)
            if removed is not None:
                detached.append(removed)
        self._cleanup_sessions(detached)

    def _reap_expired_sessions(self) -> None:
        now = self._clock()
        with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if now >= session.expires_at
            ]
        detached = []
        for session in sessions:
            removed = self._detach_session(session, expired_at=now)
            if removed is not None:
                detached.append(removed)
        self._cleanup_sessions(detached)

    @contextlib.contextmanager
    def _admit_session(
        self, session: _PacketSession, *, ready: bool = False
    ) -> Iterator[None]:
        with session.admission_lock:
            with self._lock:
                if self._sessions.get(session.id) is not session:
                    raise PdfPacketSplitRouteError(404, "packet split was not found")
                if ready and session.status != "ready":
                    raise PdfPacketSplitRouteError(
                        409, "packet preparation is not ready"
                    )
            yield

    def _detach_session(
        self,
        session: _PacketSession,
        *,
        expired_at: datetime | None = None,
    ) -> _PacketSession | None:
        with session.admission_lock:
            with self._lock:
                if self._sessions.get(session.id) is not session:
                    return None
                if expired_at is None:
                    return self._sessions.pop(session.id)
                if expired_at < session.expires_at:
                    return None
                job_ids = tuple(session.jobs)
            if any(
                job is not None and job.finished_at is None
                for job_id in job_ids
                if (job := self._registry.get(session.project_id, job_id)) is not None
            ):
                with self._lock:
                    if self._sessions.get(session.id) is session:
                        session.expires_at = max(session.expires_at, self._expiry())
                return None
            with self._lock:
                if self._sessions.get(session.id) is not session:
                    return None
                if expired_at < session.expires_at:
                    return None
                return self._sessions.pop(session.id)

    def _cleanup_sessions(self, sessions: list[_PacketSession]) -> None:
        for session in sessions:
            for job_id in list(session.jobs):
                self._registry.release(session.project_id, job_id)
            shutil.rmtree(session.directory, ignore_errors=True)

    def _sync_jobs(self, session: _PacketSession) -> None:
        with self._lock:
            jobs = list(session.jobs.values())
        for tracked in jobs:
            if tracked.copied or not tracked.kind.startswith("ocr_"):
                continue
            job = self._registry.get(session.project_id, tracked.id)
            if (
                job is None
                or job.status != "done"
                or not isinstance(job.result, TablePreviewResult)
            ):
                continue
            copied: dict[int, dict[str, Any]] = {}
            if tracked.sample_target is not None and tracked.page_map is not None:
                value = self._cached_sample_payload(session, tracked.sample_target)
                global_page = tracked.page_map.get(tracked.sample_target.page_number)
                if value is not None and global_page is not None:
                    copied[global_page] = value
            for row in job.result.rows:
                try:
                    page = int(row["page"]["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                if tracked.page_map is not None:
                    page = tracked.page_map.get(page)
                    if page is None:
                        continue
                text = str(row.get("text", {}).get("value") or "")
                blocks = list(row.get("blocks", {}).get("value") or [])
                errors = list(row.get("errors", {}).get("value") or [])
                if errors and not text and not blocks:
                    continue
                copied.setdefault(page, {"text": text, "blocks": blocks})
            with self._lock:
                cached = session.ocr.setdefault(tracked.engine or "", {})
                for page, value in copied.items():
                    cached[page] = {**cached.get(page, {}), **value}
                tracked.copied = True
                session.analysis_revision += 1

    def _snapshot(self, session: _PacketSession) -> dict[str, Any]:
        self._sync_jobs(session)
        with self._lock:
            project_id = session.project_id
            prepare_job_id = session.prepare_job_id
            tracked_jobs = tuple(session.jobs.values())
            text_pages_read = set(session.native_text)
            for ocr_pages in session.ocr.values():
                text_pages_read.update(ocr_pages)
            for source, offset in zip(
                session.sources, session.source_map.fixed_starts, strict=True
            ):
                text_pages_read.update(
                    offset + page - source.page_start for page in source.prepared_pages
                )
                inspection = session.text_inspections.get(source.key)
                if inspection is not None:
                    text_pages_read.update(
                        offset + page - source.page_start for page in inspection.pages
                    )
            snapshot: dict[str, Any] = {
                "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
                "split_id": session.id,
                "status": session.status,
                "organization": session.organization,
                "fixed_starts": list(session.source_map.fixed_starts),
                "example_pages": list(session.example_pages),
                "sheet_scope": session.sheet_scope,
                "text_categories": {
                    category: sum(
                        1
                        for item in session.text_inspections.values()
                        if item.category == category
                    )
                    for category in ("text", "missing", "suspicious", "unreadable")
                },
                "sources": [
                    {
                        "key": source.key,
                        "blob_hash": source.sha256,
                        "filename": source.filename,
                        "size": source.size,
                        "page_count": source.page_count,
                        "page_start": source.page_start,
                        "page_end": source.page_end,
                        "display_start": offset,
                        "source_url": source.source_url,
                        "text_category": session.text_inspections[source.key].category
                        if source.key in session.text_inspections
                        else None,
                        "manual_text_preview": session.text_inspections[
                            source.key
                        ].manual_text
                        if source.key in session.text_inspections
                        else None,
                        "source_sheet_id": source.source_sheet_id,
                        "source_row_id": source.source_row_id,
                        "source_column_id": source.source_column_id,
                        "prepared_page_count": len(source.prepared_pages),
                        "has_prepared": source.prepared_ref_id is not None,
                        "prepared_complete": source.prepared_ref_id is not None
                        and len(source.prepared_pages)
                        == source.page_end - source.page_start + 1,
                    }
                    for source, offset in zip(
                        session.sources, session.source_map.fixed_starts, strict=True
                    )
                ],
                "packet": {
                    "blob_hash": session.source_hash,
                    "filename": session.filename,
                    "mime": "application/pdf",
                    "size": session.size,
                    "page_count": session.page_count,
                },
                "prepare": {
                    "job_id": prepare_job_id,
                    "pages_ready": sorted(session.pages_ready),
                    "native_text_pages": sorted(
                        page for page, text in session.native_text.items() if text
                    ),
                    "text_pages_read": sorted(text_pages_read),
                    "visual_pages_ready": len(session.signatures),
                },
                "text_source": session.text_source,
                "ocr_engine": session.ocr_engine,
                "ocr_pages": sorted(
                    session.ocr.get(session.ocr_engine, {})
                    if session.ocr_engine is not None
                    else {page for pages in session.ocr.values() for page in pages}
                ),
                "analysis_revision": session.analysis_revision,
                "expires_at": session.expires_at.isoformat().replace("+00:00", "Z"),
                "commit_result": (
                    dict(session.commit_result)
                    if session.commit_result is not None
                    else None
                ),
            }
        prepare = self._registry.get(project_id, prepare_job_id)
        jobs = []
        for tracked in tracked_jobs:
            if tracked.kind == "prepare":
                continue
            job = self._registry.get(project_id, tracked.id)
            if job is not None:
                jobs.append(self._job_state(tracked, job))
        snapshot["prepare"]["progress"] = self._progress(prepare)
        snapshot["jobs"] = jobs
        return snapshot

    def _job_state(self, tracked: _PacketJob, job: PreviewJob) -> dict[str, Any]:
        receipt = job.receipt
        accounting = receipt.model_dump(mode="json") if receipt is not None else None
        return {
            "job_id": job.id,
            "kind": tracked.kind,
            "progress": self._progress(job),
            "engine": tracked.engine,
            "pages": list(tracked.pages),
            "receipt_id": receipt.receipt_id if receipt is not None else None,
            "accounting": accounting,
        }

    @staticmethod
    def _progress(job: PreviewJob | None) -> dict[str, Any]:
        if job is None:
            return {"status": "error", "done": 0, "total": None, "error": "job expired"}
        error = None
        if job.error:
            error = str(
                job.error.get("message") or job.error.get("code") or "job failed"
            )
        return {
            "status": job.status,
            "done": int(job.progress.get("done") or 0),
            "total": job.progress.get("total"),
            "error": error,
        }

    def _start_payload(self, session, job, kind, total):
        if job is None:
            raise PdfPacketSplitRouteError(404, "packet job expired")
        return {
            "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
            "split_id": session.id,
            "job_id": job.id,
            "kind": kind,
            "total": total,
            "receipt_id": job.receipt.receipt_id if job.receipt else None,
        }

    def _session(
        self, project_id: str, split_id: str, *, ready: bool = False
    ) -> _PacketSession:
        session = self._require(split_id=split_id)
        if session.project_id != project_id:
            raise PdfPacketSplitRouteError(404, "packet split was not found")
        now = self._clock()
        if now >= session.expires_at:
            detached = self._detach_session(session, expired_at=now)
            if detached is not None:
                self._cleanup_sessions([detached])
                raise PdfPacketSplitRouteError(404, "packet split expired")
            with self._lock:
                if self._sessions.get(split_id) is not session:
                    raise PdfPacketSplitRouteError(404, "packet split was not found")
        with self._lock:
            if self._sessions.get(split_id) is not session:
                raise PdfPacketSplitRouteError(404, "packet split was not found")
            if ready and session.status != "ready":
                raise PdfPacketSplitRouteError(409, "packet preparation is not ready")
            session.expires_at = self._expiry()
            return session

    def _require(self, *, split_id: str) -> _PacketSession:
        with self._lock:
            session = self._sessions.get(split_id)
        if session is None:
            raise PdfPacketSplitRouteError(404, "packet split was not found")
        return session

    @staticmethod
    def _validate_pages(session: _PacketSession, pages: list[int]) -> None:
        if not pages or any(
            type(page) is not int or not 1 <= page <= session.page_count
            for page in pages
        ):
            raise PdfPacketSplitRouteError(422, "page selection is out of range")

    def _validate_labels(self, session, confirmed, rejected) -> None:
        if confirmed:
            self._validate_pages(session, list(confirmed))
        if len(session.sources) == 1 and 1 not in confirmed:
            raise PdfPacketSplitRouteError(422, "page 1 must be a confirmed start")
        if len(confirmed) != len(set(confirmed)) or len(rejected) != len(set(rejected)):
            raise PdfPacketSplitRouteError(422, "page labels must be unique")
        if rejected:
            self._validate_pages(session, list(rejected))
        if (set(confirmed) | set(session.source_map.fixed_starts)) & set(rejected):
            raise PdfPacketSplitRouteError(
                422, "confirmed and rejected pages must be disjoint"
            )

    def _expiry(self) -> datetime:
        return self._clock() + timedelta(seconds=self._ttl_seconds)


__all__ = ["PdfPacketSplitRouteError", "PdfPacketSplitService"]

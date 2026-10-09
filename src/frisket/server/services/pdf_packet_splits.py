"""Process-local orchestration for guided PDF packet splitting.

The uploaded source is copied once into the project's content-addressed blob
store. Analysis files and job handles deliberately remain process-local: an
expired or restarted session is recreated by uploading again, rather than by
maintaining a second durable import inventory.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

from PIL import Image
from pypdf import PdfReader

from frisket.contracts.http.pdf_packet_splits import (
    PDF_PACKET_SPLIT_SCHEMA_VERSION,
    PdfPacketCandidatesRequest,
    PdfPacketCommitRequest,
    PdfPacketOcrEstimateRequest,
    PdfPacketOcrJobRequest,
    PdfPacketTextSourceRequest,
)
from frisket.engine.pdf_render import render_pdf_pages
from frisket.engine.pdf_text import PdfTextError, extract_pdf_text
from frisket.engine.executor.table_preview import TablePreviewResult
from frisket.pdf_packets import (
    PhraseRule,
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
from frisket.server.workspace import Workspace


_SESSION_TTL_SECONDS = 60 * 60
_PREPARE_DPI = 96
_THUMBNAIL_MAX_EDGE = 1400
_MAX_PACKET_PAGES = 2000
_MAX_CHILD_FILENAME_BYTES = 255
_NAME_PATTERN_FIELDS = frozenset({"packet", "index", "start", "end", "page"})
_NAME_PATTERN_FIELD_RE = re.compile(r"\{([^{}]+)\}")


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
    prepare_job_id: str = ""
    status: str = "preparing"
    pages_ready: set[int] = field(default_factory=set)
    native_text: dict[int, str] = field(default_factory=dict)
    signatures: dict[int, int] = field(default_factory=dict)
    jobs: dict[str, _PacketJob] = field(default_factory=dict)
    ocr: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)
    text_source: str = "unconfirmed"
    ocr_engine: str | None = None
    analysis_revision: int = 0
    commit_result: dict[str, Any] | None = None
    commit_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    admission_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


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
    ) -> None:
        self._workspace = workspace
        self._registry = registry
        self._preview_runs = preview_runs
        self._committer = PdfPacketCommitter(workspace)
        self._page_signature = page_signature or _page_signature
        self._ttl_seconds = ttl_seconds
        self._clock = clock or _utc_now
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
        session_id = uuid.uuid4().hex
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
            if not 1 <= page_count <= _MAX_PACKET_PAGES:
                raise PdfPacketSplitRouteError(
                    422, f"PDF must contain 1 to {_MAX_PACKET_PAGES} pages"
                )
            project = self._workspace.get(project_id)
            source_hash = project.add_blob_from_path(
                source_path,
                filename=upload.filename,
                mime="application/pdf",
                expected_digest=upload.sha256,
            )
            session = _PacketSession(
                id=session_id,
                project_id=project_id,
                source_hash=source_hash,
                filename=upload.filename,
                size=size,
                directory=directory,
                source_path=source_path,
                page_count=page_count,
                expires_at=self._expiry(),
            )
            with self._lock:
                self._sessions[session_id] = session
            with self._admit_session(session):
                job = self._start_background_job(
                    admission_lease,
                    lambda on_finished: self._registry.start(
                        project_id,
                        page_count,
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
            try:
                positioned = asyncio.run(
                    extract_pdf_text(
                        session.source_path,
                        should_cancel=cancelled.is_set,
                        max_pages=_MAX_PACKET_PAGES,
                    )
                )
            except PdfTextError:
                positioned = []
            native = {
                page.page: " ".join(token.text for token in page.tokens).strip()
                for page in positioned
            }
            with self._lock:
                session.native_text = native

            sample = self._sample_pages(session.page_count)
            remaining = [
                page for page in range(1, session.page_count + 1) if page not in sample
            ]
            for pages in (sample, remaining):
                if not pages:
                    continue
                rendered = asyncio.run(
                    render_pdf_pages(
                        session.source_path,
                        thumbs,
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
                    for (page, _path), signature in zip(
                        rendered.pages, signatures, strict=True
                    ):
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
        ocr = (
            session.ocr.get(session.ocr_engine or "", {}).get(page)
            if session.ocr_engine
            else None
        )
        return {
            "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
            "split_id": split_id,
            "page": page,
            "thumbnail_ready": page in session.pages_ready,
            "thumbnail_url": (
                f"/api/projects/{quote(project_id, safe='')}/import/pdf-packet-splits/"
                f"{quote(split_id, safe='')}/pages/{page}/thumbnail"
            ),
            "native_text": session.native_text.get(page),
            "ocr_text": ocr.get("text") if ocr else None,
            "ocr_blocks": list(ocr.get("blocks") or []) if ocr else [],
            "ocr_engine": session.ocr_engine if ocr else None,
        }

    def thumbnail(self, project_id: str, split_id: str, page: int) -> _ThumbnailLease:
        session = self._session(project_id, split_id)
        with self._admit_session(session):
            self._validate_pages(session, [page])
            source = session.directory / "pages" / f"page-{page}.png"
            if not source.is_file():
                raise PdfPacketSplitRouteError(409, "page thumbnail is not ready")
            fd, name = tempfile.mkstemp(prefix="frisket-packet-thumb-", suffix=".png")
            os.close(fd)
            target = Path(name)
            shutil.copyfile(source, target)
        return _ThumbnailLease(target)

    def estimate_ocr(
        self,
        project_id: str,
        split_id: str,
        body: PdfPacketOcrEstimateRequest,
        *,
        request_context: Any = None,
    ) -> dict[str, Any]:
        session = self._session(project_id, split_id, ready=True)
        with self._admit_session(session, ready=True):
            self._sync_jobs(session)
            pages, cached = self._ocr_pages(
                session, body.engine, body.scope, body.pages
            )
            if not pages:
                estimate = None
            else:
                plan, _source, _router, _composition, _context = (
                    self._preview_runs.prepare_ocr_scratch_path(
                        project_id,
                        session.source_path,
                        media_digest=session.source_hash,
                        media_size=session.size,
                        payload=self._ocr_payload(session, body.engine, pages),
                        max_pages=_MAX_PACKET_PAGES,
                        request_context=request_context,
                    )
                )
                estimate = self._preview_runs.scratch_estimate(project_id, plan)
        return {
            "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
            "engine": body.engine,
            "scope": body.scope,
            "pages": sorted((*cached, *pages)),
            "cached_pages": cached,
            "estimate": estimate,
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
        with self._admit_session(session, ready=True):
            self._sync_jobs(session)
            self._refuse_running_ocr(session, body.engine)
            pages, cached = self._ocr_pages(
                session, body.engine, body.scope, body.pages
            )
            if not pages:
                raise PdfPacketSplitRouteError(
                    409, "all requested pages already have OCR"
                )
            plan, _source, router, composition, execution_context = (
                self._preview_runs.prepare_ocr_scratch_path(
                    project_id,
                    session.source_path,
                    media_digest=session.source_hash,
                    media_size=session.size,
                    payload=self._ocr_payload(session, body.engine, pages),
                    on_page=lambda page: self._cache_ocr_page(
                        split_id, body.engine, page
                    ),
                    max_pages=_MAX_PACKET_PAGES,
                    request_context=request_context,
                )
            )
            started = self._preview_runs.start_scratch_preview(
                project_id,
                plan,
                confirmation=body.confirmation,
                router=router,
                composition=composition,
                execution_context=execution_context,
                replacement_key=f"pdf-packet:{split_id}:ocr:{body.engine}",
                total=len(pages),
            )
            if started.status_code != 202:
                raise PdfPacketSplitRouteError(
                    started.status_code, started.payload, bare_json=True
                )
            job_id = str(started.payload["preview_id"])
            kind = "ocr_sample" if body.scope == "sample" else "ocr_full"
            with self._lock:
                session.jobs[job_id] = _PacketJob(
                    job_id, kind, tuple(pages), engine=body.engine
                )
                session.expires_at = self._expiry()
            job = self._registry.get(project_id, job_id)
        return {
            "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
            "split_id": split_id,
            "job_id": job_id,
            "kind": kind,
            "total": len(pages),
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

    def _cache_ocr_page(self, split_id: str, engine: str, page: dict[str, Any]) -> None:
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
            }
            session.analysis_revision += 1

    @staticmethod
    def _ocr_payload(
        session: _PacketSession, engine: str, pages: list[int]
    ) -> dict[str, Any]:
        return {
            "engine": engine,
            "pages": pages,
            "filename": session.filename,
            "mime": "application/pdf",
            "searchable_pdf": False,
        }

    def _ocr_pages(
        self,
        session: _PacketSession,
        engine: str,
        scope: str,
        requested: list[int] | None,
    ) -> tuple[list[int], list[int]]:
        pages = (
            list(range(1, session.page_count + 1))
            if scope == "all"
            else list(requested or [])
        )
        self._validate_pages(session, pages)
        pages = sorted(set(pages))
        existing = session.ocr.get(engine, {})
        cached = [page for page in pages if page in existing]
        return [page for page in pages if page not in existing], cached

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
        texts = (
            session.native_text
            if session.text_source == "native"
            else {
                page: str(value.get("text") or "")
                for page, value in session.ocr.get(session.ocr_engine or "", {}).items()
            }
        )
        phrase_ids: dict[str, str] = {}
        for phrase in body.phrases:
            key = phrase.text.casefold().strip()
            if key in phrase_ids:
                raise PdfPacketSplitRouteError(422, "phrase texts must be unique")
            phrase_ids[key] = phrase.id
        matched = match_packet_pages(
            page_count=session.page_count,
            signatures=session.signatures,
            confirmed=body.confirmed_starts,
            rejected=body.rejected,
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
            "analysis_revision": session.analysis_revision,
            "clusters": [
                {"id": kind.id, "confirmed_pages": list(kind.confirmed_pages)}
                for kind in matched.kinds
            ],
            "pages": pages,
            "suggested_pages": list(matched.suggested_pages),
            "question_pages": list(matched.question_pages),
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
        session = self._session(project_id, split_id)
        request_fingerprint = hashlib.sha256(
            body.model_dump_json().encode("utf-8")
        ).hexdigest()
        with self._admit_session(session):
            existing = session.commit_keys.get(body.idempotency_key)
            if existing:
                job_id, existing_fingerprint = existing
                if existing_fingerprint != request_fingerprint:
                    raise PdfPacketSplitRouteError(
                        409, "idempotency key was already used for another commit"
                    )
                job = self._registry.get(project_id, job_id)
                return self._start_payload(
                    session, job, "commit", len(body.confirmed_starts)
                )
            if session.status != "ready":
                raise PdfPacketSplitRouteError(409, "packet preparation is not ready")
            self._validate_labels(session, body.confirmed_starts, [])
            starts = sorted(set(body.confirmed_starts))
            ranges = [
                (
                    start,
                    starts[index + 1] - 1
                    if index + 1 < len(starts)
                    else session.page_count,
                )
                for index, start in enumerate(starts)
            ]
            for index, (start, end) in enumerate(ranges, 1):
                self._child_name(body.name_pattern, session.filename, index, start, end)
            if body.keep_ocr_text:
                if session.text_source != "ocr" or session.ocr_engine is None:
                    raise PdfPacketSplitRouteError(
                        409, "keeping OCR requires selected OCR text"
                    )
                missing = [
                    page
                    for page in range(1, session.page_count + 1)
                    if page not in session.ocr.get(session.ocr_engine, {})
                ]
                if missing:
                    raise PdfPacketSplitRouteError(
                        409, "run OCR on every page before keeping OCR text"
                    )
            with self._lock:
                if session.status != "ready":
                    message = (
                        "packet commit is already running"
                        if session.status == "committing"
                        else "packet preparation is not ready"
                    )
                    raise PdfPacketSplitRouteError(409, message)
                session.status = "committing"
            try:
                job = self._start_background_job(
                    admission_lease,
                    lambda on_finished: self._registry.start(
                        project_id,
                        len(ranges),
                        lambda progress, cancelled: self._commit_run(
                            session,
                            body,
                            ranges,
                            request_context=request_context,
                            progress=progress,
                            cancelled=cancelled,
                        ),
                        replacement_key=f"pdf-packet:{split_id}:commit",
                        on_finished=on_finished,
                    ),
                )
            except BaseException:
                with self._lock:
                    session.status = "ready"
                raise
            with self._lock:
                session.jobs[job.id] = _PacketJob(job.id, "commit")
                session.commit_keys[body.idempotency_key] = (
                    job.id,
                    request_fingerprint,
                )
            return self._start_payload(session, job, "commit", len(ranges))

    def _start_background_job(
        self,
        admission_lease: ImportAdmissionLease | None,
        starter: Callable[[Callable[[], None] | None], PreviewJob],
    ) -> PreviewJob:
        if admission_lease is None:
            return starter(None)
        return admission_lease.start_background(starter)

    def _commit_run(
        self,
        session,
        body,
        ranges,
        *,
        request_context,
        progress,
        cancelled,
    ):
        try:
            committed = self._committer.commit(
                session.project_id,
                source_path=session.source_path,
                source_filename=session.filename,
                source_page_count=session.page_count,
                ranges=ranges,
                names=[
                    self._child_name(
                        body.name_pattern,
                        session.filename,
                        index,
                        start,
                        end,
                    )
                    for index, (start, end) in enumerate(ranges, 1)
                ],
                ocr_pages=(
                    session.ocr[session.ocr_engine]
                    if body.keep_ocr_text and session.ocr_engine is not None
                    else None
                ),
                ocr_engine=session.ocr_engine if body.keep_ocr_text else None,
                sheet_name=body.destination.name,
                idempotency_key=(
                    f"pdf-packet-split:{session.id}:{body.idempotency_key}"
                ),
                request_context=request_context,
                progress=progress,
                cancelled=cancelled.is_set,
                seal_cancellation=cancelled.seal,
            )
            with self._lock:
                session.commit_result = committed
                session.status = "completed"
            return committed
        except BaseException:
            with self._lock:
                if self._sessions.get(session.id) is session:
                    session.status = "ready"
            raise

    @staticmethod
    def _child_name(pattern: str, packet: str, index: int, start: int, end: int) -> str:
        stem = re.sub(r"\.pdf$", "", packet, flags=re.IGNORECASE)
        fields = {
            "packet": stem,
            "index": str(index),
            "start": str(start),
            "end": str(end),
            "page": str(start),
        }
        unknown = next(
            (
                match.group(1)
                for match in _NAME_PATTERN_FIELD_RE.finditer(pattern)
                if match.group(1) not in _NAME_PATTERN_FIELDS
            ),
            None,
        )
        if unknown is not None:
            raise PdfPacketSplitRouteError(422, "name pattern uses an unknown field")
        without_fields = _NAME_PATTERN_FIELD_RE.sub("", pattern)
        if "{" in without_fields or "}" in without_fields:
            raise PdfPacketSplitRouteError(422, "name pattern has invalid braces")
        name = _NAME_PATTERN_FIELD_RE.sub(
            lambda match: fields[match.group(1)], pattern
        ).strip()
        if not name:
            raise PdfPacketSplitRouteError(
                422, "name pattern produced an empty filename"
            )
        filename = name if name.lower().endswith(".pdf") else f"{name}.pdf"
        if len(filename.encode("utf-8")) > _MAX_CHILD_FILENAME_BYTES:
            raise PdfPacketSplitRouteError(
                422, "generated filenames must be 255 bytes or fewer"
            )
        return filename

    def close(self, project_id: str, split_id: str) -> None:
        session = self._session(project_id, split_id)
        detached = self._detach_session(session)
        if detached is not None:
            self._cleanup_sessions([detached])

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        detached = []
        for session in sessions:
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
            for row in job.result.rows:
                try:
                    page = int(row["page"]["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                text = str(row.get("text", {}).get("value") or "")
                blocks = list(row.get("blocks", {}).get("value") or [])
                errors = list(row.get("errors", {}).get("value") or [])
                if errors and not text and not blocks:
                    continue
                copied[page] = {
                    "text": text,
                    "blocks": blocks,
                }
            with self._lock:
                session.ocr.setdefault(tracked.engine or "", {}).update(copied)
                tracked.copied = True
                session.analysis_revision += 1

    def _snapshot(self, session: _PacketSession) -> dict[str, Any]:
        self._sync_jobs(session)
        with self._lock:
            project_id = session.project_id
            prepare_job_id = session.prepare_job_id
            tracked_jobs = tuple(session.jobs.values())
            snapshot: dict[str, Any] = {
                "schema_version": PDF_PACKET_SPLIT_SCHEMA_VERSION,
                "split_id": session.id,
                "status": session.status,
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
                    "visual_pages_ready": len(session.signatures),
                },
                "text_source": session.text_source,
                "ocr_engine": session.ocr_engine,
                "ocr_pages": sorted(
                    session.ocr.get(session.ocr_engine, {})
                    if session.ocr_engine is not None
                    else ()
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
        self._validate_pages(session, list(confirmed))
        if 1 not in confirmed:
            raise PdfPacketSplitRouteError(422, "page 1 must be a confirmed start")
        if len(confirmed) != len(set(confirmed)) or len(rejected) != len(set(rejected)):
            raise PdfPacketSplitRouteError(422, "page labels must be unique")
        if rejected:
            self._validate_pages(session, list(rejected))
        if set(confirmed) & set(rejected):
            raise PdfPacketSplitRouteError(
                422, "confirmed and rejected pages must be disjoint"
            )

    def _expiry(self) -> datetime:
        return self._clock() + timedelta(seconds=self._ttl_seconds)


__all__ = ["PdfPacketSplitRouteError", "PdfPacketSplitService"]

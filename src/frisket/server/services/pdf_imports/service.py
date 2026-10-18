"""Durable project-external intake for two-or-more-PDF imports."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import secrets
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from filelock import FileLock

from frisket.contracts.http.pdf_imports import PdfImportStatus
from frisket.engine.store.disk_capacity import DISK_HEADROOM_BYTES
from frisket.engine.store.import_inventory import ImportInventory
from frisket.server.services.import_bulk_sources import fsync_directory
from frisket.server.services.import_uploads import AdmittedUpload

from .analysis import analyze_pdf
from .inventory import PdfDraftInventory
from .previews import (
    ThumbnailLease,
    prepare_ocr,
    preview_settings,
    read_native_page,
    thumbnail as render_thumbnail,
)
from .storage import PdfDraftStore, PdfImportDraftError, SCHEMA_VERSION, now


MAX_MANIFEST_BATCH = 256
MAX_UPLOAD_BATCH = 64
COPY_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class FrozenPdfOccurrence:
    ordinal: int
    selected_ordinal: int
    inventory_ordinal: int
    occurrence_id: str
    logical_path: str
    filename: str
    size: int
    sha256: str | None
    path: Path | None
    page_count: int | None
    category: str
    matched: bool
    ocr_selected: bool


@dataclass(frozen=True)
class FrozenPdfImport:
    commit_id: str
    draft_id: str
    project_id: str
    storage_identity: str
    idempotency_key: str
    selected_through: int
    selected_total: int
    staged_through: int
    manifest_hash: str
    destination: Mapping[str, Any]
    ocr_policy: str
    ocr_engine: str
    ocr_authorization: Mapping[str, Any] | None
    semantic_search: bool
    semantic_setup_confirmation: str | None
    directory: Path
    source_files_root: Path
    commit_inventory_path: Path
    committed_total: int
    available_through: int
    uploads_complete: bool
    sealed: bool
    commit_result: Mapping[str, Any] | None

    def page_occurrences(
        self, *, after: int = 0, limit: int = 256
    ) -> list[FrozenPdfOccurrence]:
        if limit < 1 or limit > 256:
            raise ValueError("occurrence page limit must be 1..256")
        with PdfDraftInventory(self.directory / "inventory.db") as inventory:
            rows = inventory.projection_page(after=after, limit=limit)
        rows = [
            row for row in rows if int(row["commit_ordinal"]) <= self.available_through
        ]
        return [
            FrozenPdfOccurrence(
                ordinal=int(row["commit_ordinal"]),
                selected_ordinal=int(row["selected_ordinal"]),
                inventory_ordinal=int(row["staged_ordinal"]),
                occurrence_id=str(row["occurrence_id"]),
                logical_path=str(row["logical_path"]),
                filename=str(row["filename"]),
                size=int(row["size"]),
                sha256=str(row["sha256"]) if row.get("sha256") else None,
                path=(
                    self.source_files_root / str(row["sha256"])
                    if row.get("sha256")
                    else None
                ),
                page_count=row.get("page_count"),
                category=str(row["category"]),
                matched=bool(row.get("matched")),
                ocr_selected=(
                    self.ocr_policy == "all"
                    or (
                        self.ocr_policy == "missing_or_suspicious"
                        and str(row["category"]) in {"missing", "suspicious"}
                    )
                ),
            )
            for row in rows
        ]


def _logical_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.lower() != ".pdf"
    ):
        raise PdfImportDraftError(422, "multi-PDF import accepts safe PDF paths only")
    return str(path)


class PdfImportService:
    def __init__(
        self,
        workspace: Any,
        *,
        preview_runs: Any = None,
        destination_matcher: Callable[
            [Any, int, list[tuple[str, int]]], set[tuple[str, int]]
        ]
        | None = None,
        quality_assessor: Callable[..., Sequence[Any]] | None = None,
        batch_ocr_estimator: Callable[[str, Mapping[str, Any], Any], Mapping[str, Any]]
        | None = None,
        semantic_setup_resolver: Callable[[str, Any], Mapping[str, Any]] | None = None,
        on_commit_ready: Callable[[str, str, str, int], None] | None = None,
    ) -> None:
        self._workspace = workspace
        self._store = PdfDraftStore(workspace)
        self._root = self._store.root
        self._preview_runs = preview_runs
        self._destination_matcher = destination_matcher
        self._quality_assessor = quality_assessor
        self._batch_ocr_estimator = batch_ocr_estimator
        self._semantic_setup_resolver = semantic_setup_resolver
        self._on_commit_ready = on_commit_ready

    def create(
        self,
        project_id: str,
        *,
        expected_files: int,
        expected_bytes: int,
        source_label: str | None,
    ) -> dict[str, Any]:
        if type(expected_files) is not int or expected_files < 2:
            raise PdfImportDraftError(
                422, "multi-PDF import requires at least two PDFs"
            )
        if type(expected_bytes) is not int or expected_bytes < 0:
            raise PdfImportDraftError(422, "expected PDF bytes must be nonnegative")
        project = self._workspace.get(project_id)
        draft_id = f"pdf-{secrets.token_urlsafe(18)}"
        directory = self._root / draft_id
        try:
            directory.mkdir()
            (directory / "files").mkdir()
            (directory / "cache").mkdir()
            PdfDraftInventory(directory / "inventory.db").close()
            created_at = now()
            header = {
                "schema_version": SCHEMA_VERSION,
                "draft_id": draft_id,
                "project_id": project_id,
                "storage_identity": project.storage_identity,
                "state": "open",
                "paused": False,
                "revision": 1,
                "created_at": created_at,
                "updated_at": created_at,
                "expires_at": self._store.expiry(),
                "source_label": source_label,
                "expected_files": expected_files,
                "expected_bytes": expected_bytes,
                "preflight": {"state": "waiting", "free_bytes": None},
                "choices": {
                    "ocr_policy": "missing_or_suspicious",
                    "ocr_engine": "rapidocr",
                    "ocr_confirmation": None,
                    "semantic_search": True,
                    "semantic_setup_confirmation": None,
                    "destination": {
                        "kind": "new",
                        "sheet_name": source_label or "PDFs",
                    },
                },
                "match": self._empty_match(),
                "batch_ocr": self._empty_batch_ocr(),
                "semantic_setup": self._resolve_semantic_setup(project_id, None),
                "preview_jobs": [],
                "cleanup": {"bytes_done": 0, "bytes_total": 0},
                "commit": None,
            }
            self._store.write_header(directory, header)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return self._snapshot(header, self._stats(directory))

    def append_manifest(
        self,
        project_id: str,
        draft_id: str,
        *,
        batch_id: str,
        files: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if (
            not batch_id
            or len(batch_id) > 128
            or not 1 <= len(files) <= MAX_MANIFEST_BATCH
        ):
            raise PdfImportDraftError(
                422, "manifest batch must contain 1..256 files and a batch id"
            )
        items = []
        for raw in files:
            occurrence_id = str(raw.get("client_file_id") or "").strip()
            logical = _logical_path(str(raw.get("logical_path") or ""))
            filename = str(raw.get("filename") or PurePosixPath(logical).name)
            size = raw.get("size")
            if not occurrence_id or len(occurrence_id) > 128:
                raise PdfImportDraftError(422, "PDF client file id is invalid")
            if type(size) is not int or size < 0:
                raise PdfImportDraftError(422, "PDF file size is invalid")
            items.append(
                {
                    "occurrence_id": occurrence_id,
                    "logical_path": logical,
                    "filename": filename,
                    "size": size,
                }
            )
        directory, header = self._open(project_id, draft_id)
        with (
            self._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self._header(project_id, directory)
            self._require_state(header, "open")
            try:
                _through, added = inventory.append_manifest_batch(
                    batch_id,
                    items,
                    expected_files=int(header["expected_files"]),
                    expected_bytes=int(header["expected_bytes"]),
                )
            except OverflowError as exc:
                raise PdfImportDraftError(413, str(exc)) from exc
            except (ValueError, RuntimeError) as exc:
                raise PdfImportDraftError(409, str(exc)) from exc
            if added:
                self._save(directory, header)
            return self._snapshot(header, inventory.stats())

    def seal_manifest(self, project_id: str, draft_id: str) -> dict[str, Any]:
        directory, _ = self._open(project_id, draft_id)
        with (
            self._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self._header(project_id, directory)
            self._require_state(header, "open")
            stats = inventory.stats()
            if (
                stats["selected_count"] != header["expected_files"]
                or stats["selected_bytes"] != header["expected_bytes"]
            ):
                raise PdfImportDraftError(
                    409, "PDF selection manifest totals are incomplete"
                )
            free = shutil.disk_usage(directory).free
            header["preflight"] = {"state": "ready", "free_bytes": free}
            if free < header["expected_bytes"] + DISK_HEADROOM_BYTES:
                header["preflight"]["state"] = "insufficient"
                self._save(directory, header)
                raise PdfImportDraftError(409, "insufficient disk space for PDF import")
            inventory.seal_manifest()
            self._save(directory, header)
            return self._snapshot(header, inventory.stats())

    def upload(
        self,
        project_id: str,
        draft_id: str,
        uploads: Sequence[AdmittedUpload],
        *,
        file_ids: Sequence[str],
        logical_paths: Sequence[str],
        batch_id: str,
    ) -> dict[str, Any]:
        if (
            not batch_id
            or not uploads
            or len(uploads) > MAX_UPLOAD_BATCH
            or len(uploads) != len(file_ids)
            or len(uploads) != len(logical_paths)
        ):
            raise PdfImportDraftError(
                422, "upload fields must align in batches of 1..64"
            )
        facts = [
            {
                "occurrence_id": str(file_id),
                "logical_path": _logical_path(path),
                "sha256": upload.sha256,
                "size": upload.size,
            }
            for upload, file_id, path in zip(
                uploads, file_ids, logical_paths, strict=True
            )
        ]
        directory, _ = self._open(project_id, draft_id)

        # First admit immutable occurrence facts. Byte copying and PDFium work
        # happen outside this lock so status, pause and discard stay responsive.
        with (
            self._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self._header(project_id, directory)
            if header["state"] not in {"open", "committing"}:
                raise PdfImportDraftError(409, f"PDF import is {header['state']}")
            if header["paused"]:
                raise PdfImportDraftError(409, "PDF import is paused")
            if not inventory.stats()["manifest_sealed"]:
                raise PdfImportDraftError(409, "PDF selection manifest is not sealed")
            try:
                retry = inventory.upload_batch_through(batch_id, facts)
            except ValueError as exc:
                raise PdfImportDraftError(409, str(exc)) from exc
            self._drain_commit_ready(project_id, draft_id, directory, header)
            selected = [
                inventory.selected(str(fact["occurrence_id"])) for fact in facts
            ]
            if any(item is None for item in selected):
                raise PdfImportDraftError(409, "upload occurrence is not selected")
            if retry is None:
                for fact, item in zip(facts, selected, strict=True):
                    assert item is not None
                    if (
                        item["upload_state"] != "pending"
                        or item["logical_path"] != fact["logical_path"]
                        or item["size"] != fact["size"]
                    ):
                        raise PdfImportDraftError(
                            409, "uploaded PDF changed after selection"
                        )

        if retry is None:
            with self._files_lock(directory):
                with self._lock(directory):
                    header = self._header(project_id, directory)
                    if header["state"] not in {"open", "committing"}:
                        raise PdfImportDraftError(
                            409, f"PDF import is {header['state']}"
                        )
                    if header["paused"]:
                        raise PdfImportDraftError(409, "PDF import is paused")
                    files_root = self._files_root(directory, header)
                for upload, item in zip(uploads, selected, strict=True):
                    assert item is not None
                    self._validate_upload(upload, item)
                    target = files_root / upload.sha256
                    if not target.exists():
                        self._copy(upload, target)

                # Recheck after copying: pause/cancel or a competing admission
                # may have won while the state lock was released.
                with (
                    self._lock(directory),
                    PdfDraftInventory(directory / "inventory.db") as inventory,
                ):
                    header = self._header(project_id, directory)
                    if header["state"] not in {"open", "committing"}:
                        raise PdfImportDraftError(
                            409, f"PDF import is {header['state']}"
                        )
                    if header["paused"]:
                        raise PdfImportDraftError(409, "PDF import is paused")
                    try:
                        raced = inventory.upload_batch_through(batch_id, facts)
                        if raced is None:
                            inventory.stage_batch(batch_id, facts)
                    except ValueError as exc:
                        raise PdfImportDraftError(409, str(exc)) from exc
                    if raced is None:
                        if header["state"] == "open":
                            header["match"] = self._empty_match()
                            header["batch_ocr"] = self._empty_batch_ocr()
                        self._save(directory, header)

        # Analysis is bounded but may still be slow or encounter a hostile PDF.
        # Each result is independently claimed and published after work finishes.
        for fact in facts:
            occurrence_id = str(fact["occurrence_id"])
            with PdfDraftInventory(directory / "inventory.db") as inventory:
                item = inventory.selected(occurrence_id)
            if item is None or item["analysis_state"] != "pending":
                continue
            _, current_header = self._open(project_id, draft_id)
            analysis = self._analyze(
                self._files_root(directory, current_header) / str(item["sha256"])
            )
            with (
                self._lock(directory),
                PdfDraftInventory(directory / "inventory.db") as inventory,
            ):
                header = self._header(project_id, directory)
                if header["state"] not in {"open", "committing"}:
                    continue
                if not inventory.complete_analysis(occurrence_id, analysis):
                    continue
                if header["state"] == "committing" and self._advance_commit_inventory(
                    directory, inventory, header
                ):
                    header["commit"]["ready_notification"] = {
                        "revision": int(header["revision"]) + 1
                    }
                self._save(directory, header)
                self._drain_commit_ready(project_id, draft_id, directory, header)

        return self.status(project_id, draft_id)

    def status(self, project_id: str, draft_id: str) -> dict[str, Any]:
        directory, _ = self._open(project_id, draft_id)
        with self._lock(directory):
            header = self._header(project_id, directory)
            if self._refresh_preview_jobs(project_id, header):
                self._save(directory, header)
            return self._snapshot(header, self._stats(directory))

    def list(self, project_id: str) -> list[dict[str, Any]]:
        self._purge_expired()
        project = self._workspace.get(project_id)
        result = []
        for path in sorted(self._root.glob("pdf-*/manifest.json")):
            try:
                with self._lock(path.parent):
                    header = json.loads(path.read_text("utf-8"))
                    if (
                        header["project_id"] == project_id
                        and header["storage_identity"] == project.storage_identity
                        and header["state"] not in {"cancelled", "committed"}
                    ):
                        if self._refresh_preview_jobs(project_id, header):
                            self._save(path.parent, header)
                        result.append(self._snapshot(header, self._stats(path.parent)))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return result

    def samples(
        self,
        project_id: str,
        draft_id: str,
        *,
        category: str,
        cursor: str = "",
        limit: int = 6,
    ) -> dict[str, Any]:
        if category not in {"text", "suspicious", "missing", "unreadable"}:
            raise PdfImportDraftError(422, "invalid PDF text category")
        if not 1 <= limit <= 6:
            raise PdfImportDraftError(422, "PDF sample limit must be 1..6")
        directory, _ = self._open(project_id, draft_id)
        with PdfDraftInventory(directory / "inventory.db") as inventory:
            items, next_cursor = inventory.samples(category, after=cursor, limit=limit)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "draft_id": draft_id,
            "category": category,
            "items": [
                self._sample_public(project_id, draft_id, item) for item in items
            ],
            "next_cursor": next_cursor,
        }
        return payload

    def reconcile_reselection(
        self,
        project_id: str,
        draft_id: str,
        *,
        files: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not 1 <= len(files) <= MAX_MANIFEST_BATCH:
            raise PdfImportDraftError(422, "reselection batch must contain 1..256 PDFs")
        normalized = []
        for raw in files:
            logical = _logical_path(str(raw.get("logical_path") or ""))
            filename = str(raw.get("filename") or PurePosixPath(logical).name)
            size = raw.get("size")
            if type(size) is not int or size < 0:
                raise PdfImportDraftError(422, "PDF file size is invalid")
            normalized.append(
                {"logical_path": logical, "filename": filename, "size": size}
            )
        directory, _ = self._open(project_id, draft_id)
        with PdfDraftInventory(directory / "inventory.db") as inventory:
            if not inventory.stats()["manifest_sealed"]:
                raise PdfImportDraftError(409, "PDF selection manifest is not sealed")
            items = inventory.reconcile_reselection(normalized)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "draft_id": draft_id,
            "items": items,
        }
        return payload

    def page(
        self, project_id: str, draft_id: str, occurrence_id: str, page: int
    ) -> dict[str, Any]:
        directory, header = self._open(project_id, draft_id)
        with PdfDraftInventory(directory / "inventory.db") as inventory:
            item = self._staged(inventory, occurrence_id)
            cached = inventory.cached_ocr(str(item["sha256"]), page)
        if page < 1 or page > int(item["page_count"] or 0):
            raise PdfImportDraftError(404, "PDF page not found")
        native_text = read_native_page(
            self._files_root(directory, header) / str(item["sha256"]), page=page
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "thumbnail_url": self._thumbnail_url(
                project_id, draft_id, occurrence_id, page
            ),
            "native_text": native_text,
            "ocr_text": cached.get("text") if cached else None,
            "ocr_blocks": list(cached.get("blocks") or []) if cached else [],
            "ocr_engine": cached.get("engine") if cached else None,
        }

    def thumbnail(
        self, project_id: str, draft_id: str, occurrence_id: str, page: int
    ) -> ThumbnailLease:
        directory, header = self._open(project_id, draft_id)
        with PdfDraftInventory(directory / "inventory.db") as inventory:
            item = self._staged(inventory, occurrence_id)
        if page < 1 or page > int(item["page_count"] or 0):
            raise PdfImportDraftError(404, "PDF page not found")
        return render_thumbnail(
            directory,
            source=self._files_root(directory, header) / str(item["sha256"]),
            occurrence_id=occurrence_id,
            page=page,
        )

    def set_paused(
        self, project_id: str, draft_id: str, paused: bool
    ) -> dict[str, Any]:
        directory, header = self._open(project_id, draft_id)
        with self._lock(directory):
            header = self._header(project_id, directory)
            self._require_state(header, "open")
            if header["paused"] != paused:
                header["paused"] = paused
                self._save(directory, header)
            return self._snapshot(header, self._stats(directory))

    def update_choices(
        self,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int,
        ocr_policy: str,
        ocr_engine: str,
        ocr_confirmation: str | None,
        semantic_search: bool,
        semantic_setup_confirmation: str | None,
        destination: Mapping[str, Any],
    ) -> dict[str, Any]:
        if ocr_policy not in {"missing_or_suspicious", "all", "none"}:
            raise PdfImportDraftError(422, "invalid PDF OCR policy")
        normalized_destination = self._destination(destination)
        directory, _ = self._open(project_id, draft_id)
        with (
            self._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self._header(project_id, directory)
            self._require_state(header, "open")
            self._revision(header, expected_revision)
            old = header["choices"]
            if old["destination"] != normalized_destination:
                inventory.clear_matches()
                header["match"] = self._empty_match()
                header["batch_ocr"] = self._empty_batch_ocr()
            if old["ocr_policy"] != ocr_policy or old["ocr_engine"] != ocr_engine:
                header["batch_ocr"] = self._empty_batch_ocr()
            old.update(
                {
                    "ocr_policy": ocr_policy,
                    "ocr_engine": ocr_engine,
                    "ocr_confirmation": ocr_confirmation,
                    "semantic_search": semantic_search,
                    "semantic_setup_confirmation": semantic_setup_confirmation,
                    "destination": normalized_destination,
                }
            )
            self._save(directory, header)
            return self._snapshot(header, inventory.stats())

    def refresh_semantic_setup(
        self,
        project_id: str,
        draft_id: str,
        *,
        request_context: Any = None,
    ) -> dict[str, Any]:
        directory, _ = self._open(project_id, draft_id)
        with self._lock(directory):
            header = self._header(project_id, directory)
            self._require_state(header, "open")
            current = self._resolve_semantic_setup(project_id, request_context)
            if header["semantic_setup"] != current:
                header["semantic_setup"] = current
                header["choices"]["semantic_setup_confirmation"] = None
                self._save(directory, header)
            return self._snapshot(header, self._stats(directory))

    def match_destination(self, project_id: str, draft_id: str) -> dict[str, Any]:
        directory, _ = self._open(project_id, draft_id)
        with (
            self._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self._header(project_id, directory)
            self._require_state(header, "open")
            destination = header["choices"]["destination"]
            if destination["kind"] != "existing":
                return self._snapshot(header, inventory.stats())
            new_categories = self._zero_categories()
            project = self._workspace.get(project_id)
            sheet_id = int(destination["sheet_id"])
            if self._destination_matcher is None:
                columns = {
                    str(row["name"]).lower(): int(row["id"])
                    for row in project.columns(sheet_id)
                }
                if "filename" in columns and "size" in columns:
                    inventory.match_project_values(
                        project.db_path,
                        sheet_id=sheet_id,
                        filename_column_id=columns["filename"],
                        size_column_id=columns["size"],
                    )
                else:
                    inventory.clear_matches()
                matched_count, counted_categories = inventory.match_summary()
                new_categories.update(counted_categories)
                checked = inventory.stats()["selected_count"]
            else:
                inventory.clear_matches()
                matched_count = checked = 0
                after = 0
                while True:
                    rows = inventory.staged_page(after=after, limit=256)
                    if not rows:
                        break
                    candidates = [
                        (str(row["filename"]), int(row["size"])) for row in rows
                    ]
                    matched = self._destination_matcher(project, sheet_id, candidates)
                    ids = [
                        str(row["occurrence_id"])
                        for row in rows
                        if (str(row["filename"]), int(row["size"])) in matched
                    ]
                    matched_ids = set(ids)
                    for row in rows:
                        if str(row["occurrence_id"]) not in matched_ids:
                            new_categories[str(row["category"])] += 1
                    inventory.mark_matches(ids)
                    matched_count += len(ids)
                    checked += len(rows)
                    after = int(rows[-1]["ordinal"])
            header["match"] = {
                "state": "ready",
                "sheet_id": sheet_id,
                "checked": checked,
                "total": inventory.stats()["selected_count"],
                "matched_count": matched_count,
                "new_count": checked - matched_count,
                "new_category_counts": new_categories,
                "error": None,
            }
            header["batch_ocr"] = self._empty_batch_ocr()
            self._save(directory, header)
            return self._snapshot(header, inventory.stats())

    def estimate_batch_ocr(
        self,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int,
        engine: str,
        ocr_policy: str,
        request_context: Any = None,
    ) -> dict[str, Any]:
        directory, _ = self._open(project_id, draft_id)
        with (
            self._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self._header(project_id, directory)
            self._require_state(header, "open")
            self._revision(header, expected_revision)
            stats = inventory.stats()
            if stats["analyzed_count"] != stats["staged_count"]:
                raise PdfImportDraftError(409, "PDF text analysis is still running")
            if (
                engine != "rapidocr"
                and stats["staged_count"] != stats["selected_count"]
            ):
                raise PdfImportDraftError(
                    409, "paid or remote OCR estimate requires every selected PDF"
                )
            scope_hash, exact_count, known_pages = inventory.ocr_scope_hash(ocr_policy)
            selected_count = (
                exact_count
                if stats["staged_count"] == stats["selected_count"]
                else self._ocr_count(stats, ocr_policy)
            )
            binding = {
                "purpose": "batch_pdf_ocr",
                "engine": engine,
                "options": {
                    "dpi": 200,
                    "language": None,
                    "keep_text_position": True,
                },
                "ocr_policy": ocr_policy,
                "manifest_revision": header["revision"],
                "selected_through": inventory.selected_through(),
                "selected_count": selected_count,
                "known_pages": known_pages,
                "work_scope_hash": scope_hash,
                "destination": header["choices"]["destination"],
                "match": header["match"],
            }
            binding_hash = hashlib.sha256(
                json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if self._batch_ocr_estimator is not None:
                estimate = dict(
                    self._batch_ocr_estimator(project_id, binding, request_context)
                )
            elif engine == "rapidocr":
                estimate = {
                    "rows": selected_count,
                    "cost": 0.0,
                    "billed_cost": 0,
                    "cost_source": "local",
                    "policy_id": "local",
                    "claims": [],
                    "promise_set_hash": hashlib.sha256(
                        f"rapidocr:{binding_hash}".encode()
                    ).hexdigest(),
                    "requires_confirmation": False,
                }
            else:
                raise PdfImportDraftError(503, "batch OCR estimate is unavailable")
            header["batch_ocr"] = {
                "state": "ready",
                "advisory": True,
                **binding,
                "manifest_hash": binding_hash,
                "estimate": estimate,
                "needs_confirmation": bool(estimate.get("claims")),
            }
            self._save(directory, header)
            return dict(header["batch_ocr"])

    def estimate_ocr_preview(
        self,
        project_id: str,
        draft_id: str,
        occurrence_id: str,
        page: int,
        *,
        engine: str,
        request_context: Any = None,
    ) -> dict[str, Any]:
        prepared = self._prepare_preview(
            project_id,
            draft_id,
            occurrence_id,
            page,
            engine,
            request_context=request_context,
        )
        if prepared["cached"]:
            estimate = None
        else:
            estimate = self._preview_runs.scratch_estimate(project_id, prepared["plan"])
        return {
            "schema_version": SCHEMA_VERSION,
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "engine": engine,
            "cached": prepared["cached"],
            "estimate": estimate,
        }

    def start_ocr_preview(
        self,
        project_id: str,
        draft_id: str,
        occurrence_id: str,
        page: int,
        *,
        engine: str,
        confirmation: str | None,
        request_context: Any = None,
    ) -> dict[str, Any]:
        prepared = self._prepare_preview(
            project_id,
            draft_id,
            occurrence_id,
            page,
            engine,
            request_context=request_context,
        )
        if prepared["cached"]:
            return {
                "schema_version": SCHEMA_VERSION,
                "draft_id": draft_id,
                "occurrence_id": occurrence_id,
                "page": page,
                "job_id": None,
                "status": "done",
                "receipt_id": prepared["cached"].get("receipt_id"),
                "estimate": None,
                "needs_confirmation": False,
            }
        plan = prepared["plan"]
        estimate = self._preview_runs.scratch_estimate(project_id, plan)
        started = self._preview_runs.start_scratch_preview(
            project_id,
            plan,
            confirmation=confirmation,
            router=prepared["router"],
            composition=prepared["composition"],
            execution_context=prepared["execution_context"],
            replacement_key=f"pdf-import:{draft_id}:{occurrence_id}:{page}:{engine}",
            total=1,
        )
        if started.status_code != 202:
            raise PdfImportDraftError(
                started.status_code, started.payload, bare_json=True
            )
        payload = dict(started.payload)
        directory, _ = self._open(project_id, draft_id)
        with self._lock(directory):
            header = self._header(project_id, directory)
            header["preview_jobs"] = [
                value
                for value in header["preview_jobs"]
                if not (
                    value["occurrence_id"] == occurrence_id
                    and value["page"] == page
                    and value["engine"] == engine
                )
            ][-31:]
            header["preview_jobs"].append(
                {
                    "job_id": str(payload["preview_id"]),
                    "occurrence_id": occurrence_id,
                    "page": page,
                    "engine": engine,
                    "status": "running",
                    "receipt_id": payload.get("receipt_id"),
                    "error": None,
                }
            )
            self._save(directory, header)
        return {
            "schema_version": SCHEMA_VERSION,
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "job_id": str(payload["preview_id"]),
            "status": "running",
            "receipt_id": payload.get("receipt_id"),
            "estimate": estimate,
            "needs_confirmation": False,
        }

    def freeze_for_commit(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        expected_revision: int,
        request_context: Any = None,
    ) -> FrozenPdfImport:
        directory, _ = self._open(project_id, draft_id)
        with (
            self._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self._header(project_id, directory)
            if header["state"] in {"committing", "committed"}:
                if (header["commit"] or {}).get("idempotency_key") != idempotency_key:
                    raise PdfImportDraftError(409, "PDF import commit key changed")
                return self._frozen(directory, header, inventory)
            self._require_state(header, "open")
            self._revision(header, expected_revision)
            stats = inventory.stats()
            if not stats["manifest_sealed"]:
                raise PdfImportDraftError(409, "PDF selection manifest is not sealed")
            if stats["analyzed_count"] != stats["staged_count"]:
                raise PdfImportDraftError(409, "PDF text analysis is still running")
            destination = header["choices"]["destination"]
            if (
                destination["kind"] == "existing"
                and header["match"]["state"] != "ready"
            ):
                raise PdfImportDraftError(409, "existing sheet comparison is not ready")
            batch_ocr = header["batch_ocr"]
            if (
                header["choices"]["ocr_policy"] != "none"
                and header["choices"]["ocr_engine"] != "rapidocr"
                and batch_ocr["state"] != "ready"
            ):
                raise PdfImportDraftError(
                    409, "paid or remote OCR requires an exact frozen estimate"
                )
            if batch_ocr["state"] == "ready":
                scope_hash, exact_count, known_pages = inventory.ocr_scope_hash(
                    header["choices"]["ocr_policy"]
                )
                if (
                    batch_ocr["engine"] != header["choices"]["ocr_engine"]
                    or batch_ocr["ocr_policy"] != header["choices"]["ocr_policy"]
                    or batch_ocr.get("work_scope_hash") != scope_hash
                    or batch_ocr["selected_count"] != exact_count
                    or batch_ocr.get("known_pages") != known_pages
                ):
                    raise PdfImportDraftError(
                        409, "batch OCR estimate no longer matches the selected PDFs"
                    )
            current_semantic = self._resolve_semantic_setup(project_id, request_context)
            if header["semantic_setup"] != current_semantic:
                header["semantic_setup"] = current_semantic
                header["choices"]["semantic_setup_confirmation"] = None
                self._save(directory, header)
                raise PdfImportDraftError(
                    409, "semantic search setup facts changed; review them again"
                )
            if (
                header["choices"]["semantic_search"]
                and current_semantic["needs_confirmation"]
                and header["choices"].get("semantic_setup_confirmation")
                != current_semantic["confirmation_hash"]
            ):
                raise PdfImportDraftError(
                    409, "semantic search setup requires exact download consent"
                )
            commit_id = f"pdf-commit-{secrets.token_urlsafe(18)}"
            binding = {
                "commit_id": commit_id,
                "draft_id": draft_id,
                "project_id": project_id,
                "selected_through": inventory.selected_through(),
                "selected_total": stats["selected_count"],
                "choices": header["choices"],
                "match": header["match"],
                "batch_ocr": batch_ocr,
            }
            header["state"] = "committing"
            header["commit"] = {
                "commit_id": commit_id,
                "idempotency_key": idempotency_key,
                "requested_at": now(),
                "manifest_hash": hashlib.sha256(
                    json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "selected_through": binding["selected_through"],
                "selected_total": binding["selected_total"],
                "result": None,
                # Precommit estimates are advisory only. Paid/remote OCR must
                # obtain ordinary exact action confirmation after rows exist.
                "ocr_authorization": None,
                "ready_notification": None,
                "commit_inventory_path": str(directory / "commit-inventory.db"),
                "source_files_root": str(directory / "files"),
                "promotion_target": None,
            }
            committed_total = inventory.initialize_commit_projection()
            header["commit"]["committed_total"] = committed_total
            self._advance_commit_inventory(directory, inventory, header)
            self._save(directory, header)
            return self._frozen(directory, header, inventory)

    def reconcile_commit(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        committed: bool,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        directory, _ = self._open(project_id, draft_id)
        with self._lock(directory):
            header = self._header(project_id, directory)
            if (header.get("commit") or {}).get("idempotency_key") != idempotency_key:
                raise PdfImportDraftError(409, "PDF import commit identity changed")
            if committed:
                header["state"] = "committed"
                header["commit"]["result"] = dict(result or {})
            else:
                header["state"] = "open"
                header["commit"] = None
            self._save(directory, header)
            return self._snapshot(header, self._stats(directory))

    def promote_commit_inventory(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        target_path: Path,
    ) -> FrozenPdfImport:
        """Transfer frozen inventory and staged-byte ownership to `.imports`."""

        project = self._workspace.get(project_id)
        imports_root = (project.path / ".imports").resolve()
        target = Path(target_path).resolve()
        if target.name != "inventory.db" or imports_root not in target.parents:
            raise PdfImportDraftError(422, "commit inventory target is invalid")
        staged_target = target.parent / "staged"
        if not target.parent.is_dir():
            raise PdfImportDraftError(409, "commit intake directory is not ready")

        directory, _ = self._open(project_id, draft_id)
        with self._files_lock(directory), self._lock(directory):
            header = self._header(project_id, directory)
            if header["state"] not in {"committing", "committed"}:
                raise PdfImportDraftError(409, "PDF import is not committing")
            commit = header.get("commit") or {}
            if commit.get("idempotency_key") != idempotency_key:
                raise PdfImportDraftError(409, "PDF import commit identity changed")
            stats = self._stats(directory)
            if stats["analyzed_count"] != stats["staged_count"]:
                raise PdfImportDraftError(409, "PDF text analysis is still running")
            intended = {
                "inventory_path": str(target),
                "source_files_root": str(staged_target),
            }
            existing = commit.get("promotion_target")
            if existing is not None and existing != intended:
                raise PdfImportDraftError(409, "PDF import promotion target changed")
            if existing is None:
                commit["promotion_target"] = intended
                self._save(directory, header)

            source_inventory = Path(commit["commit_inventory_path"])
            source_files = Path(commit["source_files_root"])
            self._promote_path(source_inventory, target)
            self._promote_path(source_files, staged_target)
            if not commit.get("promoted"):
                commit["promoted"] = True
                self._save(directory, header)
            with PdfDraftInventory(directory / "inventory.db") as inventory:
                return self._frozen(directory, header, inventory)

    def record_commit_handoff(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist the sheet/session handoff while late uploads remain admitted."""

        directory, _ = self._open(project_id, draft_id)
        with self._lock(directory):
            header = self._header(project_id, directory)
            self._require_state(header, "committing")
            commit = header.get("commit") or {}
            if commit.get("idempotency_key") != idempotency_key:
                raise PdfImportDraftError(409, "PDF import commit identity changed")
            value = dict(result)
            if commit.get("result") is not None and commit["result"] != value:
                raise PdfImportDraftError(409, "PDF import handoff result changed")
            if commit.get("result") is None:
                commit["result"] = value
                self._save(directory, header)
            return self._snapshot(header, self._stats(directory))

    def discard(self, project_id: str, draft_id: str) -> dict[str, Any]:
        directory, _ = self._open(project_id, draft_id)
        with self._lock(directory):
            header = self._header(project_id, directory)
            if header["state"] == "committing":
                raise PdfImportDraftError(
                    409, "PDF import commit must be reconciled before discard"
                )
            if header["state"] == "committed":
                raise PdfImportDraftError(
                    409, "committed PDF import cannot be discarded"
                )
            if header["state"] == "cancelled":
                return self._snapshot(header, self._stats(directory))
            stats = self._stats(directory)
            header["state"] = "cancelling"
            header["cleanup"] = {"bytes_done": 0, "bytes_total": stats["staged_bytes"]}
            self._save(directory, header)
            shutil.rmtree(directory / "files", ignore_errors=True)
            shutil.rmtree(directory / "cache", ignore_errors=True)
            header["state"] = "cancelled"
            header["cleanup"]["bytes_done"] = stats["staged_bytes"]
            self._save(directory, header)
            return self._snapshot(header, stats)

    def _prepare_preview(
        self, project_id, draft_id, occurrence_id, page, engine, *, request_context
    ):
        if self._preview_runs is None:
            raise PdfImportDraftError(503, "OCR preview is unavailable")
        directory, header = self._open(project_id, draft_id)
        with PdfDraftInventory(directory / "inventory.db") as inventory:
            item = self._staged(inventory, occurrence_id)
            _settings, settings_hash = preview_settings(engine)
            cached = inventory.cached_ocr(
                str(item["sha256"]), page, engine, settings_hash
            )
        if cached is not None:
            return {"cached": cached}
        prepared = prepare_ocr(
            self._preview_runs,
            project_id=project_id,
            source=self._files_root(directory, header) / str(item["sha256"]),
            item=item,
            occurrence_id=occurrence_id,
            page=page,
            engine=engine,
            request_context=request_context,
            on_page=lambda value: self._cache_preview(
                project_id,
                draft_id,
                str(item["sha256"]),
                engine,
                settings_hash,
                value,
            ),
        )
        return {"cached": None, **prepared}

    def _cache_preview(self, project_id, draft_id, sha256, engine, settings_hash, page):
        number = page.get("page")
        if type(number) is not int:
            return
        directory, _ = self._open(project_id, draft_id)
        with (
            self._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self._header(project_id, directory)
            if header["state"] in {"cancelling", "cancelled"}:
                return
            inventory.cache_ocr(
                sha256,
                number,
                engine,
                settings_hash,
                {
                    "engine": engine,
                    "text": str(page.get("text") or ""),
                    "blocks": list(page.get("blocks") or []),
                    "width": page.get("width"),
                    "height": page.get("height"),
                    "receipt_id": page.get("receipt_id"),
                },
            )

    def _refresh_preview_jobs(self, project_id: str, header: dict[str, Any]) -> bool:
        if self._preview_runs is None:
            return False
        changed = False
        for job in header["preview_jobs"]:
            if job["status"] != "running":
                continue
            response = self._preview_runs.get_preview(project_id, str(job["job_id"]))
            payload: Mapping[str, Any]
            if response.status_code == 404:
                payload = {}
                status = "failed"
                error = "OCR preview expired before its result was collected"
            elif response.status_code == 200:
                payload = response.payload
                raw_status = payload.get("status")
                status = "failed" if raw_status == "error" else str(raw_status)
                if status not in {"running", "done", "failed", "cancelled"}:
                    continue
                raw_error = payload.get("error")
                error = (
                    str(raw_error.get("message") or raw_error)[:500]
                    if isinstance(raw_error, Mapping)
                    else (str(raw_error)[:500] if raw_error else None)
                )
            else:
                continue
            accounting = payload.get("accounting")
            receipt_id = (
                accounting.get("receipt_id")
                if isinstance(accounting, Mapping)
                else None
            )
            if (
                job["status"] != status
                or job.get("error") != error
                or (receipt_id is not None and job.get("receipt_id") != receipt_id)
            ):
                job["status"] = status
                job["error"] = error
                if receipt_id is not None:
                    job["receipt_id"] = str(receipt_id)
                changed = True
        return changed

    def _analyze(self, path: Path) -> dict[str, Any]:
        return analyze_pdf(path, quality_assessor=self._quality_assessor)

    @staticmethod
    def _validate_upload(upload: AdmittedUpload, selected: Mapping[str, Any]) -> None:
        if upload.mime not in {"application/pdf", "application/octet-stream"}:
            raise PdfImportDraftError(422, "multi-PDF import accepts PDF files only")
        if upload.size != selected["size"]:
            raise PdfImportDraftError(409, "uploaded PDF changed after selection")
        with PdfImportService._source(upload) as source:
            if source.read(5) != b"%PDF-":
                raise PdfImportDraftError(
                    422, "multi-PDF import accepts PDF files only"
                )

    @staticmethod
    @contextlib.contextmanager
    def _source(upload: AdmittedUpload):
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

    @staticmethod
    def _copy(upload: AdmittedUpload, target: Path) -> None:
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}")
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                PdfImportService._source(upload) as source,
                temporary.open("xb") as sink,
            ):
                while chunk := source.read(COPY_CHUNK):
                    size += len(chunk)
                    digest.update(chunk)
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            if size != upload.size or digest.hexdigest() != upload.sha256:
                raise PdfImportDraftError(409, "uploaded PDF changed after admission")
            os.replace(temporary, target)
            fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _open(self, project_id: str, draft_id: str) -> tuple[Path, dict[str, Any]]:
        return self._store.open(project_id, draft_id)

    def _header(self, project_id: str, directory: Path) -> dict[str, Any]:
        return self._store.header(project_id, directory)

    def _lock(self, directory: Path):
        return self._store.lock(directory)

    @staticmethod
    def _files_lock(directory: Path) -> FileLock:
        return FileLock(str(directory / ".files.lock"))

    @staticmethod
    def _commit_inventory_path(directory: Path, header: Mapping[str, Any]) -> Path:
        commit = header.get("commit") or {}
        local = Path(
            commit.get("commit_inventory_path") or directory / "commit-inventory.db"
        )
        target = (commit.get("promotion_target") or {}).get("inventory_path")
        if target is not None:
            promoted = Path(target)
            if commit.get("promoted") or promoted.exists() or not local.exists():
                return promoted
        return local

    @staticmethod
    def _files_root(directory: Path, header: Mapping[str, Any]) -> Path:
        commit = header.get("commit") or {}
        local = Path(commit.get("source_files_root") or directory / "files")
        target = (commit.get("promotion_target") or {}).get("source_files_root")
        if target is not None:
            promoted = Path(target)
            if commit.get("promoted") or promoted.exists() or not local.exists():
                return promoted
        return local

    @staticmethod
    def _promote_path(source: Path, target: Path) -> None:
        if source == target:
            return
        if target.exists():
            if source.exists():
                raise PdfImportDraftError(409, "commit promotion target already exists")
            return
        if not source.exists():
            raise PdfImportDraftError(409, "commit promotion source is missing")
        if source.is_file():
            with ImportInventory(source) as inventory:
                inventory._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            os.replace(source, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}")
            try:
                if source.is_dir():
                    shutil.copytree(source, temporary)
                    os.replace(temporary, target)
                    shutil.rmtree(source)
                else:
                    shutil.copy2(source, temporary)
                    os.replace(temporary, target)
                    source.unlink()
            finally:
                if temporary.is_dir():
                    shutil.rmtree(temporary, ignore_errors=True)
                else:
                    temporary.unlink(missing_ok=True)
        fsync_directory(target.parent)

    @staticmethod
    def _require_state(header: Mapping[str, Any], state: str) -> None:
        if header["state"] != state:
            raise PdfImportDraftError(409, f"PDF import is {header['state']}")

    @staticmethod
    def _revision(header: Mapping[str, Any], expected: int) -> None:
        if header["revision"] != expected:
            raise PdfImportDraftError(409, "PDF import draft revision changed")

    def _save(self, directory: Path, header: dict[str, Any]) -> None:
        self._store.save(directory, header)

    def _purge_expired(self) -> None:
        self._store.purge_expired()

    def _stats(self, directory: Path) -> dict[str, int]:
        return self._store.stats(directory)

    def _drain_commit_ready(
        self,
        project_id: str,
        draft_id: str,
        directory: Path,
        header: dict[str, Any],
    ) -> None:
        commit = header.get("commit")
        notification = (commit or {}).get("ready_notification")
        if (
            header.get("state") == "committing"
            and commit is not None
            and notification is not None
            and self._on_commit_ready is not None
        ):
            self._on_commit_ready(
                project_id,
                draft_id,
                str(commit["idempotency_key"]),
                int(notification["revision"]),
            )
            commit["ready_notification"] = None
            self._save(directory, header)

    @staticmethod
    def _staged(inventory: PdfDraftInventory, occurrence_id: str) -> dict[str, Any]:
        item = inventory.selected(occurrence_id)
        if item is None or item["upload_state"] != "staged":
            raise PdfImportDraftError(404, "PDF import file not found")
        return item

    @staticmethod
    def _destination(value: Mapping[str, Any]) -> dict[str, Any]:
        if value.get("kind") == "new":
            name = str(value.get("sheet_name") or "").strip()
            if not name:
                raise PdfImportDraftError(422, "new sheet name is required")
            return {"kind": "new", "sheet_name": name}
        if (
            value.get("kind") == "existing"
            and type(value.get("sheet_id")) is int
            and value["sheet_id"] > 0
        ):
            return {"kind": "existing", "sheet_id": value["sheet_id"]}
        raise PdfImportDraftError(422, "PDF import destination is invalid")

    @staticmethod
    def _empty_match() -> dict[str, Any]:
        return {
            "state": "idle",
            "sheet_id": None,
            "checked": 0,
            "total": 0,
            "matched_count": 0,
            "new_count": 0,
            "new_category_counts": PdfImportService._zero_categories(),
            "error": None,
        }

    @staticmethod
    def _zero_categories() -> dict[str, int]:
        return {
            "text": 0,
            "suspicious": 0,
            "missing": 0,
            "image": 0,
            "other": 0,
            "unreadable": 0,
            "unchecked": 0,
        }

    @staticmethod
    def _empty_batch_ocr() -> dict[str, Any]:
        return {
            "state": "idle",
            "advisory": True,
            "engine": None,
            "ocr_policy": None,
            "manifest_revision": None,
            "manifest_hash": None,
            "selected_through": 0,
            "selected_count": 0,
            "estimate": None,
            "needs_confirmation": False,
        }

    @staticmethod
    def _ocr_count(stats: Mapping[str, int], policy: str) -> int:
        if policy == "none":
            return 0
        if policy == "all":
            return stats["selected_count"]
        if policy == "missing_or_suspicious":
            return stats["missing_count"] + stats["suspicious_count"]
        raise PdfImportDraftError(422, "invalid PDF OCR policy")

    @staticmethod
    def _thumbnail_url(
        project_id: str, draft_id: str, occurrence_id: str, page: int
    ) -> str:
        return (
            f"/api/projects/{project_id}/import/pdf-imports/{draft_id}/files/"
            f"{occurrence_id}/pages/{page}/thumbnail"
        )

    def _sample_public(
        self, project_id: str, draft_id: str, item: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "occurrence_id": item["occurrence_id"],
            "logical_path": item["logical_path"],
            "filename": item["filename"],
            "size": item["size"],
            "page_count": item.get("page_count"),
            "category": item["category"],
            "analysis_state": item["analysis_state"],
            "sample_pages": item["sample_pages"],
            "thumbnail_url": (
                self._thumbnail_url(project_id, draft_id, str(item["occurrence_id"]), 1)
                if item.get("page_count")
                else None
            ),
            "error": item.get("error") if item["category"] == "unreadable" else None,
        }

    @staticmethod
    def _snapshot(
        header: Mapping[str, Any], stats: Mapping[str, int]
    ) -> dict[str, Any]:
        categories = {
            "text": stats["text_count"],
            "suspicious": stats["suspicious_count"],
            "missing": stats["missing_count"],
            "image": 0,
            "other": 0,
            "unreadable": stats["unreadable_count"],
            "unchecked": stats["selected_count"] - stats["analyzed_count"],
        }
        ready = {
            key: min(value, 6)
            for key, value in categories.items()
            if key != "unchecked"
        }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "id": header["draft_id"],
            "draft_id": header["draft_id"],
            "state": header["state"],
            "paused": header["paused"],
            "revision": header["revision"],
            "created_at": header["created_at"],
            "updated_at": header["updated_at"],
            "expires_at": header["expires_at"],
            "source_label": header["source_label"],
            "preflight": dict(header["preflight"]),
            "manifest": {
                "files_done": stats["selected_count"],
                "files_total": header["expected_files"],
                "bytes_done": stats["selected_bytes"],
                "bytes_total": header["expected_bytes"],
                "sealed": bool(stats["manifest_sealed"]),
            },
            "upload": {
                "files_done": stats["staged_count"],
                "files_total": header["expected_files"],
                "bytes_done": stats["staged_bytes"],
                "bytes_total": header["expected_bytes"],
                "sealed": stats["staged_count"] == header["expected_files"],
            },
            "analysis": {
                "files_done": stats["analyzed_count"],
                "files_total": header["expected_files"],
            },
            "category_counts": categories,
            "sample_ready_counts": ready,
            "can_review_text": stats["analyzed_count"] > 0,
            "choices": dict(header["choices"]),
            "match": dict(header["match"]),
            "batch_ocr": dict(header["batch_ocr"]),
            "semantic_setup": dict(header["semantic_setup"]),
            "preview_jobs": [dict(value) for value in header["preview_jobs"]],
            "cleanup": dict(header["cleanup"]),
            "commit": dict(header["commit"]) if header.get("commit") else None,
        }
        return PdfImportStatus.model_validate(payload).model_dump(mode="json")

    def _frozen(
        self, directory: Path, header: Mapping[str, Any], inventory: PdfDraftInventory
    ) -> FrozenPdfImport:
        commit = header["commit"]
        commit_inventory_path = self._commit_inventory_path(directory, header)
        source_files_root = self._files_root(directory, header)
        if commit_inventory_path.exists():
            with ImportInventory(commit_inventory_path) as commit_inventory:
                available_through = commit_inventory.through
                sealed = commit_inventory.sealed
        else:
            available_through = 0
            sealed = False
        stats = inventory.stats()
        return FrozenPdfImport(
            commit_id=str(commit["commit_id"]),
            draft_id=str(header["draft_id"]),
            project_id=str(header["project_id"]),
            storage_identity=str(header["storage_identity"]),
            idempotency_key=str(commit["idempotency_key"]),
            selected_through=int(commit["selected_through"]),
            selected_total=int(commit["selected_total"]),
            staged_through=inventory.base.through,
            manifest_hash=str(commit["manifest_hash"]),
            destination=dict(header["choices"]["destination"]),
            ocr_policy=str(header["choices"]["ocr_policy"]),
            ocr_engine=str(header["choices"]["ocr_engine"]),
            ocr_authorization=None,
            semantic_search=bool(header["choices"]["semantic_search"]),
            semantic_setup_confirmation=header["choices"].get(
                "semantic_setup_confirmation"
            ),
            directory=directory,
            source_files_root=source_files_root,
            commit_inventory_path=commit_inventory_path,
            committed_total=int(commit.get("committed_total") or 0),
            available_through=available_through,
            uploads_complete=stats["staged_count"] == stats["selected_count"],
            sealed=sealed,
            commit_result=(
                dict(commit["result"]) if commit.get("result") is not None else None
            ),
        )

    def _resolve_semantic_setup(
        self, project_id: str, request_context: Any
    ) -> dict[str, Any]:
        if self._semantic_setup_resolver is not None:
            value = dict(self._semantic_setup_resolver(project_id, request_context))
        else:
            from frisket.engine.jobs.engine_setup import local_search_setup_requirements

            requirement = next(
                (
                    item
                    for item in local_search_setup_requirements()
                    if str(item.capability) == "semantic_search"
                ),
                None,
            )
            if requirement is None:
                return {
                    "setup_ref": None,
                    "display_name": "Search by meaning",
                    "ready": False,
                    "runtime_ready": False,
                    "download_bytes": None,
                    "additional_runtime_download": False,
                    "needs_confirmation": False,
                    "confirmation_hash": None,
                    "license": None,
                    "source_url": None,
                    "available": False,
                }
            ready = bool(requirement.runtime_ready and requirement.weights_ready)
            value = {
                "setup_ref": str(requirement.setup_ref),
                "display_name": str(requirement.display_name),
                "ready": ready,
                "runtime_ready": bool(requirement.runtime_ready),
                "download_bytes": (
                    None
                    if requirement.weights_ready
                    else int(requirement.download_bytes_if_missing)
                ),
                "additional_runtime_download": bool(
                    requirement.runtime_installable and not requirement.runtime_ready
                ),
                "license": str(requirement.license),
                "source_url": str(requirement.source_url),
                "available": bool(
                    ready
                    or requirement.runtime_ready
                    or requirement.runtime_installable
                ),
            }
        binding = {
            key: value.get(key)
            for key in (
                "setup_ref",
                "display_name",
                "runtime_ready",
                "download_bytes",
                "additional_runtime_download",
                "license",
                "source_url",
                "available",
            )
        }
        needs_confirmation = bool(
            value.get("available", True) and not value.get("ready")
        )
        return {
            **value,
            "needs_confirmation": needs_confirmation,
            "confirmation_hash": (
                hashlib.sha256(
                    json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if needs_confirmation
                else None
            ),
        }

    @staticmethod
    def _advance_commit_inventory(
        directory: Path,
        inventory: PdfDraftInventory,
        header: Mapping[str, Any],
    ) -> bool:
        """Append the longest newly available contiguous projection prefix."""

        target = PdfImportService._commit_inventory_path(directory, header)
        with ImportInventory(target) as commit_inventory:
            PdfImportService._ensure_commit_metadata(commit_inventory)
            initial_through = commit_inventory.through
            initially_sealed = commit_inventory.sealed
            after = commit_inventory.through
            while True:
                rows = inventory.projection_page(after=after, limit=256)
                if not rows:
                    break
                ready = []
                for row in rows:
                    if (
                        row["upload_state"] != "staged"
                        or row["analysis_state"] == "pending"
                    ):
                        break
                    ready.append(
                        {
                            "logical_path": row["logical_path"],
                            "mime": "application/pdf",
                            "sha256": row["sha256"],
                            "size": row["size"],
                            "kind": "files",
                        }
                    )
                if ready:
                    PdfImportService._append_commit_page(
                        commit_inventory,
                        ready,
                        rows[: len(ready)],
                        ocr_policy=str(header["choices"]["ocr_policy"]),
                    )
                    after = commit_inventory.through
                if len(ready) != len(rows):
                    break
            stats = inventory.stats()
            if (
                stats["staged_count"] == stats["selected_count"]
                and stats["analyzed_count"] == stats["selected_count"]
                and not commit_inventory.sealed
            ):
                commit_inventory.seal()
            return (
                commit_inventory.through != initial_through
                or commit_inventory.sealed != initially_sealed
            )

    @staticmethod
    def _ensure_commit_metadata(commit_inventory: ImportInventory) -> None:
        commit_inventory._db.execute(
            "CREATE TABLE IF NOT EXISTS pdf_commit_occurrences ("
            "ordinal INTEGER PRIMARY KEY,"
            "occurrence_id TEXT NOT NULL UNIQUE,"
            "selected_ordinal INTEGER NOT NULL UNIQUE,"
            "category TEXT NOT NULL,"
            "ocr_selected INTEGER NOT NULL CHECK(ocr_selected IN (0,1)))"
        )
        commit_inventory._db.commit()

    @staticmethod
    def _append_commit_page(
        commit_inventory: ImportInventory,
        items: Sequence[Mapping[str, Any]],
        rows: Sequence[Mapping[str, Any]],
        *,
        ocr_policy: str,
    ) -> None:
        db = commit_inventory._db
        db.execute("BEGIN IMMEDIATE")
        count = total_bytes = last = 0
        try:
            if commit_inventory.sealed:
                raise RuntimeError("import inventory is sealed")
            for item, row in zip(items, rows, strict=True):
                values = ImportInventory._validated(item)
                cursor = db.execute(
                    "INSERT INTO inventory_items"
                    "(logical_path,mime,sha256,size,kind,email_format) "
                    "VALUES(?,?,?,?,?,?)",
                    values,
                )
                ordinal = int(cursor.lastrowid)
                if ordinal != int(row["commit_ordinal"]):
                    raise RuntimeError("commit inventory ordinal changed")
                category = str(row["category"])
                ocr_selected = ocr_policy == "all" or (
                    ocr_policy == "missing_or_suspicious"
                    and category in {"missing", "suspicious"}
                )
                db.execute(
                    "INSERT INTO pdf_commit_occurrences"
                    "(ordinal,occurrence_id,selected_ordinal,category,ocr_selected) "
                    "VALUES(?,?,?,?,?)",
                    (
                        ordinal,
                        str(row["occurrence_id"]),
                        int(row["selected_ordinal"]),
                        category,
                        int(ocr_selected),
                    ),
                )
                count += 1
                total_bytes += int(values[3])
                last = ordinal
            db.execute(
                "UPDATE inventory_meta SET item_count=item_count+?,"
                "total_bytes=total_bytes+?,through_ordinal=? WHERE singleton=1",
                (count, total_bytes, last),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise


__all__ = [
    "FrozenPdfImport",
    "FrozenPdfOccurrence",
    "PdfImportDraftError",
    "PdfImportService",
    "SCHEMA_VERSION",
]

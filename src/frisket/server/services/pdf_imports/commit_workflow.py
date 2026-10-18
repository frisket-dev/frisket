"""Commit freeze, canonical inventory promotion, and reconciliation."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from filelock import FileLock

from frisket.engine.store.import_inventory import ImportInventory

from .inventory import PdfDraftInventory
from .storage import PdfImportDraftError, now


class CommitHost(Protocol):
    _workspace: Any

    def _open(self, project_id: str, draft_id: str) -> tuple[Path, dict[str, Any]]: ...

    def _lock(self, directory: Path) -> FileLock: ...

    def _files_lock(self, directory: Path) -> FileLock: ...

    def _header(self, project_id: str, directory: Path) -> dict[str, Any]: ...

    def _save(self, directory: Path, header: dict[str, Any]) -> None: ...

    def _stats(self, directory: Path) -> dict[str, int]: ...

    def _snapshot(
        self, header: Mapping[str, Any], stats: Mapping[str, int]
    ) -> dict[str, Any]: ...

    def _require_state(self, header: Mapping[str, Any], state: str) -> None: ...

    def _revision(self, header: Mapping[str, Any], expected: int) -> None: ...

    def _resolve_semantic_setup(
        self, project_id: str, request_context: Any
    ) -> dict[str, Any]: ...

    def _ocr_action_params(
        self, selection: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def _is_rapidocr(self, action_params: Mapping[str, Any]) -> bool: ...

    def _empty_batch_ocr(self) -> dict[str, Any]: ...

    def _commit_inventory_path(
        self, directory: Path, header: Mapping[str, Any]
    ) -> Path: ...

    def _files_root(self, directory: Path, header: Mapping[str, Any]) -> Path: ...

    def _promote_path(self, source: Path, target: Path) -> None: ...


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
    error: str | None
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
    ocr_action_params: Mapping[str, Any]
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
                error=str(row["error"]) if row.get("error") else None,
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



class PdfImportCommitWorkflow:
    def __init__(self, host: CommitHost) -> None:
        self.host = host

    def freeze_for_commit(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        expected_revision: int,
        request_context: Any = None,
    ) -> FrozenPdfImport:
        directory, _ = self.host._open(project_id, draft_id)
        with (
            self.host._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self.host._header(project_id, directory)
            if header["state"] in {"committing", "committed"}:
                if (header["commit"] or {}).get("idempotency_key") != idempotency_key:
                    raise PdfImportDraftError(409, "PDF import commit key changed")
                return self._frozen(directory, header, inventory)
            self.host._require_state(header, "open")
            self.host._revision(header, expected_revision)
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
                and not self.host._is_rapidocr(
                    self.host._ocr_action_params(header["choices"]["ocr_selection"])
                )
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
                    batch_ocr.get("ocr_action_params")
                    != self.host._ocr_action_params(header["choices"]["ocr_selection"])
                    or batch_ocr["ocr_policy"] != header["choices"]["ocr_policy"]
                    or batch_ocr.get("work_scope_hash") != scope_hash
                    or batch_ocr["selected_count"] != exact_count
                    or batch_ocr.get("known_pages") != known_pages
                ):
                    raise PdfImportDraftError(
                        409, "batch OCR estimate no longer matches the selected PDFs"
                    )
            current_semantic = self.host._resolve_semantic_setup(project_id, request_context)
            if header["semantic_setup"] != current_semantic:
                header["semantic_setup"] = current_semantic
                header["choices"]["semantic_setup_confirmation"] = None
                self.host._save(directory, header)
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
            self.advance_commit_inventory(directory, inventory, header)
            self.host._save(directory, header)
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
        directory, _ = self.host._open(project_id, draft_id)
        with (
            self.host._files_lock(directory),
            self.host._lock(directory),
            PdfDraftInventory(directory / "inventory.db") as inventory,
        ):
            header = self.host._header(project_id, directory)
            commit = header.get("commit") or {}
            if commit.get("idempotency_key") != idempotency_key:
                raise PdfImportDraftError(409, "PDF import commit identity changed")
            if committed:
                frozen = self._frozen(directory, header, inventory)
                if (
                    not commit.get("promoted")
                    or not frozen.uploads_complete
                    or not frozen.sealed
                ):
                    raise PdfImportDraftError(
                        409, "PDF import commit is not fully admitted"
                    )
                header["state"] = "committed"
                header["commit"]["result"] = dict(result or {})
            else:
                if commit.get("promoted") or commit.get("promotion_target"):
                    raise PdfImportDraftError(
                        409, "promoted PDF import cannot return to draft state"
                    )
                commit_inventory_path = commit.get("commit_inventory_path")
                if isinstance(commit_inventory_path, str):
                    Path(commit_inventory_path).unlink(missing_ok=True)
                inventory.reset_commit_projection()
                header["state"] = "open"
                header["commit"] = None
                header["batch_ocr"] = self.host._empty_batch_ocr()
            self.host._save(directory, header)
            return self.host._snapshot(header, inventory.stats())

    def promote_commit_inventory(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        target_path: Path,
    ) -> FrozenPdfImport:
        """Transfer frozen inventory and staged-byte ownership to `.imports`."""

        project = self.host._workspace.get(project_id)
        imports_root = (project.path / ".imports").resolve()
        target = Path(target_path).resolve()
        if target.name != "inventory.db" or imports_root not in target.parents:
            raise PdfImportDraftError(422, "commit inventory target is invalid")
        staged_target = target.parent / "staged"
        if not target.parent.is_dir():
            raise PdfImportDraftError(409, "commit intake directory is not ready")

        directory, _ = self.host._open(project_id, draft_id)
        with self.host._files_lock(directory), self.host._lock(directory):
            header = self.host._header(project_id, directory)
            if header["state"] not in {"committing", "committed"}:
                raise PdfImportDraftError(409, "PDF import is not committing")
            commit = header.get("commit") or {}
            if commit.get("idempotency_key") != idempotency_key:
                raise PdfImportDraftError(409, "PDF import commit identity changed")
            stats = self.host._stats(directory)
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
                self.host._save(directory, header)

            source_inventory = Path(commit["commit_inventory_path"])
            source_files = Path(commit["source_files_root"])
            self.host._promote_path(source_inventory, target)
            self.host._promote_path(source_files, staged_target)
            if not commit.get("promoted"):
                commit["promoted"] = True
                self.host._save(directory, header)
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

        directory, _ = self.host._open(project_id, draft_id)
        with self.host._lock(directory):
            header = self.host._header(project_id, directory)
            self.host._require_state(header, "committing")
            commit = header.get("commit") or {}
            if commit.get("idempotency_key") != idempotency_key:
                raise PdfImportDraftError(409, "PDF import commit identity changed")
            value = dict(result)
            if commit.get("result") is not None and commit["result"] != value:
                raise PdfImportDraftError(409, "PDF import handoff result changed")
            if commit.get("result") is None:
                commit["result"] = value
                self.host._save(directory, header)
            return self.host._snapshot(header, self.host._stats(directory))

    def _frozen(
        self, directory: Path, header: Mapping[str, Any], inventory: PdfDraftInventory
    ) -> FrozenPdfImport:
        commit = header["commit"]
        commit_inventory_path = self.host._commit_inventory_path(directory, header)
        source_files_root = self.host._files_root(directory, header)
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
            ocr_action_params=self.host._ocr_action_params(
                header["choices"]["ocr_selection"]
            ),
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

    def advance_commit_inventory(
        self,
        directory: Path,
        inventory: PdfDraftInventory,
        header: Mapping[str, Any],
    ) -> bool:
        """Append the longest newly available contiguous projection prefix."""

        target = self.host._commit_inventory_path(directory, header)
        with ImportInventory(target) as commit_inventory:
            self._ensure_commit_metadata(commit_inventory)
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
                    self._append_commit_page(
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
            "error TEXT,"
            "ocr_selected INTEGER NOT NULL CHECK(ocr_selected IN (0,1)))"
        )
        columns = {
            str(row["name"])
            for row in commit_inventory._db.execute(
                "PRAGMA table_info(pdf_commit_occurrences)"
            )
        }
        if "error" not in columns:
            commit_inventory._db.execute(
                "ALTER TABLE pdf_commit_occurrences ADD COLUMN error TEXT"
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
                    "(ordinal,occurrence_id,selected_ordinal,category,error,ocr_selected) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        ordinal,
                        str(row["occurrence_id"]),
                        int(row["selected_ordinal"]),
                        category,
                        str(row["error"]) if row.get("error") else None,
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
    "PdfImportCommitWorkflow",
]

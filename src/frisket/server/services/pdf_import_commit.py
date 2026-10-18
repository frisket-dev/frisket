"""Promote frozen PDF drafts into the ordinary ``import.files`` intake."""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from filelock import FileLock

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import ActionResult, ReceiptIO
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    bind_typed_action_job,
    reserve_typed_action_job,
)
from frisket.engine.executor.file_inventory_read import FileInventoryAdmission
from frisket.engine.executor.table_action import run_inventory_table_page
from frisket.engine.jobs.queue import IMPORT_FILES_PAGE_KIND
from frisket.engine.store.import_intake import (
    ImportIntakeHeader,
    compact_terminal_inventory,
    import_admit_lock,
    import_intake_dir,
    import_worker_lock,
    read_import_header,
    write_import_header,
)
from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.import_sessions import ImportSession, ImportSessionStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.server.services.import_sessions import import_page_dedupe_key
from frisket.server.services.import_uploads import upload_sheet_name
from frisket.server.services.pdf_imports.storage import PdfImportPromotionRejected


_PAGE_SIZE = 256
_PAGE_BYTES = 64 * 1024 * 1024
LOG = logging.getLogger("frisket.server")


class PdfImportIntake(Protocol):
    def freeze_for_commit(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        expected_revision: int,
    ) -> Any: ...

    def promote_commit_inventory(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        target_path: Path,
    ) -> Any: ...

    def record_commit_handoff(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        result: Mapping[str, Any],
    ) -> Any: ...

    def reconcile_commit(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        committed: bool,
        result: Mapping[str, Any] | None = None,
    ) -> Any: ...


class PdfImportTextPreparation(Protocol):
    def reserve_pdf_import(
        self, project_id: str, *, receipt_id: str, import_ref: str
    ) -> Any: ...

    def progress_pdf_import(self, project_id: str, import_ref: str) -> Any: ...

    def abandon_pdf_import(
        self, project_id: str, *, receipt_id: str, import_ref: str
    ) -> Any: ...


@dataclass(frozen=True)
class PdfImportCommitResult:
    commit_id: str
    import_ref: str
    receipt_id: str
    sheet_id: int
    state: str
    committed_rows: int
    selected_total: int
    available_files: int
    uploads_complete: bool


class PdfImportCommitService:
    """Turn a reviewed draft into one durable ordinary import session."""

    def __init__(
        self,
        workspace: Any,
        *,
        intake: PdfImportIntake,
        text_preparation: PdfImportTextPreparation | None = None,
        enqueue: Callable[[str, str, int, bool], Any] | None = None,
    ) -> None:
        self._workspace = workspace
        self._intake = intake
        self._text_preparation = text_preparation
        self._enqueue = enqueue or self._enqueue_page

    def enqueue_continuation(
        self,
        project_id: str,
        draft_id: str,
        idempotency_key: str,
        expected_revision: int,
    ) -> int:
        """Backend callback after a late upload advances the canonical roster."""

        frozen = self._intake.freeze_for_commit(
            project_id,
            draft_id,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
        )
        ref, directory, _header = self._ensure_intake(project_id, frozen)
        self._promote(project_id, draft_id, idempotency_key, directory)
        with ImportInventory(directory / "inventory.db") as inventory:
            through, sealed = inventory.through, inventory.sealed
        job_id = int(self._enqueue(project_id, ref, through, sealed) or 0)
        if sealed and getattr(frozen, "commit_result", None) is not None:
            self._mark_handed_off(
                project_id, draft_id, frozen, dict(frozen.commit_result)
            )
        return job_id

    admit_ready = enqueue_continuation

    def commit(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        expected_revision: int,
    ) -> PdfImportCommitResult:
        frozen = self._intake.freeze_for_commit(
            project_id,
            draft_id,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
        )
        return self.commit_frozen(project_id, frozen)

    def commit_frozen(self, project_id: str, frozen: Any) -> PdfImportCommitResult:
        """Commit a snapshot already frozen by the request-authority route."""

        draft_id = str(frozen.draft_id)
        idempotency_key = str(frozen.idempotency_key)
        if frozen.project_id != project_id:
            raise ValueError("frozen PDF import belongs to another project")
        project = self._workspace.get(project_id)
        if frozen.storage_identity != project.storage_identity:
            raise ValueError("frozen PDF import storage identity changed")

        ref, directory, header = self._ensure_intake(project_id, frozen)
        try:
            self._promote(project_id, draft_id, idempotency_key, directory)
        except PdfImportPromotionRejected:
            self._rollback_unpromoted(
                project_id,
                draft_id,
                idempotency_key,
                directory,
                header,
            )
            raise
        envelope = ActionJobEnvelope.from_json(header.envelope)
        bound = bind_typed_action_job(envelope, project=project)
        if isinstance(bound, ActionResult):
            raise ValueError("PDF import request no longer validates")

        with import_worker_lock(directory, timeout=5):
            session = ImportSessionStore(project).get(ref)
            if session is None:
                self._start_reserved_receipt(project, envelope, frozen)
                with ImportInventory(directory / "inventory.db") as inventory:
                    self._promote_page_blobs(project, directory, inventory, after=0)
                    session = run_inventory_table_page(
                        project,
                        project_id,
                        bound,
                        admission=FileInventoryAdmission(
                            ref,
                            inventory,
                            project.blob_store,
                            after=0,
                            limit=_PAGE_SIZE,
                            byte_budget=_PAGE_BYTES,
                        ),
                        session_id=ref,
                        writer_authority=f"pdf-import-bootstrap:{ref}",
                        reserved_action_id=envelope.action_id,
                        reserved_receipt_id=envelope.receipt_id,
                        destination_sheet_id=header.destination_sheet_id,
                    )
            elif session.state in {"active", "paused"}:
                with ImportInventory(directory / "inventory.db") as inventory:
                    if session.cursor < inventory.through or inventory.sealed:
                        authority = f"pdf-import-bootstrap:{ref}"
                        if (
                            session.writer_authority != authority
                            or session.state == "paused"
                        ):
                            session = StreamingSheetWriter.resume_session(
                                project,
                                session_id=ref,
                                writer_authority=authority,
                                expected_cursor=session.cursor,
                            )
                        self._promote_page_blobs(
                            project, directory, inventory, after=session.cursor
                        )
                        session = run_inventory_table_page(
                            project,
                            project_id,
                            bound,
                            admission=FileInventoryAdmission(
                                ref,
                                inventory,
                                project.blob_store,
                                after=session.cursor,
                                limit=_PAGE_SIZE,
                                byte_budget=_PAGE_BYTES,
                            ),
                            session_id=ref,
                            writer_authority=authority,
                            reserved_action_id=envelope.action_id,
                            reserved_receipt_id=envelope.receipt_id,
                            destination_sheet_id=header.destination_sheet_id,
                        )
            handoff = self._record_handoff(project_id, draft_id, frozen, ref, session)

        with ImportInventory(directory / "inventory.db") as inventory:
            through, sealed = inventory.through, inventory.sealed
        if session.state not in {"completed", "kept", "removed"}:
            self._enqueue(project_id, ref, through, sealed)
        if sealed:
            self._mark_handed_off(project_id, draft_id, frozen, handoff)
        if self._text_preparation is not None:
            try:
                self._text_preparation.progress_pdf_import(project_id, ref)
            except Exception:  # noqa: BLE001 - durable recovery owns retries
                LOG.warning(
                    "reviewed PDF text preparation will retry in recovery",
                    exc_info=True,
                    extra={"project_id": project_id, "import_ref": ref},
                )
        if session.state in {"completed", "kept"}:
            with import_worker_lock(directory, timeout=5):
                with import_admit_lock(directory):
                    compact_terminal_inventory(directory)
        return self._result(frozen, ref, session, through, sealed)

    @staticmethod
    def _ref(commit_id: str) -> str:
        digest = hashlib.sha256(f"pdf-import:{commit_id}".encode()).hexdigest()
        return f"import-{digest[:32]}"

    def _ensure_intake(
        self, project_id: str, frozen: Any
    ) -> tuple[str, Path, ImportIntakeHeader]:
        project = self._workspace.get(project_id)
        ref = self._ref(str(frozen.commit_id))
        root = Path(project.path) / ".imports"
        root.mkdir(parents=True, exist_ok=True)
        directory = import_intake_dir(project.path, ref)
        with FileLock(str(root / ".create.lock")):
            manifest = directory / "manifest.json"
            if manifest.exists():
                header = read_import_header(directory)
                facts = header.pdf_preparation or {}
                if (
                    header.project_id != project_id
                    or header.storage_identity != project.storage_identity
                    or facts.get("commit_id") != frozen.commit_id
                    or facts.get("manifest_hash") != frozen.manifest_hash
                ):
                    raise ValueError("promoted PDF import identity changed")
                return ref, directory, header
            directory.mkdir(exist_ok=True)
            destination_sheet_id, sheet_name = self._destination(project, frozen)
            action = typed_action_for_request(
                {
                    "action_id": "import.files",
                    "scope": {"kind": "project"},
                    "sheet_name": sheet_name,
                    "params": {"inventory_ref": ref},
                    "output_names": {},
                    "idempotency_key": f"pdf-import:{frozen.commit_id}",
                }
            )
            envelope = reserve_typed_action_job(project, project_id, action)
            if isinstance(envelope, ActionResult):
                raise ValueError("PDF import reservation is already terminal")
            header = ImportIntakeHeader(
                project_id=project_id,
                storage_identity=project.storage_identity,
                envelope=envelope.to_json(),
                max_rows=int(frozen.committed_total),
                destination_sheet_id=destination_sheet_id,
                staged_files=True,
                pdf_preparation={
                    "commit_id": str(frozen.commit_id),
                    "draft_id": str(frozen.draft_id),
                    "manifest_hash": str(frozen.manifest_hash),
                    "selected_total": int(frozen.selected_total),
                    "ocr_policy": str(frozen.ocr_policy),
                    "ocr_action_params": dict(frozen.ocr_action_params),
                    "ocr_authorization": (
                        frozen.ocr_authorization.as_mapping()
                        if frozen.ocr_authorization is not None
                        else None
                    ),
                    "semantic_search": bool(frozen.semantic_search),
                    "semantic_setup_confirmation": frozen.semantic_setup_confirmation,
                },
            )
            write_import_header(directory, header)
            if self._text_preparation is not None:
                self._text_preparation.reserve_pdf_import(
                    project_id,
                    receipt_id=envelope.receipt_id,
                    import_ref=ref,
                )
            return ref, directory, header

    def _promote(
        self,
        project_id: str,
        draft_id: str,
        idempotency_key: str,
        directory: Path,
    ) -> None:
        self._intake.promote_commit_inventory(
            project_id,
            draft_id,
            idempotency_key=idempotency_key,
            target_path=directory / "inventory.db",
        )

    def _rollback_unpromoted(
        self,
        project_id: str,
        draft_id: str,
        idempotency_key: str,
        directory: Path,
        header: ImportIntakeHeader,
    ) -> None:
        """Restore draft ownership before releasing an unused reservation."""

        self._intake.reconcile_commit(
            project_id,
            draft_id,
            idempotency_key=idempotency_key,
            committed=False,
        )
        envelope = ActionJobEnvelope.from_json(header.envelope)
        project = self._workspace.get(project_id)
        ReceiptStore(project).delete_queued(envelope.receipt_id)
        if self._text_preparation is not None:
            self._text_preparation.abandon_pdf_import(
                project_id,
                receipt_id=envelope.receipt_id,
                import_ref=self._ref(str(header.pdf_preparation["commit_id"])),
            )
        shutil.rmtree(directory, ignore_errors=True)

    @staticmethod
    def _destination(project: Any, frozen: Any) -> tuple[int | None, str]:
        destination = frozen.destination
        if destination.get("kind") == "new":
            requested = str(destination.get("sheet_name") or "").strip()
            if not requested:
                raise ValueError("new PDF import sheet name is empty")
            return None, upload_sheet_name(
                project,
                "import.files",
                f"pdf-import:{frozen.commit_id}",
                requested,
                allocate=True,
            )
        if destination.get("kind") != "existing":
            raise ValueError("PDF import destination is invalid")
        sheet_id = int(destination["sheet_id"])
        row = project.db.execute(
            "SELECT name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        if row is None:
            raise ValueError("PDF import destination sheet is unavailable")
        return sheet_id, str(row["name"])

    @staticmethod
    def _start_reserved_receipt(
        project: Any, envelope: ActionJobEnvelope, frozen: Any
    ) -> dict[str, Any]:
        receipts = ReceiptStore(project)
        stored = receipts.find_by_id(envelope.receipt_id)
        if stored is None:
            raise ValueError("PDF import reservation is unavailable")
        if stored.status == "running":
            return
        if stored.status != "queued":
            raise ValueError("PDF import reservation is already terminal")
        receipt = stored.parsed()
        running = receipt.model_copy(
            update={
                "status": "running",
                "inputs": [
                    *receipt.inputs,
                    ReceiptIO(
                        name="pdf_import_commit",
                        ref={
                            "kind": "pdf_import_commit",
                            "commit_id": str(frozen.commit_id),
                            "draft_id": str(frozen.draft_id),
                            "manifest_hash": str(frozen.manifest_hash),
                            "selected_total": int(frozen.selected_total),
                            "ocr_policy": str(frozen.ocr_policy),
                            "ocr_action_params": dict(frozen.ocr_action_params),
                            "semantic_search": bool(frozen.semantic_search),
                        },
                    ),
                ],
            }
        )
        if not receipts.update_body_status(
            running, require_status="queued", commit=True
        ):
            raise ValueError("PDF import reservation changed")

    @staticmethod
    def _promote_page_blobs(
        project: Any,
        directory: Path,
        inventory: ImportInventory,
        *,
        after: int,
    ) -> None:
        for item in inventory.page(
            after=after, limit=_PAGE_SIZE, byte_budget=_PAGE_BYTES
        ):
            digest = str(item["sha256"])
            staged = directory / "staged" / digest
            if staged.is_file():
                observed = project.blob_store.put_path(
                    staged,
                    expected_digest=digest,
                )
                if observed != digest:
                    raise ValueError("promoted PDF staging digest changed")
                staged.unlink()
            else:
                # A crash can land after the content-addressed copy and unlink
                # but before the ordinary import page commits. Re-verify the
                # canonical owner instead of requiring a second staged copy.
                with project.blob_store.materialize(digest):
                    pass

    def _record_handoff(
        self,
        project_id: str,
        draft_id: str,
        frozen: Any,
        ref: str,
        session: ImportSession,
    ) -> None:
        if session.sheet_id is None or session.receipt_id is None:
            raise ValueError("PDF import session lost its result identity")
        result = {
            "import_ref": ref,
            "sheet_id": session.sheet_id,
            "receipt_id": session.receipt_id,
        }
        existing = getattr(frozen, "commit_result", None)
        if existing is not None:
            if dict(existing) != result:
                raise ValueError("PDF import handoff result changed")
            return result
        self._intake.record_commit_handoff(
            project_id,
            draft_id,
            idempotency_key=frozen.idempotency_key,
            result=result,
        )
        return result

    def _mark_handed_off(
        self,
        project_id: str,
        draft_id: str,
        frozen: Any,
        result: Mapping[str, Any],
    ) -> None:
        self._intake.reconcile_commit(
            project_id,
            draft_id,
            idempotency_key=frozen.idempotency_key,
            committed=True,
            result=dict(result),
        )

    def _enqueue_page(
        self, project_id: str, ref: str, through: int, sealed: bool
    ) -> int:
        return int(
            self._workspace.queue.enqueue(
                IMPORT_FILES_PAGE_KIND,
                {
                    **dict(getattr(self._workspace, "queue_payload_extra", {})),
                    "workspace_root": str(self._workspace.root),
                    "project_id": project_id,
                    "import_ref": ref,
                    "through": through,
                    "sealed": sealed,
                    "dedupe_key": import_page_dedupe_key(
                        project_id, ref, through, sealed
                    ),
                },
            )
        )

    @staticmethod
    def _result(
        frozen: Any,
        ref: str,
        session: ImportSession,
        through: int,
        sealed: bool,
    ) -> PdfImportCommitResult:
        if session.sheet_id is None or session.receipt_id is None:
            raise ValueError("PDF import session lost its result identity")
        state = session.state
        if state == "active" and session.cursor >= through and not sealed:
            state = "waiting_for_upload"
        elif state == "active":
            state = "running"
        return PdfImportCommitResult(
            commit_id=str(frozen.commit_id),
            import_ref=ref,
            receipt_id=session.receipt_id,
            sheet_id=session.sheet_id,
            state=state,
            committed_rows=session.committed_rows,
            selected_total=int(frozen.committed_total),
            available_files=through,
            uploads_complete=bool(frozen.uploads_complete),
        )


__all__ = [
    "PdfImportCommitResult",
    "PdfImportCommitService",
    "PdfImportIntake",
    "PdfImportTextPreparation",
]

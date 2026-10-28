"""Planning and durable coordination for local search preparation."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from frisket.contracts.http.search_preparation import (
    MAX_INDEXING_SHEET_NAMES,
    MAX_PDF_INDEXING_FILES,
    SearchPreparationBatch,
    SearchPreparationBatchList,
    SearchPreparationGroup,
    SearchPreparationIndexing,
    SearchPreparationJob,
    SearchPreparationModel,
    SearchPreparationPdfFile,
    SearchPreparationTextSample,
    SearchPreparationTextSamples,
)
from frisket.engine.jobs.search_index import index_work_marker
from frisket.engine.store.search_index_work import read_dirty_scopes
from frisket.engine.jobs.search_preparation_store import (
    SearchPreparationBatchRow,
    SearchPreparationChildRow,
    SearchPreparationStore,
    canonical_plan_hash,
    plan_sources,
)
from frisket.engine.jobs.model_pull import ModelPullEnqueueError, schedule_model_pull
from frisket.engine.jobs.queue import SEARCH_INDEX_KIND, SERVER_SCOPED_JOB_PAYLOAD_KEY
from frisket.engine.store.document_text import (
    document_text_role,
)
from frisket.engine.store.document_text_adoption import adopt_document_text_role
from frisket.execution.provider import ExactExecutionMatch
from frisket.execution.targets import CAPABILITY_OCR, CAPABILITY_TRANSCRIBE
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.import_intake import import_intake_dir, read_import_header
from frisket.engine.store.receipts import ReceiptStore
from frisket.project_settings import read_project_settings
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.retrieval.preparation import DEFAULT_TEXT_SPACE, PDF_PAGE_RECIPE_ID
from frisket.retrieval.query import (
    read_pdf_indexing_progress,
    read_preparation_sheet_names,
)
from frisket.server.route_errors import RouteError
from frisket.server.services.action_runs import ActionRunRouteError, ActionRunService
from frisket.server.services.pdf_import_preparation import (
    PdfImportPreparationCoordinator,
)
from frisket.server.services.pdf_imports.semantic_setup import semantic_setup_facts
from frisket.server.services.search_preparation_planning import (
    build_plan,
    capture_sources,
    sample_pdf,
)


_OCR_REF = "engine-setup:rapidocr.local@1"
_TRANSCRIBE_REF = "engine-setup:parakeet-tdt.local-onnx@1"
_LOCAL_TRANSCRIBE_TARGET = ExactExecutionMatch(
    target_id="local-onnx",
    capability=CAPABILITY_TRANSCRIBE,
    engine="parakeet-tdt",
)
_SEMANTIC_REF = "engine-setup:embeddinggemma-2.local@1"
LOG = logging.getLogger(__name__)

_RECEIPT_TO_CHILD_STATE = {
    "queued": "queued",
    "running": "running",
    "completed": "done",
    "partial": "failed",
    "failed": "failed",
    "cancelled": "cancelled",
}
_JOB_TO_CHILD_STATE = {
    "queued": "queued",
    "running": "running",
    "done": "done",
    "failed": "failed",
    "cancelled": "cancelled",
}
_ACTIVE_PROGRESS_STATES = frozenset({"queued", "running", "stalled", "orphaned"})
_PREPARATION_WARNING_LIMIT = 100
_INDEXING_SHEET_SCOPE_CANDIDATES = 64


def _lexical_indexing_sheet_names(project: Any) -> tuple[str, ...]:
    scopes = read_dirty_scopes(project.db, limit=_INDEXING_SHEET_SCOPE_CANDIDATES)
    if not scopes:
        return ()
    if scopes[0].sheet_id is None:
        column_id, _row_id = json.loads(scopes[0].scan_cursor)
        if column_id <= 0:
            return ()
        row = project.db.execute(
            "SELECT sheet.name FROM columns AS column "
            "JOIN sheets AS sheet ON sheet.id=column.sheet_id "
            "WHERE column.id=? AND column.hidden=0 AND sheet.hidden=0",
            (column_id,),
        ).fetchone()
        return () if row is None else (str(row["name"]),)
    sheet_ids: list[int] = []
    for scope in scopes:
        if scope.sheet_id is None:
            break
        if scope.sheet_id not in sheet_ids:
            sheet_ids.append(scope.sheet_id)
    placeholders = ",".join("?" for _ in sheet_ids)
    rows = project.db.execute(
        f"SELECT id,name FROM sheets WHERE hidden=0 AND id IN ({placeholders})",
        sheet_ids,
    ).fetchall()
    names = {int(row["id"]): str(row["name"]) for row in rows}
    return tuple(names[sheet_id] for sheet_id in sheet_ids if sheet_id in names)[
        :MAX_INDEXING_SHEET_NAMES
    ]


def _sha(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _requirement_dict(item: Any, *, required: bool) -> dict[str, Any]:
    return {
        "setup_ref": str(item.setup_ref),
        "capability": str(item.capability),
        "display_name": (
            "Parakeet"
            if str(item.setup_ref) == _TRANSCRIBE_REF
            else str(item.display_name)
        ),
        "purpose": {
            "ocr": "read_text",
            "transcription": "transcribe",
            "semantic_search": "search_by_meaning",
        }[str(item.capability)],
        "required": required,
        "runtime_ready": bool(item.runtime_ready),
        "weights_ready": bool(item.weights_ready),
        "download_bytes": (
            None if item.weights_ready else int(item.download_bytes_if_missing)
        ),
        "additional_runtime_download": (
            str(item.capability) == "semantic_search" and not item.runtime_ready
        ),
        "remediation": item.runtime_remediation,
        "license": str(item.license),
        "source_url": str(item.source_url),
        "artifact_refs": list(item.artifact_refs),
        "download_size_basis": str(item.download_size_basis),
        "runtime_installable": bool(item.runtime_installable),
    }


def _requirements() -> tuple[Any, ...]:
    from frisket.engine.jobs.engine_setup import local_search_setup_requirements

    return local_search_setup_requirements()


@dataclass(frozen=True, slots=True)
class _PreparationKind:
    action_id: str
    capability: str | None
    execution_target: ExactExecutionMatch | None = None


_PREPARATION_KINDS = {
    "native_pdf": _PreparationKind("media.read_pdf_text", None),
    "ocr": _PreparationKind(
        "media.ocr",
        "ocr",
        ExactExecutionMatch(
            target_id="local", capability=CAPABILITY_OCR, engine="rapidocr"
        ),
    ),
    "transcribe": _PreparationKind(
        "media.transcribe", "transcription", _LOCAL_TRANSCRIBE_TARGET
    ),
}


@dataclass(frozen=True, slots=True)
class _PlannedChild:
    kind: str
    column_id: int
    column_name: str
    items: tuple[dict[str, Any], ...]
    child_key: str

    @property
    def specification(self) -> _PreparationKind:
        return _PREPARATION_KINDS[self.kind]


@dataclass(frozen=True, slots=True)
class _SetupSnapshot:
    requirements_by_ref: Mapping[str, Any]
    pulls: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _DocumentTextTarget:
    name: str
    column_id: int


def _document_text_target(
    project: Any, *, source_column_id: int
) -> _DocumentTextTarget:
    """Return one role-backed output, creating its empty column if needed.

    Search preparation owns this destination choice. The ordinary action host
    still owns result generations and current cell heads; pre-creating the
    column lets the request fence the exact identity before a worker can run.
    """

    with project.transaction_scope():
        role = document_text_role(project, source_column_id=source_column_id)
        if role is None:
            source = project.get_column(source_column_id)
            if source is None:
                raise ValueError(
                    f"document text source column {source_column_id} does not exist"
                )
            sheet_id = int(source["sheet_id"])
            used = {
                str(column["name"]).strip().casefold()
                for column in project.columns(sheet_id, include_hidden=True)
            }
            name = "text"
            suffix = 2
            while name.strip().casefold() in used:
                name = f"text_{suffix}"
                suffix += 1
            text_column_id = project.add_column(
                sheet_id,
                name,
                type="text",
                ai_generated=True,
                commit=False,
            )
        elif not role.source_active or role.source_hidden:
            raise ValueError("designated document text source column is unavailable")
        elif not role.text_active:
            raise ValueError("designated document text column is inactive")
        adoption = adopt_document_text_role(
            project,
            source_column_id=source_column_id,
            text_column_id=role.text_column_id if role is not None else text_column_id,
        )
        role = adoption.role
        if not role.source_active or role.source_hidden:
            raise ValueError("designated document text source column is unavailable")
        if not role.text_active:
            raise ValueError("designated document text column is inactive")
        target = project.get_column(role.text_column_id)
        if target is None:
            raise ValueError(
                f"document text column {role.text_column_id} does not exist"
            )
        return _DocumentTextTarget(str(target["name"]), int(target["id"]))


def _planned_child_key(
    kind: str, column_id: int, items: Sequence[Mapping[str, Any]]
) -> str:
    identity = [
        (
            int(item["row_id"]),
            str(item["blob_hash"]),
            int(item["retrieval_revision"]),
        )
        for item in items
    ]
    return f"{kind}:{column_id}:{_sha(identity)}"


def _planned_children(plan: Mapping[str, Any]) -> tuple[_PlannedChild, ...]:
    grouped: defaultdict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for item in plan["items"]:
        grouped[(item["kind"], item["column_id"], item["column_name"])].append(item)
    return tuple(
        _PlannedChild(
            kind=kind,
            column_id=column_id,
            column_name=column_name,
            items=tuple(items),
            child_key=_planned_child_key(kind, column_id, items),
        )
        for (kind, column_id, column_name), items in grouped.items()
    )


def _adopt_planned_document_text(project: Any, plan: Mapping[str, Any]) -> None:
    """Adopt legacy prepared refs before a reviewed plan becomes authority."""

    source_column_ids = {
        child.column_id
        for child in _planned_children(plan)
        if child.kind in {"native_pdf", "ocr"}
    }
    for source_column_id in sorted(source_column_ids):
        _document_text_target(project, source_column_id=source_column_id)


class SearchPreparationService:
    """Small coordinator over existing action jobs and model-pull jobs."""

    def __init__(
        self,
        workspace: Any,
        *,
        action_runs: ActionRunService | None = None,
        setup_start: Callable[[str, str | None], int] | None = None,
        preview_runs: Any | None = None,
    ) -> None:
        self.workspace = workspace
        self.store = SearchPreparationStore(
            workspace.queue, workspace_root=str(workspace.root)
        )
        self.action_runs = action_runs or ActionRunService(workspace)
        self.setup_start = setup_start or self._start_setup
        self._split_preview_runs = preview_runs
        self._split_jobs_service = None

    def automatic_enabled(self, project_id: str) -> bool:
        project = self.workspace.get(project_id)
        return (
            self.workspace.edition != "cloud"
            and read_project_settings(project).prepare_new_files_automatically
        )

    def _setup_snapshot(self) -> _SetupSnapshot:
        from frisket.engine.jobs import model_pull_store

        requirements = tuple(_requirements())
        pulls = tuple(
            model_pull_store.list_recent(
                self.workspace.queue.engine,
                str(self.workspace.root),
                limit=100,
                persist=False,
            )
        )
        return _SetupSnapshot(
            requirements_by_ref={item.setup_ref: item for item in requirements},
            pulls=pulls,
        )

    @classmethod
    def _latest_pulls(
        cls, row: SearchPreparationBatchRow, setup: _SetupSnapshot
    ) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        for pull in setup.pulls:
            if cls._pull_relevant(row, pull):
                latest.setdefault(pull.model_ref, pull)
        return latest

    def create(
        self,
        project_id: str,
        *,
        sheet_id: int,
        row_ids: Sequence[int] | None,
        force_ocr: bool,
        may_setup_models: bool = True,
        may_run_actions: bool = True,
    ) -> SearchPreparationBatch:
        if self.workspace.edition == "cloud":
            raise RouteError(409, "local search preparation is unavailable in Cloud")
        project = self.workspace.get(project_id)
        plan = self._plan(
            project, sheet_id=sheet_id, row_ids=row_ids, force_ocr=force_ocr
        )
        if not plan["items"]:
            raise RouteError(
                409,
                "this sheet has no current media that needs search preparation",
            )
        batch = self.store.create(
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            trigger_receipt_id=None,
            sheet_id=sheet_id,
            row_ids=row_ids,
            force_ocr=force_ocr,
            plan=plan,
        )
        if (
            batch.stop_requested_at is not None
            or batch.decision == "not_now"
            or batch.errors
            or (batch.plan is not None and self._project(batch).status == "failed")
        ):
            batch = self.store.create(
                storage_org_id=self.workspace.queue_storage_org_id,
                project_id=project_id,
                trigger_receipt_id=None,
                sheet_id=sheet_id,
                row_ids=row_ids,
                force_ocr=force_ocr,
                plan=plan,
                retry_terminal=True,
            )
        return self.get(
            project_id,
            batch.id,
            may_setup_models=may_setup_models,
            may_run_actions=may_run_actions,
        )

    def _pdf_imports(self) -> PdfImportPreparationCoordinator:
        return PdfImportPreparationCoordinator(
            self.workspace,
            action_runs=self.action_runs,
            pending_store=self.store,
            document_text_target=_document_text_target,
            ensure_semantic_setup=self._ensure_pdf_semantic_setup,
        )

    def create_from_pdf_import(self, project_id: str, **kwargs: Any) -> Any:
        return self._pdf_imports().create(project_id, **kwargs)

    def reserve_pdf_import(
        self, project_id: str, *, receipt_id: str, import_ref: str
    ) -> None:
        self._pdf_imports().reserve(
            project_id, receipt_id=receipt_id, import_ref=import_ref
        )

    def abandon_pdf_import(
        self, project_id: str, *, receipt_id: str, import_ref: str
    ) -> None:
        pending = self.store.for_receipt(
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            receipt_id=receipt_id,
            include_pending=True,
        )
        if pending is None or pending.import_ref != import_ref:
            return
        self.store.mark_settled(
            pending.id,
            project_id=project_id,
            storage_org_id=self.workspace.queue_storage_org_id,
        )

    def _ensure_pdf_semantic_setup(
        self,
        project_id: str,
        *,
        import_receipt_id: str,
        enabled: bool,
        confirmation: str | None,
    ) -> None:
        if not enabled or self.workspace.edition == "cloud":
            return
        requirement = self._setup_snapshot().requirements_by_ref.get(_SEMANTIC_REF)
        if requirement is None:
            raise RouteError(409, "semantic search setup is unavailable")
        facts = semantic_setup_facts(requirement)
        if facts["ready"]:
            return
        if not facts["available"] or confirmation != facts["confirmation_hash"]:
            raise RouteError(
                409, "semantic search setup facts changed; review them again"
            )
        self.setup_start(_SEMANTIC_REF, f"pdf-import:{import_receipt_id}")

    def _split_jobs(self, project_id: str, import_ref: str):
        try:
            header = read_import_header(
                import_intake_dir(self.workspace.get(project_id).path, import_ref)
            )
        except (OSError, ValueError):
            return None
        if (header.pdf_preparation or {}).get("kind") != "pdf_split_source_pages":
            return None
        if self._split_jobs_service is None:
            from frisket.server.services.action_preview_runs import (
                ActionPreviewRunService,
            )
            from frisket.server.services.pdf_split_preparation_jobs import (
                PdfSplitPreparationJobs,
            )

            if self._split_preview_runs is None:
                self._split_preview_runs = ActionPreviewRunService(self.workspace)
            self._split_jobs_service = PdfSplitPreparationJobs(
                self.workspace, preview_runs=self._split_preview_runs
            )
        return self._split_jobs_service

    def progress_pdf_import(self, project_id: str, import_ref: str) -> int:
        # One recovery tick admits at most one bounded inventory page. The
        # durable cursor lets the normal recovery scheduler continue later.
        split = self._split_jobs(project_id, import_ref)
        if split is not None:
            project = self.workspace.get(project_id)
            session = ImportSessionStore(project).get(import_ref)
            header = read_import_header(import_intake_dir(project.path, import_ref))
            facts = header.pdf_preparation or {}
            if (
                session is not None
                and session.state in {"kept", "completed"}
                and not header.cancel_requested
            ):
                pending = self.store.for_receipt(
                    storage_org_id=self.workspace.queue_storage_org_id,
                    project_id=project_id,
                    receipt_id=str(header.envelope["receipt_id"]),
                    include_pending=True,
                )
                if pending is None:
                    self.reserve_pdf_import(
                        project_id,
                        receipt_id=str(header.envelope["receipt_id"]),
                        import_ref=import_ref,
                    )
            if (
                session is not None
                and session.state in {"kept", "completed"}
                and not header.cancel_requested
            ):
                try:
                    self._ensure_pdf_semantic_setup(
                        project_id,
                        import_receipt_id=str(header.envelope["receipt_id"]),
                        enabled=bool(facts.get("semantic_search")),
                        confirmation=facts.get("semantic_setup_confirmation"),
                    )
                except Exception as exc:  # noqa: BLE001 - independent semantic setup must not discard OCR
                    split.record_semantic_error(project_id, import_ref, str(exc))
                else:
                    split.record_semantic_error(project_id, import_ref, None)
            return split.progress(project_id, import_ref)
        return self._pdf_imports().progress(project_id, import_ref)

    def pdf_import_summary(self, project_id: str, import_ref: str) -> Any:
        split = self._split_jobs(project_id, import_ref)
        return (split or self._pdf_imports()).summary(project_id, import_ref)

    def pdf_import_child_page(
        self, project_id: str, import_ref: str, *, after: int, limit: int = 100
    ) -> Any:
        split = self._split_jobs(project_id, import_ref)
        return (split or self._pdf_imports()).child_page(
            project_id, import_ref, after=after, limit=limit
        )

    def stop_pdf_import(self, project_id: str, import_ref: str) -> Any:
        split = self._split_jobs(project_id, import_ref)
        return (split or self._pdf_imports()).stop(project_id, import_ref)

    def create_after_import(
        self,
        project_id: str,
        *,
        receipt_id: str,
        sheet_id: int,
        row_ids: Sequence[int] | None = None,
    ) -> str | None:
        existing = self.store.for_receipt(
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            receipt_id=receipt_id,
            include_pending=True,
        )
        if existing is None:
            project = self.workspace.get(project_id)
            imported = project.db.execute(
                "SELECT id FROM import_sessions WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if (
                imported is not None
                and self._split_jobs(project_id, str(imported[0])) is not None
            ):
                self.reserve_pdf_import(
                    project_id, receipt_id=receipt_id, import_ref=str(imported[0])
                )
                existing = self.store.for_receipt(
                    storage_org_id=self.workspace.queue_storage_org_id,
                    project_id=project_id,
                    receipt_id=receipt_id,
                    include_pending=True,
                )
        if existing is not None and existing.import_ref is not None:
            project = self.workspace.get(project_id)
            try:
                header = read_import_header(
                    import_intake_dir(project.path, existing.import_ref)
                )
            except (OSError, ValueError):
                header = None
            if header is not None and header.pdf_preparation is not None:
                # The ordinary import terminal callback is also the wake-up for
                # reviewed PDF preparation. Preserve that reserved cursor;
                # hydrating it as a generic automatic plan would strand OCR.
                self.progress_pdf_import(project_id, existing.import_ref)
                return existing.id
        if not self.automatic_enabled(project_id):
            if existing is not None and existing.plan is None:
                self.store.mark_settled(
                    existing.id,
                    project_id=project_id,
                    storage_org_id=self.workspace.queue_storage_org_id,
                )
            return None
        if existing is not None and existing.plan is not None:
            return existing.id
        return self._reserve_and_complete_automatic(
            project_id,
            sheet_id=sheet_id,
            row_ids=row_ids,
            pending=existing,
            receipt_id=receipt_id,
        )

    def _reserve_and_complete_automatic(
        self,
        project_id: str,
        *,
        sheet_id: int,
        row_ids: Sequence[int] | None,
        pending: SearchPreparationBatchRow | None,
        receipt_id: str | None = None,
    ) -> str | None:
        project = self.workspace.get(project_id)
        if pending is None or pending.captured_sources is None:
            sources = capture_sources(project, sheet_id=sheet_id, row_ids=row_ids)
            pending = self.store.reserve_automatic_plan(
                storage_org_id=self.workspace.queue_storage_org_id,
                project_id=project_id,
                pending=pending,
                receipt_id=receipt_id,
                sheet_id=sheet_id,
                sources=sources,
                source_cutoff=int(project.op_cursor),
            )
        return self._complete_automatic_plan(pending)

    def _complete_automatic_plan(
        self, pending: SearchPreparationBatchRow
    ) -> str | None:
        if pending.plan is not None:
            return pending.id
        if pending.stop_requested_at is not None or pending.settled_at is not None:
            return None
        if not self.automatic_enabled(pending.project_id):
            self.store.mark_settled(
                pending.id,
                project_id=pending.project_id,
                storage_org_id=pending.storage_org_id,
            )
            return None
        if pending.sheet_id is None or pending.captured_sources is None:
            return None
        project = self.workspace.get(pending.project_id)
        planning_error = None
        try:
            if any(
                not self._source_item_current(project, pending.sheet_id, item)
                for item in pending.captured_sources
            ):
                raise RouteError(
                    422, "A source file changed; prepare again to review it."
                )
            plan = self._plan(
                project,
                sheet_id=pending.sheet_id,
                row_ids=pending.row_ids,
                force_ocr=False,
                intersect_visible_rows=True,
                include_transcription=read_project_settings(
                    project
                ).transcribe_new_files_automatically,
                source_cells=frozenset(
                    (int(item["row_id"]), int(item["column_id"]))
                    for item in pending.captured_sources
                ),
            )
            captured = {
                (int(item["row_id"]), int(item["column_id"])): (
                    item["blob_hash"],
                    item["page_number"],
                    item["retrieval_revision"],
                )
                for item in pending.captured_sources
            }
            # Planning may wait on earlier PDF previews. Its returned work must
            # still refer to the saved bindings, not replacements read later.
            if any(
                captured.get((int(item["row_id"]), int(item["column_id"])))
                != (item["blob_hash"], item["page_number"], item["retrieval_revision"])
                for item in plan_sources(plan)
            ):
                raise RouteError(
                    422, "A source file changed; prepare again to review it."
                )
        except RouteError as exc:
            if exc.status_code != 422:
                raise
            planning_error = str(exc.detail)
            plan = {"force_ocr": False, "groups": [], "models": [], "items": []}
        plan["source_cutoff"] = pending.source_cutoff
        plan["admission_policy"] = "automatic_local"
        if not plan["items"] and planning_error is None:
            self.store.mark_settled(
                pending.id,
                project_id=pending.project_id,
                storage_org_id=pending.storage_org_id,
            )
            return None
        batch = self.store.hydrate_pending_plan(
            pending.id,
            project_id=pending.project_id,
            storage_org_id=pending.storage_org_id,
            sheet_id=pending.sheet_id,
            plan=plan,
            error_message=planning_error,
        )
        if batch.plan is not None and planning_error is None:
            self._advance(batch)
        return batch.id if batch.plan is not None else None

    def reserve_import(
        self, project_id: str, *, receipt_id: str, import_ref: str
    ) -> None:
        """Durably admit automatic preparation before the import worker runs."""

        if not self.automatic_enabled(project_id):
            return
        self.store.create_pending_import(
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            receipt_id=receipt_id,
            import_ref=import_ref,
        )

    def batch_for_receipt(self, project_id: str, receipt_id: str) -> str | None:
        row = self.store.for_receipt(
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            receipt_id=receipt_id,
        )
        return None if row is None else row.id

    def handoff_for_receipt(
        self, project_id: str, receipt_id: str
    ) -> tuple[Literal["pending", "available", "none"], str | None]:
        """Read durable handoff availability without advancing preparation."""
        row = self.store.for_receipt(
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            receipt_id=receipt_id,
            include_pending=True,
        )
        if row is None:
            return "none", None
        # Failed, stopped, and completed plans remain available for review,
        # even if automatic preparation has subsequently been disabled.
        if row.plan is not None:
            return "available", row.id
        if (
            row.settled_at is not None
            or row.stop_requested_at is not None
            or not self.automatic_enabled(project_id)
        ):
            return "none", None
        return "pending", None

    def create_after_materialization(
        self,
        project_id: str,
        *,
        sheet_id: int,
        row_ids: Sequence[int],
        materialization_job_id: int | None = None,
    ) -> str | None:
        pending = (
            None
            if materialization_job_id is None
            else self.store.pending_materialization(
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                job_id=materialization_job_id,
                sheet_id=sheet_id,
                row_ids=row_ids,
            )
        )
        if materialization_job_id is not None and pending is None:
            return None
        if not self.automatic_enabled(project_id):
            if pending is not None:
                self.store.mark_settled(
                    pending.id,
                    project_id=project_id,
                    storage_org_id=self.workspace.queue_storage_org_id,
                )
            return None
        return self._reserve_and_complete_automatic(
            project_id,
            sheet_id=sheet_id,
            row_ids=row_ids,
            pending=pending,
        )

    def list(
        self,
        project_id: str,
        *,
        may_setup_models: bool = True,
        may_run_actions: bool = True,
    ) -> SearchPreparationBatchList:
        project = self.workspace.get(project_id)
        setup = self._setup_snapshot()
        indexing = self._indexing_status(project_id, project)
        return SearchPreparationBatchList(
            batches=[
                self._project(
                    row,
                    may_setup_models=may_setup_models,
                    may_run_actions=may_run_actions,
                    setup=setup,
                )
                for row in self.store.list_recent(
                    project_id=project_id,
                    storage_org_id=self.workspace.queue_storage_org_id,
                )
            ],
            indexing=indexing,
        )

    def _indexing_status(
        self, project_id: str, project: Any
    ) -> SearchPreparationIndexing:
        # A completed encoder install can happen in an external worker. This
        # reconciliation only requeues preparations already recorded as
        # encoder-unavailable; ordinary dirty-work enqueue recovery remains a
        # search-boundary responsibility.
        retry_encoder = getattr(project, "_frisket_retry_retrieval_encoder", None)
        if callable(retry_encoder):
            retry_encoder()
        marker = index_work_marker(project)
        index_job = self.workspace.queue.find_job_by_refs(
            SEARCH_INDEX_KIND,
            statuses=("running",),
            project_id=project_id,
            storage_org_id=self.workspace.queue_storage_org_id,
            workspace_root=str(self.workspace.root),
        )
        if index_job is None:
            index_job = self.workspace.queue.find_job_by_refs(
                SEARCH_INDEX_KIND,
                statuses=("queued",),
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                workspace_root=str(self.workspace.root),
            )
        failed_job = None
        if marker is not None and index_job is None:
            failed_job = self.workspace.queue.find_job_by_refs(
                SEARCH_INDEX_KIND,
                statuses=("failed",),
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                workspace_root=str(self.workspace.root),
                dedupe_key=f"search-index:{marker}",
            )
        projected_job = index_job or failed_job
        indexing_status = (
            projected_job.status
            if projected_job is not None
            else "pending"
            if marker is not None
            else "idle"
        )
        pdf_files, pdf_files_truncated = (), False
        sheet_names: tuple[str, ...] = ()
        if indexing_status != "idle":
            try:
                if index_work_marker(project, include_preparation=False) is not None:
                    sheet_names = _lexical_indexing_sheet_names(project)
                else:
                    sheet_names = read_preparation_sheet_names(
                        project, limit=MAX_INDEXING_SHEET_NAMES
                    )
            except sqlite3.Error:
                # Optional context must not hide the authoritative queue state.
                pass
            try:
                pdf_files, pdf_files_truncated = read_pdf_indexing_progress(
                    project,
                    space_id=DEFAULT_TEXT_SPACE.identity,
                    recipe_id=PDF_PAGE_RECIPE_ID,
                    limit=MAX_PDF_INDEXING_FILES,
                )
            except sqlite3.Error:
                # Optional progress must not hide the authoritative queue state.
                pass
        return SearchPreparationIndexing(
            status=indexing_status,
            job_id=projected_job.id if projected_job is not None else None,
            error=failed_job.error if failed_job is not None else None,
            pdf_files=[
                SearchPreparationPdfFile(
                    source_id=file.source_id,
                    filename=file.filename,
                    completed=file.completed,
                    total=file.total,
                )
                for file in pdf_files
            ],
            pdf_files_truncated=pdf_files_truncated,
            sheet_names=list(sheet_names),
        )

    def get(
        self,
        project_id: str,
        batch_id: str,
        *,
        may_setup_models: bool = True,
        may_run_actions: bool = True,
    ) -> SearchPreparationBatch:
        try:
            row = self.store.get(
                batch_id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
            )
        except KeyError as exc:
            raise RouteError(404, "search preparation batch was not found") from exc
        if row.plan is None:
            raise RouteError(404, "search preparation batch was not found")
        return self._project(
            row,
            may_setup_models=may_setup_models,
            may_run_actions=may_run_actions,
            setup=self._setup_snapshot(),
        )

    def replan(
        self,
        project_id: str,
        batch_id: str,
        *,
        plan_hash: str,
        force_ocr: bool,
        may_setup_models: bool = True,
        may_run_actions: bool = True,
    ) -> SearchPreparationBatch:
        try:
            row = self.store.get(
                batch_id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
            )
        except KeyError as exc:
            raise RouteError(404, "search preparation batch was not found") from exc
        if row.plan is None or row.sheet_id is None:
            raise RouteError(404, "search preparation batch was not found")
        if (
            row.plan_hash != plan_hash
            or row.decision is not None
            or row.stop_requested_at is not None
            or row.errors
            or self.store.children(row.id)
        ):
            raise RouteError(
                409, "only the current unstarted preparation plan can be replaced"
            )
        project = self.workspace.get(project_id)
        if any(
            not self._source_item_current(project, row.sheet_id, item)
            for item in plan_sources(row.plan)
        ):
            raise RouteError(
                409, "a source file changed; review a new preparation plan"
            )
        source_cells = frozenset(
            (int(item["row_id"]), int(item["column_id"]))
            for item in plan_sources(row.plan)
        )
        plan = self._plan(
            project,
            sheet_id=row.sheet_id,
            row_ids=sorted({row_id for row_id, _ in source_cells}),
            force_ocr=force_ocr,
            source_cells=source_cells,
        )
        # Re-scan is a new manual review, even for an automatic import batch.
        plan["admission_policy"] = "manual_review"
        try:
            replacement = self.store.replace_undecided(
                row.id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                plan_hash=plan_hash,
                plan=plan,
            )
        except ValueError as exc:
            raise RouteError(409, str(exc)) from exc
        return self._project(
            replacement,
            may_setup_models=may_setup_models,
            may_run_actions=may_run_actions,
        )

    def decide(
        self,
        project_id: str,
        batch_id: str,
        *,
        plan_hash: str,
        decision: str,
        selected_setup_refs: Sequence[str],
        may_setup_models: bool,
        initiated_by: str | None,
        may_run_actions: bool = True,
    ) -> SearchPreparationBatch:
        try:
            row = self.store.get(
                batch_id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
            )
        except KeyError as exc:
            raise RouteError(404, "search preparation batch was not found") from exc
        if row.plan is None or row.sheet_id is None or row.plan_hash is None:
            raise RouteError(404, "search preparation batch was not found")
        if row.stop_requested_at is not None:
            raise RouteError(409, "search preparation was stopped")
        if row.errors:
            raise RouteError(409, "failed search preparation cannot be decided")
        replay_refs = tuple(sorted(set(selected_setup_refs)))
        if row.decision is not None:
            if (
                plan_hash == row.plan_hash
                and row.decision == decision
                and row.selected_setup_refs == replay_refs
            ):
                return self.get(
                    project_id,
                    batch_id,
                    may_setup_models=may_setup_models,
                    may_run_actions=may_run_actions,
                )
            raise RouteError(409, "search preparation decision is already recorded")
        if not may_run_actions:
            raise RouteError(403, "preparing search requires action permission")
        if decision != "prepare":
            raise RouteError(422, "choose Prepare or stop this preparation batch")
        project = self.workspace.get(project_id)
        _adopt_planned_document_text(project, row.plan)
        current = self._current_decision_plan(project, row)
        if (
            plan_hash != row.plan_hash
            or canonical_plan_hash(current) != row.plan_hash
            or not self._consent_facts_current(project, row)
        ):
            self._store_changed_plan_and_raise(
                row,
                current,
                may_setup_models=may_setup_models,
                may_run_actions=may_run_actions,
            )
        missing = {
            model["setup_ref"]: model
            for model in row.plan["models"]
            if not model["weights_ready"]
            or (not model["runtime_ready"] and model["runtime_installable"])
        }
        selected = set(selected_setup_refs)
        if not selected.issubset(missing):
            raise RouteError(409, "selected setup references do not match this plan")
        setup_eligible = {
            ref
            for ref, model in missing.items()
            if model["runtime_ready"] or model["runtime_installable"]
        }
        if not selected.issubset(setup_eligible):
            raise RouteError(409, "selected model setup is unavailable on this server")
        required = {
            ref
            for ref, model in missing.items()
            if model["required"]
            and (model["runtime_ready"] or model["runtime_installable"])
        }
        if decision == "prepare" and not required.issubset(selected):
            raise RouteError(
                409, "select every required model download or stop this preparation"
            )
        if selected and not may_setup_models:
            raise RouteError(
                403, "model setup requires workspace model-management permission"
            )
        try:
            row = self.store.decide(
                batch_id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                plan_hash=plan_hash,
                decision=decision,
                selected_setup_refs=selected_setup_refs,
                decided_by=initiated_by,
            )
        except ValueError as exc:
            raise RouteError(409, str(exc)) from exc
        if decision == "prepare" and selected and self.setup_start is not None:
            # The model-pull store admits only one active workspace pull.
            # Required engines run before optional semantic setup.
            self._start_next_setup(row)
        self._advance(row)
        return self.get(
            project_id,
            batch_id,
            may_setup_models=may_setup_models,
            may_run_actions=may_run_actions,
        )

    def _current_decision_plan(
        self, project: Any, row: SearchPreparationBatchRow
    ) -> dict[str, Any]:
        assert row.plan is not None and row.sheet_id is not None
        current = self._plan(
            project,
            sheet_id=row.sheet_id,
            row_ids=row.row_ids,
            force_ocr=row.force_ocr,
            source_cells=frozenset(
                (int(item["row_id"]), int(item["column_id"]))
                for item in plan_sources(row.plan)
            ),
        )
        if "admission_policy" in row.plan:
            current["admission_policy"] = row.plan["admission_policy"]
        return current

    def _store_changed_plan_and_raise(
        self,
        row: SearchPreparationBatchRow,
        current: Mapping[str, Any],
        *,
        may_setup_models: bool,
        may_run_actions: bool,
    ) -> None:
        refreshed = self.store.replan(
            row.id,
            project_id=row.project_id,
            storage_org_id=self.workspace.queue_storage_org_id,
            plan=current,
        )
        raise RouteError(
            409,
            {
                "code": "plan_changed",
                "message": "Search preparation changed; review the refreshed plan.",
                "batch": self._project(
                    refreshed,
                    may_setup_models=may_setup_models,
                    may_run_actions=may_run_actions,
                ).model_dump(mode="json", by_alias=True),
            },
        )

    @staticmethod
    def _consent_facts_current(project: Any, row: SearchPreparationBatchRow) -> bool:
        live_requirements = {item.setup_ref: item for item in _requirements()}
        for planned in row.plan["models"]:
            live = live_requirements.get(planned["setup_ref"])
            if live is None:
                return False
            current = _requirement_dict(live, required=bool(planned["required"]))
            if current != planned:
                return False
        return all(
            SearchPreparationService._source_item_current(project, row.sheet_id, item)
            for item in plan_sources(row.plan)
        )

    def stop(
        self,
        project_id: str,
        batch_id: str,
        *,
        may_setup_models: bool = True,
        may_run_actions: bool = True,
    ) -> SearchPreparationBatch:
        if not may_run_actions:
            raise RouteError(403, "stopping preparation requires action permission")
        try:
            row = self.store.request_stop(
                batch_id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
            )
        except KeyError as exc:
            raise RouteError(404, "search preparation batch was not found") from exc
        project = self.workspace.get(row.project_id)
        for child in self.store.children(row.id):
            receipt_id = child.receipt_id
            if receipt_id is None:
                receipt = self._child_action_receipt(project, row=row, child=child)
                receipt_id = receipt.id if receipt is not None else None
            if receipt_id is not None:
                self._cancel_action_receipt(row, receipt_id)
        return self._project(
            row, may_setup_models=may_setup_models, may_run_actions=may_run_actions
        )

    def _cancel_action_receipt(
        self, row: SearchPreparationBatchRow, receipt_id: str
    ) -> None:
        """Request cancellation from the ordinary action-run authority."""

        try:
            self.action_runs.cancel_prepared_action(
                row.project_id,
                receipt_id=receipt_id,
            )
        except RouteError:
            # The ordinary cancellation path records durable cancellation
            # intent before reporting live-writer or terminal races.
            return

    def setup_completed(self, setup_ref: str) -> None:
        """Wake every decided intent when the workspace setup slot becomes free."""

        del setup_ref
        self.recover()

    def _start_setup(self, setup_ref: str, initiated_by: str | None) -> int:
        from frisket.engine.jobs import model_pull_store

        try:
            pull, _created = schedule_model_pull(
                self.workspace.queue,
                workspace_root=self.workspace.root,
                model_ref=setup_ref,
                initiated_by=initiated_by,
                queue_payload_extra={SERVER_SCOPED_JOB_PAYLOAD_KEY: True},
            )
        except model_pull_store.ModelPullBusyError as exc:
            return exc.active.id
        except ModelPullEnqueueError as exc:
            raise RouteError(503, "failed to schedule model setup") from exc
        return pull.id

    def recover(self) -> None:
        """Resume durable decided intents at process startup without new consent."""

        if self.workspace.edition == "cloud":
            self._recover_cloud_pdf_imports()
            return

        after_created_at = None
        after_id = None
        while True:
            rows = self.store.list_active_page(
                storage_org_id=self.workspace.queue_storage_org_id,
                limit=100,
                after_created_at=after_created_at,
                after_id=after_id,
            )
            for row in rows:
                try:
                    if row.plan is None:
                        self._recover_pending(row)
                    else:
                        self._start_next_setup(row)
                        self._advance(row)
                except KeyError:
                    self.store.mark_settled(
                        row.id,
                        project_id=row.project_id,
                        storage_org_id=row.storage_org_id,
                    )
                except Exception as exc:
                    # One broken project or transient open must not starve the
                    # rest of this stable page. Leave it active for retry.
                    if getattr(exc, "status_code", None) in {404, 422}:
                        self.store.mark_settled(
                            row.id,
                            project_id=row.project_id,
                            storage_org_id=row.storage_org_id,
                        )
                        continue
                    LOG.warning(
                        "search preparation intent recovery failed",
                        exc_info=True,
                    )
            if len(rows) < 100:
                break
            after_created_at = rows[-1].created_at
            after_id = rows[-1].id

    def _recover_cloud_pdf_imports(self) -> None:
        """Cloud skips local automatic setup but still resumes reviewed PDF OCR."""

        after_created_at = None
        after_id = None
        while True:
            rows = self.store.list_active_page(
                storage_org_id=self.workspace.queue_storage_org_id,
                limit=100,
                after_created_at=after_created_at,
                after_id=after_id,
            )
            for row in rows:
                if row.plan is not None or row.import_ref is None:
                    continue
                try:
                    project = self.workspace.get(row.project_id)
                    header = read_import_header(
                        import_intake_dir(project.path, row.import_ref)
                    )
                    if header.pdf_preparation is not None:
                        self._recover_pending_import(row)
                except Exception:
                    LOG.warning("PDF import preparation recovery failed", exc_info=True)
            if len(rows) < 100:
                return
            after_created_at = rows[-1].created_at
            after_id = rows[-1].id

    def _recover_pending(self, row: SearchPreparationBatchRow) -> None:
        if row.materialization_job_id is not None:
            self._recover_pending_materialization(row)
            return
        self._recover_pending_import(row)

    def _recover_pending_materialization(self, row: SearchPreparationBatchRow) -> None:
        job = self.workspace.queue.get(row.materialization_job_id)
        if job is None or job.status in {"failed", "cancelled"}:
            self.store.mark_settled(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            return
        if job.status != "done":
            return
        if row.sheet_id is None or not row.row_ids:
            self.store.mark_settled(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            return
        self._reserve_and_complete_automatic(
            row.project_id,
            sheet_id=row.sheet_id,
            row_ids=row.row_ids,
            pending=row,
        )

    def _recover_pending_import(self, row: SearchPreparationBatchRow) -> None:
        if row.captured_sources is not None:
            self._complete_automatic_plan(row)
            return
        if row.import_ref is None or row.trigger_receipt_id is None:
            self.store.mark_settled(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            return
        project = self.workspace.get(row.project_id)
        session = ImportSessionStore(project).get(row.import_ref)
        if session is None:
            receipt = ReceiptStore(project).find_by_id(row.trigger_receipt_id)
            if receipt is None or receipt.status in {"cancelled", "failed"}:
                self.store.mark_settled(
                    row.id,
                    project_id=row.project_id,
                    storage_org_id=row.storage_org_id,
                )
            return
        directory = import_intake_dir(project.path, row.import_ref)
        try:
            pdf_preparation = read_import_header(directory).pdf_preparation
        except (OSError, ValueError):
            pdf_preparation = None
        if pdf_preparation is not None:
            scheduled = self.progress_pdf_import(row.project_id, row.import_ref)
            summary = self.pdf_import_summary(row.project_id, row.import_ref)
            if summary.stopped or (
                session.state not in {"active", "paused", "cancelled"}
                and scheduled >= session.cursor
            ):
                self.store.mark_settled(
                    row.id,
                    project_id=row.project_id,
                    storage_org_id=row.storage_org_id,
                )
            return
        if session.state in {"active", "paused", "cancelled"}:
            return
        if session.state == "removed" or session.sheet_id is None:
            self.store.mark_settled(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            return
        self.create_after_import(
            row.project_id,
            receipt_id=row.trigger_receipt_id,
            sheet_id=session.sheet_id,
        )

    def text_samples(
        self,
        project_id: str,
        batch_id: str,
        *,
        row_id: int | None,
        page_number: int | None,
        column_id: int | None = None,
    ) -> SearchPreparationTextSamples:
        try:
            row = self.store.get(
                batch_id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
            )
        except KeyError as exc:
            raise RouteError(404, "search preparation batch was not found") from exc
        if row.plan is None or row.sheet_id is None:
            raise RouteError(404, "search preparation batch was not found")
        project = self.workspace.get(project_id)
        samples: list[SearchPreparationTextSample] = []
        for item in row.plan.get("items", []):
            if item["mime"] != "application/pdf":
                continue
            if row_id is not None and item["row_id"] != row_id:
                continue
            if column_id is not None and item["column_id"] != column_id:
                continue
            wanted_page = page_number or item.get("page_number") or 1
            text, count, source, truncated = sample_pdf(
                project, row.sheet_id, item, wanted_page
            )
            samples.append(
                SearchPreparationTextSample(
                    row_id=item["row_id"],
                    column_id=item["column_id"],
                    filename=item.get("filename"),
                    page_number=wanted_page,
                    page_count=count,
                    preview_url=(
                        f"/api/projects/{project_id}/blobs/{item['blob_hash']}"
                        f"/pages/{wanted_page}/image"
                    ),
                    text=text,
                    source=source,
                    page_scoped=item.get("page_number") is not None,
                    truncated=truncated,
                )
            )
            if len(samples) >= 8:
                break
        if (row_id is not None or column_id is not None) and not samples:
            raise RouteError(404, "the selected PDF is not in this preparation scope")
        return SearchPreparationTextSamples(batch_id=batch_id, samples=samples)

    def _plan(
        self,
        project: Any,
        *,
        sheet_id: int,
        row_ids: Sequence[int] | None,
        force_ocr: bool,
        intersect_visible_rows: bool = False,
        source_cells: frozenset[tuple[int, int]] | None = None,
        include_transcription: bool = True,
    ) -> dict[str, Any]:
        return build_plan(
            project,
            sheet_id=sheet_id,
            row_ids=row_ids,
            force_ocr=force_ocr,
            requirements=_requirements(),
            requirement_dict=_requirement_dict,
            intersect_visible_rows=intersect_visible_rows,
            source_cells=source_cells,
            include_transcription=include_transcription,
        )

    def _automatic_transcription_disabled(self, row: SearchPreparationBatchRow) -> bool:
        # Explicitly reviewed preparation retains the user's transcription choice.
        if row.decision is not None or not row.plan:
            return False
        policy = row.plan.get("admission_policy")
        automatic = policy == "automatic_local" or (
            policy is None
            and (
                row.trigger_receipt_id is not None
                or row.materialization_job_id is not None
            )
        )
        return (
            automatic
            and not read_project_settings(
                self.workspace.get(row.project_id)
            ).transcribe_new_files_automatically
        )

    def _awaiting_decision(
        self,
        row: SearchPreparationBatchRow,
        children: Sequence[SearchPreparationChildRow],
    ) -> bool:
        if row.decision is not None or not row.plan:
            return False
        policy = row.plan.get("admission_policy")
        automatic = (
            policy == "automatic_local"
            or (
                policy is None
                and (
                    row.trigger_receipt_id is not None
                    or row.materialization_job_id is not None
                )
            )
        ) and self.automatic_enabled(row.project_id)
        review_required = not automatic or any(
            item["mime"] == "application/pdf" for item in row.plan["items"]
        )
        if not review_required:
            return False
        bound = {child.child_key for child in children if child.receipt_id is not None}
        # Existing ordinary admissions retain their authority, including the
        # enqueue/bind crash gap. An unadmitted reservation is not permission.
        return any(
            child.child_key not in bound for child in _planned_children(row.plan)
        )

    def _advance(
        self,
        row: SearchPreparationBatchRow,
        *,
        setup: _SetupSnapshot | None = None,
    ) -> None:
        if row.plan is None or row.sheet_id is None:
            return
        if row.stop_requested_at is not None or row.errors:
            self.store.mark_settled(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            return
        setup = setup or self._setup_snapshot()
        planned_by_ref = {m["setup_ref"]: m for m in row.plan["models"]}
        readiness = {}
        for live in setup.requirements_by_ref.values():
            planned = planned_by_ref.get(live.setup_ref)
            if planned is not None:
                readiness[live.capability] = _requirement_dict(
                    live, required=bool(planned["required"])
                )
        children = {child.child_key: child for child in self.store.children(row.id)}
        if self._awaiting_decision(row, tuple(children.values())):
            # Recover an action admitted before a crash between enqueue and
            # binding, without reserving or dispatching any fresh action.
            project = self.workspace.get(row.project_id)
            for child in children.values():
                if child.receipt_id is None:
                    self._adopt_existing_action_child(project, row=row, child=child)
            children = {child.child_key: child for child in self.store.children(row.id)}
            if self._awaiting_decision(row, tuple(children.values())):
                return
        transcription_disabled = self._automatic_transcription_disabled(row)
        for planned_child in _planned_children(row.plan):
            kind = planned_child.kind
            if kind == "transcribe" and transcription_disabled:
                continue
            capability = planned_child.specification.capability
            requirement = readiness.get(capability) if capability is not None else None
            if row.decision == "not_now" and capability is not None:
                continue
            if capability is not None and requirement is None:
                continue
            if requirement is not None and not (
                requirement["weights_ready"] and requirement["runtime_ready"]
            ):
                continue
            child = children.get(planned_child.child_key)
            if child is not None and child.receipt_id is not None:
                continue
            if child is None:
                try:
                    child = self.store.reserve_child(
                        row.id,
                        child_key=planned_child.child_key,
                        kind=kind,
                    )
                except ValueError:
                    # A concurrent stop owns the state transition. It is a
                    # normal reconciliation outcome, not a recovery failure.
                    current = self.store.get(
                        row.id,
                        project_id=row.project_id,
                        storage_org_id=row.storage_org_id,
                    )
                    if current.stop_requested_at is not None:
                        return
                    raise
            project = self.workspace.get(row.project_id)
            if self._adopt_existing_action_child(
                project,
                row=row,
                child=child,
            ):
                continue
            if any(
                not self._source_item_current(project, row.sheet_id, item)
                for item in planned_child.items
            ):
                self.store.append_error(
                    row.id,
                    project_id=row.project_id,
                    storage_org_id=row.storage_org_id,
                    message="A source file changed before search preparation started.",
                )
                return
            body = self._action_body(
                project,
                row,
                kind,
                planned_child.column_name,
                planned_child.items,
                planned_child.child_key,
            )
            response = self.action_runs.run_action(
                row.project_id,
                body,
                required_execution_target=planned_child.specification.execution_target,
            )
            receipt_id = response.payload.get("receipt_id")
            if not isinstance(receipt_id, str):
                errors = response.payload.get("errors")
                if self._retryable_action_errors(errors):
                    # Another process owns the ordinary action reservation.
                    # Its receipt will expose run/job identity after enqueue;
                    # the next durable reconciliation adopts it above.
                    return
                message = "Search preparation could not start."
                if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                    message = str(errors[0].get("message") or message)
                self.store.append_error(
                    row.id,
                    project_id=row.project_id,
                    storage_org_id=row.storage_org_id,
                    message=message,
                )
                return
            raw_job_id = response.payload.get("job_id")
            if isinstance(raw_job_id, bool) or not isinstance(raw_job_id, int):
                self.store.append_error(
                    row.id,
                    project_id=row.project_id,
                    storage_org_id=row.storage_org_id,
                    message="Search preparation did not return a queued job.",
                )
                return
            bound = self.store.bind_child(
                child.id,
                job_id=raw_job_id,
                receipt_id=receipt_id,
            )
            if not bound:
                # Stop raced after this batch enqueued its own action. Do not
                # cancel shared model pulls; only cancel this unbound child.
                self._cancel_action_receipt(row, receipt_id)
                return
        self._settle_if_coordinated(row, setup=setup)

    def _adopt_existing_action_child(
        self,
        project: Any,
        *,
        row: SearchPreparationBatchRow,
        child: SearchPreparationChildRow,
    ) -> bool:
        """Bind an ordinary action reservation created by a concurrent advance.

        A receipt without a run/job is still inside the action admission
        transaction. Returning true defers without manufacturing a coordinator
        failure; periodic recovery retries after that transaction publishes its
        durable job identity.
        """

        specification = _PREPARATION_KINDS[child.kind]
        admission = self.action_runs.lookup_admission(
            row.project_id,
            action_kind=specification.action_id,
            idempotency_key=f"search-preparation:{row.id}:{child.child_key}",
        )
        if admission is None:
            return False
        receipt, job = admission.receipt, admission.job
        if receipt.run_id is None:
            return True
        if job is None:
            current = self.store.get(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            if current.stop_requested_at is not None:
                return True
            response = self.action_runs.recover_prepared_action(
                row.project_id,
                receipt_id=receipt.id,
                required_execution_target=specification.execution_target,
            )
            if response is None:
                return True
            recovered_receipt_id = response.payload.get("receipt_id")
            recovered_job_id = response.payload.get("job_id")
            if (
                isinstance(recovered_receipt_id, str)
                and isinstance(recovered_job_id, int)
                and not isinstance(recovered_job_id, bool)
            ):
                bound = self.store.bind_child(
                    child.id,
                    job_id=recovered_job_id,
                    receipt_id=recovered_receipt_id,
                )
                if not bound:
                    self._cancel_action_receipt(row, recovered_receipt_id)
                return True
            errors = response.payload.get("errors")
            if self._retryable_action_errors(errors):
                return True
            message = "Search preparation could not recover its queued action."
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = str(errors[0].get("message") or message)
            self.store.append_error(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
                message=message,
            )
            return True
        bound = self.store.bind_child(
            child.id,
            job_id=job.id,
            receipt_id=receipt.id,
        )
        if not bound:
            self._cancel_action_receipt(row, receipt.id)
        return True

    @staticmethod
    def _child_action_receipt(
        project: Any,
        *,
        row: SearchPreparationBatchRow,
        child: SearchPreparationChildRow,
    ) -> Any | None:
        specification = _PREPARATION_KINDS.get(child.kind)
        if specification is None:
            return None
        return ReceiptStore(project).find_by_action_kind_and_idempotency_key(
            action_kind=specification.action_id,
            key=f"search-preparation:{row.id}:{child.child_key}",
        )

    @staticmethod
    def _retryable_action_errors(errors: object) -> bool:
        return isinstance(errors, list) and any(
            isinstance(error, dict)
            and error.get("code") in {"idempotency_in_progress", "output_column_busy"}
            for error in errors
        )

    @staticmethod
    def _pull_relevant(row: SearchPreparationBatchRow, pull: Any) -> bool:
        epoch = row.decision_at or row.created_at
        return (
            pull.created_at >= epoch
            or pull.status in {"pending", "running"}
            or (pull.finished_at is not None and pull.finished_at >= epoch)
        )

    def _settle_if_coordinated(
        self,
        row: SearchPreparationBatchRow,
        *,
        setup: _SetupSnapshot | None = None,
    ) -> None:
        if self._awaiting_decision(row, self.store.children(row.id)):
            return
        setup = setup or self._setup_snapshot()
        live = setup.requirements_by_ref
        latest = self._latest_pulls(row, setup)
        transcription_disabled = self._automatic_transcription_disabled(row)
        for planned in row.plan["models"]:
            if planned["capability"] == "transcription" and transcription_disabled:
                continue
            requirement = live.get(planned["setup_ref"])
            if requirement is None:
                if planned["required"]:
                    self.store.mark_settled(
                        row.id,
                        project_id=row.project_id,
                        storage_org_id=row.storage_org_id,
                    )
                    return
                continue
            ready = requirement is not None and (
                requirement.weights_ready and requirement.runtime_ready
            )
            if ready:
                continue
            if planned["required"] and row.decision is None:
                # Keep an undecided automatic intent recoverable. The model
                # may become ready through another explicitly consented pull.
                return
            if requirement is not None and (
                not requirement.runtime_ready and not requirement.runtime_installable
            ):
                continue
            pull = latest.get(planned["setup_ref"])
            if planned["setup_ref"] in row.selected_setup_refs and pull is not None:
                if pull.status in {"failed", "cancelled"}:
                    self.store.mark_settled(
                        row.id,
                        project_id=row.project_id,
                        storage_org_id=row.storage_org_id,
                    )
                    return
            if row.decision == "prepare" and (
                planned["required"] or planned["setup_ref"] in row.selected_setup_refs
            ):
                return
        capability_ready = {
            str(requirement.capability): bool(
                requirement.weights_ready and requirement.runtime_ready
            )
            for requirement in live.values()
        }
        expected = set()
        for planned_child in _planned_children(row.plan):
            if planned_child.kind == "transcribe" and transcription_disabled:
                continue
            capability = planned_child.specification.capability
            if capability is not None and not capability_ready.get(capability, False):
                continue
            if row.decision == "not_now" and capability is not None:
                continue
            expected.add(planned_child.child_key)
        children = {child.child_key: child for child in self.store.children(row.id)}
        if all(
            key in children
            and children[key].receipt_id is not None
            and children[key].job_id is not None
            for key in expected
        ):
            self.store.mark_settled(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )

    @staticmethod
    def _source_item_current(
        project: Any, sheet_id: int, item: Mapping[str, Any]
    ) -> bool:
        source = resolve_cell_blob_source(
            project,
            sheet_id=sheet_id,
            row_id=int(item["row_id"]),
            column_id=int(item["column_id"]),
        )
        return source is not None and (
            source.blob_hash == item["blob_hash"]
            and source.page_number == item["page_number"]
            and source.binding.retrieval_revision == item["retrieval_revision"]
        )

    def _action_body(
        self,
        project: Any,
        row: SearchPreparationBatchRow,
        kind: str,
        column_name: str,
        items: Sequence[Mapping[str, Any]],
        child_key: str,
    ) -> dict[str, Any]:
        specification = _PREPARATION_KINDS[kind]
        row_ids = sorted(item["row_id"] for item in items)
        source_column_ids = {int(item["column_id"]) for item in items}
        if len(source_column_ids) != 1:
            raise ValueError("search preparation child must use one source column")
        source_column_id = next(iter(source_column_ids))
        text_target = (
            _document_text_target(project, source_column_id=source_column_id)
            if kind in {"native_pdf", "ocr"}
            else None
        )
        common = {
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": row.sheet_id,
                "row_ids": row_ids,
            },
            "idempotency_key": f"search-preparation:{row.id}:{child_key}",
        }
        columns = {
            str(column["name"]): int(column["id"])
            for column in project.columns(row.sheet_id, include_hidden=True)
        }

        def replace_owned_outputs(body: dict[str, Any]) -> dict[str, Any]:
            body["replace_existing"] = True
            body["expected_output_columns"] = {
                name: columns.get(name) for name in body["output_names"].values()
            }
            if text_target is not None:
                body["expected_output_columns"][text_target.name] = (
                    text_target.column_id
                )
            return body

        if kind == "native_pdf":
            assert text_target is not None
            return replace_owned_outputs(
                {
                    **common,
                    "action_id": specification.action_id,
                    "params": {"source": column_name},
                    "output_names": {"text": text_target.name},
                }
            )
        if kind == "ocr":
            assert text_target is not None
            body = {
                **common,
                "action_id": specification.action_id,
                "params": {"source": column_name, "engine": "rapidocr"},
                "output_names": {
                    "text": text_target.name,
                    "blocks": f"Search positions · {column_name}",
                },
            }
            return replace_owned_outputs(body)
        return replace_owned_outputs(
            {
                **common,
                "action_id": specification.action_id,
                "params": {"source": column_name, "engine": "parakeet-tdt"},
                "output_names": {
                    "text": f"Transcript · {column_name}",
                    "segments": f"Transcript segments · {column_name}",
                },
            }
        )

    def _start_next_setup(
        self,
        row: SearchPreparationBatchRow,
        *,
        setup: _SetupSnapshot | None = None,
    ) -> None:
        if self.setup_start is None or row.decision != "prepare":
            return
        setup = setup or self._setup_snapshot()
        requirements = setup.requirements_by_ref
        latest = self._latest_pulls(row, setup)
        planned = {item["setup_ref"]: item for item in row.plan["models"]}
        ordered = sorted(
            row.selected_setup_refs,
            key=lambda ref: (not bool(planned.get(ref, {}).get("required")), ref),
        )
        for ref in ordered:
            requirement = requirements.get(ref)
            if requirement is not None and (
                not requirement.weights_ready
                or (not requirement.runtime_ready and requirement.runtime_installable)
            ):
                pull = latest.get(ref)
                if pull is not None and pull.status in {"failed", "cancelled"}:
                    return
                self.setup_start(ref, row.decided_by)
                return

    def _project(
        self,
        row: SearchPreparationBatchRow,
        *,
        may_setup_models: bool = True,
        may_run_actions: bool = True,
        setup: _SetupSnapshot | None = None,
    ) -> SearchPreparationBatch:
        setup = setup or self._setup_snapshot()
        planned_children = tuple(
            child
            for child in _planned_children(row.plan)
            if row.decision != "not_now" or child.specification.capability is None
        )
        expected_keys = {child.child_key for child in planned_children}
        children = [
            child
            for child in self.store.children(row.id)
            if child.child_key in expected_keys
        ]
        awaiting_decision = self._awaiting_decision(row, children)
        project = self.workspace.get(row.project_id)
        receipts = ReceiptStore(project)
        jobs: list[SearchPreparationJob] = []
        child_states: dict[int, str] = {}
        projection_errors = list(row.errors)
        projection_warnings: dict[str, None] = dict.fromkeys(
            row.plan.get("warnings", [])
        )
        for child in children:
            stored = (
                receipts.find_by_id(child.receipt_id)
                if child.receipt_id is not None
                else None
            )
            state = (
                "planning"
                if child.receipt_id is None
                else "failed"
                if stored is None
                else _RECEIPT_TO_CHILD_STATE.get(stored.status, "failed")
            )
            progress: Mapping[str, Any] | None = None
            if (
                child.job_id is not None
                and stored is not None
                and stored.status in {"queued", "running"}
            ):
                try:
                    live_job = self.action_runs.job_detail(row.project_id, child.job_id)
                except ActionRunRouteError:
                    # The receipt remains the durable fallback if the queue job
                    # was removed or belongs to a retired execution backend.
                    pass
                else:
                    candidate = live_job.get("progress")
                    if isinstance(candidate, Mapping):
                        progress = candidate
                        progress_status = str(candidate.get("status") or "")
                        if progress_status in _ACTIVE_PROGRESS_STATES:
                            state = (
                                "queued" if progress_status == "queued" else "running"
                            )
                        elif progress_status in {"failed", "partial"}:
                            state = "failed"
                        elif progress_status == "cancelled":
                            state = "cancelled"
                        elif progress_status == "completed":
                            state = (
                                "failed"
                                if int(candidate.get("failed") or 0) > 0
                                else "done"
                            )
                    else:
                        state = _JOB_TO_CHILD_STATE.get(
                            str(live_job.get("status") or ""), state
                        )
            child_states[child.id] = state
            if stored is not None and state in {"done", "failed", "cancelled"}:
                try:
                    receipt = stored.parsed()
                except Exception:
                    if state != "done":
                        projection_errors.append("Search preparation action failed.")
                else:
                    for warning in receipt.warnings:
                        if len(projection_warnings) > _PREPARATION_WARNING_LIMIT:
                            break
                        projection_warnings.setdefault(warning, None)
                    if state != "done":
                        projection_errors.extend(
                            error.message for error in receipt.errors if error.message
                        )
            if child.job_id is not None and state != "planning":
                jobs.append(
                    SearchPreparationJob(
                        job_id=child.job_id,
                        status=state,
                        action_kind=child.kind,
                        progress=progress,
                    )
                )
        current = setup.requirements_by_ref
        pulls = self._latest_pulls(row, setup)
        models: list[SearchPreparationModel] = []
        missing_required = False
        unavailable_required = False
        setup_failed = False
        setup_active = False
        for planned in row.plan["models"]:
            live = current.get(planned["setup_ref"])
            weights_ready = bool(live.weights_ready) if live is not None else False
            runtime_ready = bool(live.runtime_ready) if live is not None else False
            ready = weights_ready and runtime_ready
            setup_eligible = live is not None and (
                runtime_ready or bool(planned["runtime_installable"])
            )
            missing_required = missing_required or (
                planned["required"] and not ready and setup_eligible
            )
            unavailable_required = unavailable_required or (
                planned["required"] and not ready and not setup_eligible
            )
            pull = (
                pulls.get(planned["setup_ref"])
                if planned["setup_ref"] in row.selected_setup_refs
                else None
            )
            if pull is not None and planned["setup_ref"] in row.selected_setup_refs:
                setup_active = setup_active or pull.status in {"pending", "running"}
                if pull.status in {"failed", "cancelled"} and not ready:
                    setup_failed = True
                    if pull.error_message:
                        projection_errors.append(pull.error_message)
            models.append(
                SearchPreparationModel(
                    setup_ref=planned["setup_ref"],
                    display_name=planned["display_name"],
                    purpose=planned["purpose"],
                    required=planned["required"],
                    ready=ready,
                    runtime_ready=runtime_ready,
                    can_setup=may_setup_models and not ready and setup_eligible,
                    download_bytes=None if weights_ready else planned["download_bytes"],
                    additional_runtime_download=planned["additional_runtime_download"],
                    remediation=planned["remediation"],
                    license=planned["license"],
                    source_url=planned["source_url"],
                    pull_id=None if pull is None else pull.id,
                )
            )
        job_statuses = set(child_states.values())
        all_children = len(children) == len(expected_keys)
        indexing = index_work_marker(project, include_preparation=False) is not None
        if row.stop_requested_at is not None:
            status = "stopped"
        elif row.errors or setup_failed or job_statuses & {"failed", "cancelled"}:
            status = "failed"
        elif awaiting_decision or (missing_required and row.decision is None):
            status = "needs_consent"
        elif row.decision != "not_now" and (missing_required or setup_active):
            status = "preparing"
        elif unavailable_required:
            status = "failed"
        elif (
            not all_children
            or job_statuses & {"planning", "queued", "running"}
            or indexing
        ):
            status = "preparing"
        elif all_children and job_statuses <= {"done"}:
            status = "complete"
        else:
            status = "planning"
        groups = []
        for group in row.plan["groups"]:
            kind_children = [c for c in children if c.kind == group["kind"]]
            kind_states = {child_states[c.id] for c in kind_children}
            if row.stop_requested_at is not None:
                group_status = "stopped"
            elif row.decision == "not_now" and group["kind"] != "native_pdf":
                group_status = "not_prepared"
            elif any(
                model.required
                and not model.ready
                and not model.can_setup
                and (
                    (group["kind"] == "ocr" and model.purpose == "read_text")
                    or (group["kind"] == "transcribe" and model.purpose == "transcribe")
                )
                for model in models
            ):
                group_status = "not_prepared"
            elif kind_states & {"failed", "cancelled"}:
                group_status = "failed"
            elif not kind_children and status == "needs_consent":
                group_status = "needs_consent"
            elif not kind_children and setup_active:
                group_status = "waiting_for_download"
            elif kind_children and kind_states == {"done"}:
                group_status = "complete"
            elif kind_states & {"running"}:
                group_status = "running"
            elif kind_states & {"queued"}:
                group_status = "queued"
            elif kind_children:
                group_status = "planning"
            else:
                group_status = "planning"
            groups.append(SearchPreparationGroup(**group, status=group_status))
        warnings = list(projection_warnings)
        if len(warnings) > _PREPARATION_WARNING_LIMIT:
            warnings = [
                *warnings[: _PREPARATION_WARNING_LIMIT - 1],
                "Additional preparation warnings omitted.",
            ]
        return SearchPreparationBatch(
            id=row.id,
            sheet_id=row.sheet_id,
            plan_hash=row.plan_hash,
            force_ocr=row.force_ocr,
            status=status,
            groups=groups,
            models=models,
            jobs=jobs,
            can_stop=may_run_actions and status in {"preparing", "needs_consent"},
            can_setup=may_setup_models
            and status == "needs_consent"
            and any(m.can_setup for m in models),
            errors=list(dict.fromkeys(projection_errors)),
            warnings=warnings,
        )


__all__ = ["SearchPreparationService"]

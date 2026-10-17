"""Planning and durable coordination for local search preparation."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from frisket.contracts.http.search_preparation import (
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
from frisket.engine.jobs.search_preparation_store import (
    SearchPreparationBatchRow,
    SearchPreparationChildRow,
    SearchPreparationStore,
    plan_sources,
)
from frisket.engine.jobs.model_pull import ModelPullEnqueueError, schedule_model_pull
from frisket.engine.jobs.queue import SEARCH_INDEX_KIND, SERVER_SCOPED_JOB_PAYLOAD_KEY
from frisket.execution.provider import ExactExecutionMatch
from frisket.execution.targets import CAPABILITY_TRANSCRIBE
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.project_settings import read_project_settings
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.retrieval.preparation import DEFAULT_TEXT_SPACE, PDF_PAGE_RECIPE_ID
from frisket.retrieval.query import read_pdf_indexing_progress
from frisket.server.route_errors import RouteError
from frisket.server.services.action_runs import ActionRunRouteError, ActionRunService
from frisket.server.services.search_preparation_planning import build_plan, sample_pdf


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


_PREPARATION_KINDS = {
    "native_pdf": _PreparationKind("media.read_pdf_text", None),
    "ocr": _PreparationKind("media.ocr", "ocr"),
    "transcribe": _PreparationKind("media.transcribe", "transcription"),
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


def _awaiting_pdf_review(
    row: SearchPreparationBatchRow,
    children: Sequence[SearchPreparationChildRow],
) -> bool:
    if (
        row.decision is not None
        or not row.plan
        or not any(item["mime"] == "application/pdf" for item in row.plan["items"])
    ):
        return False
    bound = {child.child_key for child in children if child.receipt_id is not None}
    # Previously admitted actions keep their authority after upgrading. Fresh
    # work, including mixed-file batches, waits for the exact-plan decision.
    return any(child.child_key not in bound for child in _planned_children(row.plan))


class SearchPreparationService:
    """Small coordinator over existing action jobs and model-pull jobs."""

    def __init__(
        self,
        workspace: Any,
        *,
        action_runs: ActionRunService | None = None,
        setup_start: Callable[[str, str | None], int] | None = None,
    ) -> None:
        self.workspace = workspace
        self.store = SearchPreparationStore(
            workspace.queue, workspace_root=str(workspace.root)
        )
        self.action_runs = action_runs or ActionRunService(workspace)
        self.setup_start = setup_start or self._start_setup

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
        trigger_receipt_id: str | None = None,
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
            trigger_receipt_id=trigger_receipt_id,
            sheet_id=sheet_id,
            row_ids=row_ids,
            force_ocr=force_ocr,
            plan=plan,
        )
        if trigger_receipt_id is None and (
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
        self._advance(batch)
        return self.get(project_id, batch.id)

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
        if not self.automatic_enabled(project_id):
            if existing is not None and existing.plan is None:
                self.store.mark_settled(
                    existing.id,
                    project_id=project_id,
                    storage_org_id=self.workspace.queue_storage_org_id,
                )
            return None
        project = self.workspace.get(project_id)
        if existing is not None and existing.plan is not None:
            return existing.id
        planning_error = None
        try:
            plan = self._plan(
                project,
                sheet_id=sheet_id,
                row_ids=row_ids,
                force_ocr=False,
                intersect_visible_rows=True,
            )
        except RouteError as exc:
            if exc.status_code != 422:
                raise
            # The files are already committed. Publish a failed preparation
            # outcome instead of making a deterministic preview error look like
            # a still-running import. Other failures retain their retry path.
            planning_error = str(exc.detail)
            plan = {
                "source_cutoff": int(project.op_cursor),
                "force_ocr": False,
                "groups": [],
                "models": [],
                "items": [],
            }
        if not plan["items"] and planning_error is None:
            if existing is not None:
                self.store.mark_settled(
                    existing.id,
                    project_id=project_id,
                    storage_org_id=self.workspace.queue_storage_org_id,
                )
            return None
        if existing is not None:
            batch = self.store.hydrate_pending_import(
                existing.id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                sheet_id=sheet_id,
                plan=plan,
                error_message=planning_error,
            )
        else:
            batch = self.store.create(
                storage_org_id=self.workspace.queue_storage_org_id,
                project_id=project_id,
                trigger_receipt_id=receipt_id,
                sheet_id=sheet_id,
                row_ids=row_ids,
                force_ocr=False,
                plan=plan,
                error_message=planning_error,
            )
        if planning_error is None:
            self._advance(batch)
        return batch.id

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
        project = self.workspace.get(project_id)
        plan = self._plan(
            project,
            sheet_id=sheet_id,
            row_ids=row_ids,
            force_ocr=False,
            intersect_visible_rows=True,
        )
        if not plan["items"]:
            if pending is not None:
                self.store.mark_settled(
                    pending.id,
                    project_id=project_id,
                    storage_org_id=self.workspace.queue_storage_org_id,
                )
            return None
        if pending is not None:
            batch = self.store.hydrate_pending_materialization(
                pending.id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                plan=plan,
            )
            self._advance(batch)
            return batch.id
        batch = self.store.create(
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            trigger_receipt_id=None,
            sheet_id=sheet_id,
            row_ids=row_ids,
            force_ocr=False,
            plan=plan,
        )
        self._advance(batch)
        return batch.id

    def list(
        self, project_id: str, *, may_setup_models: bool = True
    ) -> SearchPreparationBatchList:
        project = self.workspace.get(project_id)
        setup = self._setup_snapshot()
        indexing = self._indexing_status(project_id, project)
        return SearchPreparationBatchList(
            batches=[
                self._project(
                    row,
                    may_setup_models=may_setup_models,
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
        if indexing_status != "idle":
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
        )

    def get(
        self, project_id: str, batch_id: str, *, may_setup_models: bool = True
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
            setup=self._setup_snapshot(),
        )

    def replan(
        self,
        project_id: str,
        batch_id: str,
        *,
        plan_hash: str,
        force_ocr: bool,
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
        return self._project(replacement)

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
                return self.get(project_id, batch_id)
            raise RouteError(409, "search preparation decision is already recorded")
        project = self.workspace.get(project_id)
        if plan_hash != row.plan_hash or not self._consent_facts_current(project, row):
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
            refreshed = self.store.replan(
                row.id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                plan=current,
            )
            raise RouteError(
                409,
                {
                    "code": "plan_changed",
                    "message": "Search preparation changed; review the refreshed plan.",
                    "batch": self._project(refreshed).model_dump(
                        mode="json", by_alias=True
                    ),
                },
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
                409, "select every required model download or choose Not now"
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
        return self.get(project_id, batch_id)

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
        for item in plan_sources(row.plan):
            source = resolve_cell_blob_source(
                project,
                sheet_id=row.sheet_id,
                row_id=int(item["row_id"]),
                column_id=int(item["column_id"]),
            )
            if source is None or (
                source.blob_hash != item["blob_hash"]
                or source.page_number != item["page_number"]
                or source.binding.retrieval_revision != item["retrieval_revision"]
            ):
                return False
        return True

    def stop(self, project_id: str, batch_id: str) -> SearchPreparationBatch:
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
        return self._project(row)

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
        self.create_after_materialization(
            row.project_id,
            sheet_id=row.sheet_id,
            row_ids=row.row_ids,
            materialization_job_id=row.materialization_job_id,
        )

    def _recover_pending_import(self, row: SearchPreparationBatchRow) -> None:
        if row.import_ref is None or row.trigger_receipt_id is None:
            self.store.mark_settled(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            return
        project = self.workspace.get(row.project_id)
        if not self.automatic_enabled(row.project_id):
            self.store.mark_settled(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            return
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
        if row_id is not None and not samples:
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
        if _awaiting_pdf_review(row, tuple(children.values())):
            # Recover an action admitted before a crash between enqueue and
            # binding, without reserving or dispatching any fresh action.
            project = self.workspace.get(row.project_id)
            for child in children.values():
                if child.receipt_id is None:
                    self._adopt_existing_action_child(project, row=row, child=child)
            children = {child.child_key: child for child in self.store.children(row.id)}
            if _awaiting_pdf_review(row, tuple(children.values())):
                return
        for planned_child in _planned_children(row.plan):
            kind = planned_child.kind
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
            if kind == "transcribe":
                response = self.action_runs.run_action(
                    row.project_id,
                    body,
                    required_execution_target=_LOCAL_TRANSCRIBE_TARGET,
                )
            else:
                response = self.action_runs.run_action(row.project_id, body)
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

        receipt = self._child_action_receipt(project, row=row, child=child)
        if receipt is None:
            return False
        if receipt.run_id is None:
            return True
        job = self.workspace.queue.get_project_run_job(
            row.project_id,
            receipt.run_id,
            storage_org_id=row.storage_org_id,
        )
        if job is None:
            current = self.store.get(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            if current.stop_requested_at is not None:
                return True
            if child.kind == "transcribe":
                response = self.action_runs.recover_prepared_action(
                    row.project_id,
                    receipt_id=receipt.id,
                    required_execution_target=_LOCAL_TRANSCRIBE_TARGET,
                )
            else:
                response = self.action_runs.recover_prepared_action(
                    row.project_id,
                    receipt_id=receipt.id,
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
        if _awaiting_pdf_review(row, self.store.children(row.id)):
            return
        setup = setup or self._setup_snapshot()
        live = setup.requirements_by_ref
        latest = self._latest_pulls(row, setup)
        for planned in row.plan["models"]:
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
            return body

        if kind == "native_pdf":
            return replace_owned_outputs(
                {
                    **common,
                    "action_id": specification.action_id,
                    "params": {"source": column_name},
                    "output_names": {"text": f"Search text · {column_name}"},
                }
            )
        if kind == "ocr":
            text_name = (
                f"Search text · {column_name}"
                if row.force_ocr
                else f"OCR text · {column_name}"
            )
            body = {
                **common,
                "action_id": specification.action_id,
                "params": {"source": column_name, "engine": "rapidocr"},
                "output_names": {
                    "text": text_name,
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
        awaiting_pdf_review = _awaiting_pdf_review(row, children)
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
                    can_setup=not ready and setup_eligible,
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
        elif awaiting_pdf_review or (missing_required and row.decision is None):
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
            can_stop=status in {"preparing", "needs_consent"},
            can_setup=may_setup_models
            and status == "needs_consent"
            and any(m.required and not m.ready and m.can_setup for m in models),
            errors=list(dict.fromkeys(projection_errors)),
            warnings=warnings,
        )


__all__ = ["SearchPreparationService"]

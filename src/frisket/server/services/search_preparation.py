"""Planning and durable coordination for local search preparation."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from frisket.contracts.http.search_preparation import (
    SearchPreparationBatch,
    SearchPreparationBatchList,
    SearchPreparationGroup,
    SearchPreparationJob,
    SearchPreparationModel,
    SearchPreparationTextSample,
    SearchPreparationTextSamples,
)
from frisket.engine.jobs.search_index import index_work_marker
from frisket.engine.jobs.search_preparation_store import (
    SearchPreparationBatchRow,
    SearchPreparationChildRow,
    SearchPreparationStore,
)
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.project_settings import read_project_settings
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.server.route_errors import RouteError
from frisket.server.services.action_runs import ActionRunService
from frisket.server.services.search_preparation_planning import build_plan, sample_pdf


_OCR_REF = "engine-setup:rapidocr.local@1"
_TRANSCRIBE_REF = "engine-setup:parakeet-tdt.local-onnx@1"
_SEMANTIC_REF = "engine-setup:embeddinggemma-2.local@1"
LOG = logging.getLogger(__name__)


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
        batch = self.store.create(
            workspace_root=str(self.workspace.root),
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            trigger_receipt_id=trigger_receipt_id,
            sheet_id=sheet_id,
            row_ids=row_ids,
            source_cutoff=int(project.op_cursor),
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
                workspace_root=str(self.workspace.root),
                storage_org_id=self.workspace.queue_storage_org_id,
                project_id=project_id,
                trigger_receipt_id=None,
                sheet_id=sheet_id,
                row_ids=row_ids,
                source_cutoff=int(project.op_cursor),
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
            workspace_root=str(self.workspace.root),
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
        plan = self._plan(project, sheet_id=sheet_id, row_ids=row_ids, force_ocr=False)
        if not plan["items"]:
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
                source_cutoff=int(project.op_cursor),
                plan=plan,
            )
            self._advance(batch)
            return batch.id
        batch = self.store.create(
            workspace_root=str(self.workspace.root),
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            trigger_receipt_id=receipt_id,
            sheet_id=sheet_id,
            row_ids=row_ids,
            source_cutoff=int(project.op_cursor),
            force_ocr=False,
            plan=plan,
        )
        self._advance(batch)
        return batch.id

    def reserve_import(
        self, project_id: str, *, receipt_id: str, import_ref: str
    ) -> None:
        """Durably admit automatic preparation before the import worker runs."""

        if not self.automatic_enabled(project_id):
            return
        self.store.create_pending_import(
            workspace_root=str(self.workspace.root),
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            receipt_id=receipt_id,
            import_ref=import_ref,
        )

    def batch_for_receipt(self, project_id: str, receipt_id: str) -> str | None:
        row = self.store.for_receipt(
            workspace_root=str(self.workspace.root),
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            receipt_id=receipt_id,
        )
        return None if row is None else row.id

    def create_after_materialization(
        self, project_id: str, *, sheet_id: int, row_ids: Sequence[int]
    ) -> str | None:
        pending = self.store.pending_materialization(
            project_id=project_id,
            storage_org_id=self.workspace.queue_storage_org_id,
            sheet_id=sheet_id,
            row_ids=row_ids,
        )
        if not self.automatic_enabled(project_id):
            if pending is not None:
                self.store.mark_settled(
                    pending.id,
                    project_id=project_id,
                    storage_org_id=self.workspace.queue_storage_org_id,
                )
            return None
        project = self.workspace.get(project_id)
        plan = self._plan(project, sheet_id=sheet_id, row_ids=row_ids, force_ocr=False)
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
                source_cutoff=int(project.op_cursor),
                plan=plan,
            )
            self._advance(batch)
            return batch.id
        batch = self.store.create(
            workspace_root=str(self.workspace.root),
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            trigger_receipt_id=None,
            sheet_id=sheet_id,
            row_ids=row_ids,
            source_cutoff=int(project.op_cursor),
            force_ocr=False,
            plan=plan,
        )
        self._advance(batch)
        return batch.id

    def list(
        self, project_id: str, *, may_setup_models: bool = True
    ) -> SearchPreparationBatchList:
        return SearchPreparationBatchList(
            batches=[
                self._project(row, may_setup_models=may_setup_models)
                for row in self.store.list_recent(
                    project_id=project_id,
                    storage_org_id=self.workspace.queue_storage_org_id,
                )
            ]
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
        return self._project(row, may_setup_models=may_setup_models)

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
        project = self.workspace.get(project_id)
        if plan_hash != row.plan_hash or not self._consent_facts_current(project, row):
            current = self._plan(
                project,
                sheet_id=row.sheet_id,
                row_ids=row.row_ids,
                force_ocr=row.force_ocr,
            )
            refreshed = self.store.replan(
                row.id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                source_cutoff=int(project.op_cursor),
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
            )
        except ValueError as exc:
            raise RouteError(409, str(exc)) from exc
        if decision == "prepare" and selected and self.setup_start is not None:
            # The model-pull store admits only one active workspace pull.
            # Required engines run before optional semantic setup.
            self._start_next_setup(row, initiated_by=initiated_by)
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
        for item in row.plan["items"]:
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
        for child in self.store.children(row.id):
            if child.receipt_id is not None:
                self._cancel_action_receipt(row, child.receipt_id)
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
        from frisket.engine.jobs.model_pull import MODEL_PULL_MAX_ATTEMPTS
        from frisket.engine.jobs.queue import (
            MODEL_PULL_KIND,
            SERVER_SCOPED_JOB_PAYLOAD_KEY,
        )

        try:
            pull, created = model_pull_store.create_or_get_active(
                self.workspace.queue.engine,
                workspace_root=str(self.workspace.root),
                model_ref=setup_ref,
                initiated_by=initiated_by,
            )
        except model_pull_store.ModelPullBusyError as exc:
            return exc.active.id
        if not created:
            return pull.id
        try:
            job_id = self.workspace.queue.enqueue(
                MODEL_PULL_KIND,
                {
                    "pull_id": pull.id,
                    "workspace_root": str(self.workspace.root),
                    SERVER_SCOPED_JOB_PAYLOAD_KEY: True,
                },
                max_attempts=MODEL_PULL_MAX_ATTEMPTS,
            )
        except Exception as exc:
            model_pull_store.mark_failed(
                self.workspace.queue.engine,
                pull.id,
                error_code="enqueue_failed",
                error_message=f"failed to enqueue engine setup ({type(exc).__name__})",
            )
            raise RouteError(503, "failed to schedule model setup") from exc
        model_pull_store.set_job_id(self.workspace.queue.engine, pull.id, job_id=job_id)
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
                    if getattr(exc, "status_code", None) == 404:
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
            text, count, source = sample_pdf(project, row.sheet_id, item, wanted_page)
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
                    text=text[:12000],
                    source=source,
                    truncated=len(text) > 12000,
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
    ) -> dict[str, Any]:
        return build_plan(
            project,
            sheet_id=sheet_id,
            row_ids=row_ids,
            force_ocr=force_ocr,
            requirements=_requirements(),
            requirement_dict=_requirement_dict,
        )

    def _advance(self, row: SearchPreparationBatchRow) -> None:
        if row.plan is None or row.sheet_id is None:
            return
        if row.stop_requested_at is not None or row.errors:
            self.store.mark_settled(
                row.id,
                project_id=row.project_id,
                storage_org_id=row.storage_org_id,
            )
            return
        planned_by_ref = {m["setup_ref"]: m for m in row.plan["models"]}
        readiness = {}
        for live in _requirements():
            planned = planned_by_ref.get(live.setup_ref)
            if planned is not None:
                readiness[live.capability] = _requirement_dict(
                    live, required=bool(planned["required"])
                )
        grouped: defaultdict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(
            list
        )
        for item in row.plan["items"]:
            grouped[(item["kind"], item["column_id"], item["column_name"])].append(item)
        children = {child.child_key: child for child in self.store.children(row.id)}
        cursor = row.cursor
        for (kind, column_id, column_name), items in grouped.items():
            capability = (
                "ocr"
                if kind == "ocr"
                else "transcription"
                if kind == "transcribe"
                else None
            )
            requirement = readiness.get(capability) if capability is not None else None
            if row.decision == "not_now" and capability is not None:
                continue
            if capability is not None and requirement is None:
                continue
            if requirement is not None and not (
                requirement["weights_ready"] and requirement["runtime_ready"]
            ):
                continue
            key = self._child_key(kind, column_id, items)
            child = children.get(key)
            if child is not None and child.receipt_id is not None:
                continue
            if child is None:
                child = self.store.reserve_child_and_advance(
                    row.id, expected_cursor=cursor, child_key=key, kind=kind
                )
                cursor += 1
            project = self.workspace.get(row.project_id)
            if self._adopt_existing_action_child(
                project,
                row=row,
                child=child,
                kind=kind,
                child_key=key,
            ):
                continue
            if any(
                not self._source_item_current(project, row.sheet_id, item)
                for item in items
            ):
                self.store.append_error(
                    row.id,
                    project_id=row.project_id,
                    storage_org_id=row.storage_org_id,
                    message="A source file changed before search preparation started.",
                )
                return
            body = self._action_body(project, row, kind, column_name, items, key)
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
        self._settle_if_coordinated(row)

    def _adopt_existing_action_child(
        self,
        project: Any,
        *,
        row: SearchPreparationBatchRow,
        child: SearchPreparationChildRow,
        kind: str,
        child_key: str,
    ) -> bool:
        """Bind an ordinary action reservation created by a concurrent advance.

        A receipt without a run/job is still inside the action admission
        transaction. Returning true defers without manufacturing a coordinator
        failure; periodic recovery retries after that transaction publishes its
        durable job identity.
        """

        action_kind = {
            "native_pdf": "media.read_pdf_text",
            "ocr": "media.ocr",
            "transcribe": "media.transcribe",
        }.get(kind)
        if action_kind is None:
            return False
        receipt = ReceiptStore(project).find_by_action_kind_and_idempotency_key(
            action_kind=action_kind,
            key=f"search-preparation:{row.id}:{child_key}",
        )
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
    def _retryable_action_errors(errors: object) -> bool:
        return isinstance(errors, list) and any(
            isinstance(error, dict)
            and error.get("code")
            in {"idempotency_in_progress", "output_column_busy"}
            for error in errors
        )

    def _settle_if_coordinated(self, row: SearchPreparationBatchRow) -> None:
        from frisket.engine.jobs import model_pull_store

        live = {item.setup_ref: item for item in _requirements()}
        latest = {}
        for pull in model_pull_store.list_recent(
            self.workspace.queue.engine,
            str(self.workspace.root),
            limit=100,
            persist=False,
        ):
            if pull.created_at >= (row.decision_at or row.created_at):
                latest.setdefault(pull.model_ref, pull)
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
        grouped: defaultdict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        capability_ready = {
            str(requirement.capability): bool(
                requirement.weights_ready and requirement.runtime_ready
            )
            for requirement in live.values()
        }
        for item in row.plan["items"]:
            capability = (
                "ocr"
                if item["kind"] == "ocr"
                else "transcription"
                if item["kind"] == "transcribe"
                else None
            )
            if capability is not None and not capability_ready.get(capability, False):
                continue
            if row.decision == "not_now" and capability is not None:
                continue
            grouped[(item["kind"], item["column_id"])].append(item)
        expected = {
            self._child_key(kind, column_id, items)
            for (kind, column_id), items in grouped.items()
        }
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
    def _child_key(
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
        items: list[dict[str, Any]],
        child_key: str,
    ) -> dict[str, Any]:
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
                    "action_id": "media.read_pdf_text",
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
                "action_id": "media.ocr",
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
                "action_id": "media.transcribe",
                "params": {"source": column_name, "engine": "parakeet-tdt"},
                "output_names": {
                    "text": f"Transcript · {column_name}",
                    "segments": f"Transcript segments · {column_name}",
                },
            }
        )

    def _start_next_setup(
        self, row: SearchPreparationBatchRow, *, initiated_by: str | None = None
    ) -> None:
        from frisket.engine.jobs import model_pull_store

        if self.setup_start is None or row.decision != "prepare":
            return
        requirements = {item.setup_ref: item for item in _requirements()}
        latest = {}
        for pull in model_pull_store.list_recent(
            self.workspace.queue.engine,
            str(self.workspace.root),
            limit=100,
            persist=False,
        ):
            if pull.created_at >= (row.decision_at or row.created_at):
                latest.setdefault(pull.model_ref, pull)
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
                self.setup_start(ref, initiated_by)
                return

    def _project(
        self, row: SearchPreparationBatchRow, *, may_setup_models: bool = True
    ) -> SearchPreparationBatch:
        from frisket.engine.jobs import model_pull_store

        grouped: defaultdict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for item in row.plan["items"]:
            if row.decision == "not_now" and item["kind"] != "native_pdf":
                continue
            grouped[(item["kind"], item["column_id"])].append(item)
        expected_keys = {
            self._child_key(kind, column_id, items)
            for (kind, column_id), items in grouped.items()
        }
        children = [
            child
            for child in self.store.children(row.id)
            if child.child_key in expected_keys
        ]
        project = self.workspace.get(row.project_id)
        receipts = ReceiptStore(project)
        jobs: list[SearchPreparationJob] = []
        child_states: dict[int, str] = {}
        projection_errors = list(row.errors)
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
                else {
                    "queued": "queued",
                    "running": "running",
                    "completed": "done",
                    "partial": "failed",
                    "failed": "failed",
                    "cancelled": "cancelled",
                }.get(stored.status, "failed")
            )
            child_states[child.id] = state
            if stored is not None and state in {"failed", "cancelled"}:
                try:
                    receipt = stored.parsed()
                except Exception:
                    projection_errors.append("Search preparation action failed.")
                else:
                    projection_errors.extend(
                        error.message for error in receipt.errors if error.message
                    )
            if child.job_id is not None and state != "planning":
                jobs.append(
                    SearchPreparationJob(
                        job_id=child.job_id,
                        status=state,
                        action_kind=child.kind,
                    )
                )
        current = {item.setup_ref: item for item in _requirements()}
        pulls = {}
        for pull in model_pull_store.list_recent(
            self.workspace.queue.engine,
            str(self.workspace.root),
            limit=100,
            persist=False,
        ):
            if (
                pull.created_at >= (row.decision_at or row.created_at)
                and pull.model_ref in row.selected_setup_refs
                and pull.model_ref not in pulls
            ):
                pulls[pull.model_ref] = pull
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
            pull = pulls.get(planned["setup_ref"])
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
        elif missing_required and row.decision is None:
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
        return SearchPreparationBatch(
            id=row.id,
            sheet_id=row.sheet_id,
            plan_hash=row.plan_hash,
            status=status,
            groups=groups,
            models=models,
            jobs=jobs,
            can_stop=status in {"preparing", "needs_consent"},
            can_setup=may_setup_models
            and status == "needs_consent"
            and any(m.required and not m.ready and m.can_setup for m in models),
            errors=list(dict.fromkeys(projection_errors)),
        )


__all__ = ["SearchPreparationService"]

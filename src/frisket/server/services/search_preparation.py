"""Planning and durable coordination for local search preparation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
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
from frisket.engine.jobs.queue import ACTION_RUN_KIND
from frisket.engine.jobs.search_index import index_work_marker
from frisket.engine.jobs.search_preparation_store import (
    SearchPreparationBatchRow,
    SearchPreparationStore,
)
from frisket.engine.pdf_text import PdfTextError, extract_pdf_text
from frisket.engine.store.prepared_content import PreparedContentStore
from frisket.project_settings import read_project_settings
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.server.route_errors import RouteError
from frisket.server.services.action_runs import ActionRunService


_OCR_REF = "engine-setup:rapidocr.local@1"
_TRANSCRIBE_REF = "engine-setup:parakeet-tdt.local-onnx@1"
_SEMANTIC_REF = "engine-setup:embeddinggemma-2.local@1"
_GROUP_ORDER = ("native_pdf", "ocr", "transcribe")
_SCAN_PAGE = 200


def _sha(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _requirement_dict(item: Any, *, required: bool) -> dict[str, Any]:
    return {
        "setup_ref": str(item.setup_ref),
        "capability": str(item.capability),
        "display_name": str(item.display_name),
        "purpose": {
            "ocr": "Read text in scans and images",
            "transcription": "Transcribe recordings and video",
            "semantic_search": "Search by meaning",
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
        if not self.automatic_enabled(project_id):
            return None
        return self.create(
            project_id,
            sheet_id=sheet_id,
            row_ids=row_ids,
            force_ocr=False,
            trigger_receipt_id=receipt_id,
        ).id

    def batch_for_receipt(self, project_id: str, receipt_id: str) -> str | None:
        row = self.store.for_receipt(
            workspace_root=str(self.workspace.root),
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            receipt_id=receipt_id,
        )
        return None if row is None else row.id

    def list(self, project_id: str) -> SearchPreparationBatchList:
        return SearchPreparationBatchList(
            batches=[
                self._project(row)
                for row in self.store.list_recent(
                    project_id=project_id,
                    storage_org_id=self.workspace.queue_storage_org_id,
                )
            ]
        )

    def get(self, project_id: str, batch_id: str) -> SearchPreparationBatch:
        try:
            row = self.store.get(
                batch_id,
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
            )
        except KeyError as exc:
            raise RouteError(404, "search preparation batch was not found") from exc
        return self._project(row)

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
        row = self.store.get(
            batch_id,
            project_id=project_id,
            storage_org_id=self.workspace.queue_storage_org_id,
        )
        project = self.workspace.get(project_id)
        current = self._plan(
            project,
            sheet_id=row.sheet_id,
            row_ids=row.row_ids,
            force_ocr=row.force_ocr,
        )
        from frisket.engine.jobs.search_preparation_store import canonical_plan_hash

        if (
            int(project.op_cursor) != row.source_cutoff
            or canonical_plan_hash(current) != row.plan_hash
        ):
            raise RouteError(
                409, "search preparation plan changed; review the refreshed plan"
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
        unavailable = [
            model
            for model in row.plan["models"]
            if model["required"]
            and not model["runtime_ready"]
            and not model["runtime_installable"]
        ]
        if decision == "prepare" and unavailable:
            raise RouteError(
                409,
                unavailable[0]["remediation"] or "local runtime is unavailable",
            )
        required = {ref for ref, model in missing.items() if model["required"]}
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
            # The model-pull store admits only one active workspace pull.  Start
            # the first missing ref; the terminal callback advances the next.
            first = next(ref for ref in selected_setup_refs if ref in missing)
            self.setup_start(first, initiated_by)
        self._advance(row)
        return self.get(project_id, batch_id)

    def stop(self, project_id: str, batch_id: str) -> SearchPreparationBatch:
        row = self.store.request_stop(
            batch_id,
            project_id=project_id,
            storage_org_id=self.workspace.queue_storage_org_id,
        )
        for child in self.store.children(row.id):
            if child.job_id is not None:
                self.workspace.queue.cancel(child.job_id)
        return self._project(row)

    def setup_completed(self, setup_ref: str) -> None:
        """Wake waiting intents after a durable setup terminal event."""

        for project in self.workspace.list():
            project_id = str(project["id"])
            for row in self.store.list_recent(
                project_id=project_id,
                storage_org_id=self.workspace.queue_storage_org_id,
                limit=100,
            ):
                if (
                    setup_ref in row.selected_setup_refs
                    and row.stop_requested_at is None
                ):
                    self._start_next_setup(row)
                    self._advance(row)

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

        for project in self.workspace.list():
            for row in self.store.list_recent(
                project_id=str(project["id"]),
                storage_org_id=self.workspace.queue_storage_org_id,
                limit=100,
            ):
                if row.decision == "prepare" and row.stop_requested_at is None:
                    self._start_next_setup(row)
                    self._advance(row)

    def text_samples(
        self,
        project_id: str,
        batch_id: str,
        *,
        row_id: int | None,
        page_number: int | None,
    ) -> SearchPreparationTextSamples:
        row = self.store.get(
            batch_id,
            project_id=project_id,
            storage_org_id=self.workspace.queue_storage_org_id,
        )
        project = self.workspace.get(project_id)
        samples: list[SearchPreparationTextSample] = []
        for item in row.plan.get("items", []):
            if item["mime"] != "application/pdf":
                continue
            if row_id is not None and item["row_id"] != row_id:
                continue
            wanted_page = page_number or item.get("page_number") or 1
            text, count, source = self._sample_pdf(project, item, wanted_page)
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
        columns = [
            column
            for column in project.columns(sheet_id)
            if column["type"] in {"file", "image", "audio", "video"}
            and not dict(column).get("hidden", False)
        ]
        selected = project.visible_row_ids(
            sheet_id, None if row_ids is None else list(row_ids)
        )
        if row_ids is not None and set(selected) != set(row_ids):
            raise RouteError(
                404, "one or more selected rows are not visible in this sheet"
            )
        items: list[dict[str, Any]] = []
        counts: defaultdict[str, int] = defaultdict(int)
        for offset in range(0, len(selected), _SCAN_PAGE):
            page_rows = selected[offset : offset + _SCAN_PAGE]
            for column in columns:
                for selected_row, value in project.get_values(
                    sheet_id, int(column["id"]), row_ids=page_rows
                ).items():
                    if not isinstance(value, dict) or not isinstance(
                        value.get("blob"), str
                    ):
                        continue
                    source = resolve_cell_blob_source(
                        project,
                        sheet_id=sheet_id,
                        row_id=selected_row,
                        column_id=int(column["id"]),
                    )
                    if source is None:
                        continue
                    mime = source.mime_type.lower()
                    kind: str | None = None
                    page_count: int | None = None
                    if mime == "application/pdf":
                        native, page_count = self._pdf_has_complete_text(
                            project, source.blob_hash, source.page_number
                        )
                        kind = "ocr" if force_ocr or not native else "native_pdf"
                    elif mime.startswith("image/"):
                        kind = "ocr"
                    elif mime.startswith("audio/") or mime.startswith("video/"):
                        kind = "transcribe"
                    if kind is None or self._has_current_result(project, source, kind):
                        continue
                    item = {
                        "kind": kind,
                        "row_id": selected_row,
                        "column_id": int(column["id"]),
                        "column_name": str(column["name"]),
                        "blob_hash": source.blob_hash,
                        "mime": mime,
                        "filename": value.get("filename"),
                        "page_number": source.page_number,
                        "page_count": page_count,
                        "retrieval_revision": source.binding.retrieval_revision,
                    }
                    items.append(item)
                    counts[kind] += 1
        capabilities: set[str] = set()
        if counts["ocr"]:
            capabilities.add("ocr")
        if counts["transcribe"]:
            capabilities.add("transcription")
        models = []
        for requirement in _requirements():
            capability = str(requirement.capability)
            if capability in capabilities:
                models.append(_requirement_dict(requirement, required=True))
            elif capability == "semantic_search":
                models.append(_requirement_dict(requirement, required=False))
        return {
            "source_cutoff": int(project.op_cursor),
            "force_ocr": force_ocr,
            "groups": [
                {"kind": kind, "count": counts[kind]}
                for kind in _GROUP_ORDER
                if counts[kind]
            ],
            "models": models,
            "items": items,
        }

    @staticmethod
    def _pdf_has_complete_text(
        project: Any, blob_hash: str, page_number: int | None
    ) -> tuple[bool, int]:
        try:
            with project.materialize_blob(blob_hash) as source:
                pages = asyncio.run(extract_pdf_text(Path(source)))
        except (PdfTextError, OSError):
            return False, 1
        selected = (
            pages
            if page_number is None
            else [p for p in pages if p.page == page_number]
        )
        return bool(selected) and all(
            any(token.text.strip() for token in p.tokens) for p in selected
        ), len(pages)

    @staticmethod
    def _has_current_result(project: Any, source: Any, kind: str) -> bool:
        if kind == "native_pdf":
            return False
        role = "media_ocr_grounding" if kind == "ocr" else "media_transcribe_temporal"
        row = project.db.execute(
            "SELECT 1 FROM source_artifacts a JOIN source_spans s ON s.artifact_id=a.id "
            "JOIN evidence_link_spans ls ON ls.span_id=s.id "
            "JOIN evidence_links l ON l.id=ls.link_id "
            "WHERE a.blob_hash=? AND a.source_sheet_id=? AND a.source_row_id=? "
            "AND a.source_column_id=? AND l.status='active' AND l.link_role=? LIMIT 1",
            (
                source.blob_hash,
                source.sheet_id,
                source.binding.row_id,
                source.binding.column_id,
                role,
            ),
        ).fetchone()
        return row is not None

    def _advance(self, row: SearchPreparationBatchRow) -> None:
        if row.stop_requested_at is not None or row.decision == "not_now":
            return
        planned_by_ref = {m["setup_ref"]: m for m in row.plan["models"]}
        readiness = {}
        for live in _requirements():
            planned = planned_by_ref.get(live.setup_ref)
            if planned is not None:
                readiness[live.capability] = _requirement_dict(
                    live, required=bool(planned["required"])
                )
        allowed = row.decision == "prepare"
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
            if requirement is not None and not (
                requirement["weights_ready"] and requirement["runtime_ready"]
            ):
                if not allowed:
                    continue
                return
            key = f"{kind}:{column_id}:{_sha([(i['row_id'], i['blob_hash'], i['retrieval_revision']) for i in items])}"
            if key in children:
                continue
            child = self.store.reserve_child_and_advance(
                row.id, expected_cursor=cursor, child_key=key, kind=kind
            )
            cursor += 1
            body = self._action_body(row, kind, column_name, items, key)
            response = self.action_runs.run_action(row.project_id, body)
            receipt_id = response.payload.get("receipt_id")
            job_id = None
            if isinstance(receipt_id, str):
                job = self.workspace.queue.find_job_by_refs(
                    ACTION_RUN_KIND,
                    statuses=("queued", "running", "done", "failed", "cancelled"),
                    project_id=row.project_id,
                    storage_org_id=row.storage_org_id,
                    receipt_id=receipt_id,
                )
                job_id = None if job is None else job.id
            self.store.bind_child(
                child.id,
                job_id=job_id,
                receipt_id=receipt_id if isinstance(receipt_id, str) else None,
            )

    @staticmethod
    def _action_body(
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
        if kind == "native_pdf":
            return {
                **common,
                "action_id": "media.read_pdf_text",
                "params": {"source": column_name},
                "output_names": {"text": f"Search text · {column_name}"},
            }
        if kind == "ocr":
            return {
                **common,
                "action_id": "media.ocr",
                "params": {"source": column_name, "engine": "rapidocr"},
                "output_names": {
                    "text": f"Search text · {column_name}",
                    "blocks": f"Search positions · {column_name}",
                },
            }
        return {
            **common,
            "action_id": "media.transcribe",
            "params": {"source": column_name, "engine": "parakeet-tdt"},
            "output_names": {
                "text": f"Transcript · {column_name}",
                "segments": f"Transcript segments · {column_name}",
            },
        }

    def _start_next_setup(self, row: SearchPreparationBatchRow) -> None:
        if self.setup_start is None or row.decision != "prepare":
            return
        requirements = {item.setup_ref: item for item in _requirements()}
        for ref in row.selected_setup_refs:
            requirement = requirements.get(ref)
            if requirement is not None and (
                not requirement.weights_ready
                or (not requirement.runtime_ready and requirement.runtime_installable)
            ):
                self.setup_start(ref, None)
                return

    def _project(self, row: SearchPreparationBatchRow) -> SearchPreparationBatch:
        children = self.store.children(row.id)
        jobs = []
        for child in children:
            if child.job_id is None:
                continue
            job = self.workspace.queue.get(child.job_id)
            if job is not None:
                jobs.append(
                    SearchPreparationJob(
                        job_id=job.id,
                        status=job.status,
                        action_kind=child.kind,
                    )
                )
        current = {item.setup_ref: item for item in _requirements()}
        models = []
        missing_required = False
        for planned in row.plan["models"]:
            live = current.get(planned["setup_ref"])
            weights_ready = (
                bool(live.weights_ready)
                if live is not None
                else planned["weights_ready"]
            )
            runtime_ready = (
                bool(live.runtime_ready)
                if live is not None
                else planned["runtime_ready"]
            )
            missing_required = missing_required or (
                planned["required"] and not weights_ready
            )
            models.append(
                SearchPreparationModel(
                    setup_ref=planned["setup_ref"],
                    display_name=planned["display_name"],
                    purpose=planned["purpose"],
                    required=planned["required"],
                    ready=weights_ready and runtime_ready,
                    runtime_ready=runtime_ready,
                    download_bytes=None if weights_ready else planned["download_bytes"],
                    additional_runtime_download=planned["additional_runtime_download"],
                    remediation=planned["remediation"],
                    license=planned["license"],
                    source_url=planned["source_url"],
                    pull_id=None,
                )
            )
        job_statuses = {job.status for job in jobs}
        all_children = len(children) == len(
            {(item["kind"], item["column_id"]) for item in row.plan["items"]}
        )
        project = self.workspace.get(row.project_id)
        indexing = index_work_marker(project, include_preparation=False) is not None
        if row.stop_requested_at is not None:
            status = "stopped"
        elif "failed" in job_statuses:
            status = "failed"
        elif row.decision == "not_now":
            status = "complete"
        elif missing_required and row.decision is None:
            status = "needs_consent"
        elif missing_required:
            status = "preparing"
        elif not all_children or job_statuses & {"queued", "running"} or indexing:
            status = "preparing"
        elif job_statuses <= {"done"}:
            status = "complete"
        else:
            status = "planning"
        groups = []
        for group in row.plan["groups"]:
            kind_children = [c for c in children if c.kind == group["kind"]]
            if row.stop_requested_at is not None:
                group_status = "stopped"
            elif row.decision == "not_now" and (
                group["kind"] != "native_pdf"
                and any(m.required and not m.ready for m in models)
            ):
                group_status = "not_prepared"
            elif not kind_children and status == "needs_consent":
                group_status = "needs_consent"
            elif kind_children and all(
                self.workspace.queue.get(c.job_id).status == "done"
                for c in kind_children
                if c.job_id is not None
            ):
                group_status = "complete"
            elif kind_children:
                group_status = "running"
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
            can_setup=status == "needs_consent"
            and any(m.required and not m.ready for m in models),
            errors=list(row.errors),
        )

    @staticmethod
    def _sample_pdf(
        project: Any, item: Mapping[str, Any], page: int
    ) -> tuple[str, int, str]:
        # Prefer canonical prepared content associated with this exact artifact.
        row = project.db.execute(
            "SELECT c.prepared_ref_id FROM current_cell_values c "
            "WHERE c.row_id=? AND c.prepared_ref_id IS NOT NULL ORDER BY c.column_id DESC LIMIT 1",
            (item["row_id"],),
        ).fetchone()
        if row is not None:
            prepared = PreparedContentStore(project).resolve(int(row[0]))
            pin = next((pin for pin in prepared.pins if pin.page_number == page), None)
            if pin is not None:
                return (
                    prepared.text[pin.start : pin.end],
                    len(prepared.pins),
                    "prepared",
                )
        with project.materialize_blob(str(item["blob_hash"])) as source:
            pages = asyncio.run(extract_pdf_text(Path(source)))
        selected = next((value for value in pages if value.page == page), None)
        if selected is None:
            raise RouteError(422, "PDF page is outside the document")
        return " ".join(token.text for token in selected.tokens), len(pages), "native"


__all__ = ["SearchPreparationService"]

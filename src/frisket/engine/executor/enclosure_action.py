"""Typed enclosure capability; downloads commit row by row, not batch-atomically."""

from __future__ import annotations

import json
from functools import partial
from typing import Any

from frisket.actions.core import _ProjectAction
from frisket.actions.enclosure_types import (
    EnclosureMaterializer,
    MaterializedEnclosure,
    MaterializedEnclosures,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import SheetRows

from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_families._errors import (
    receipt_stale_replay_error,
)
from frisket.engine.executor.action_inventory import (
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_reservations import (
    _cleanup_reserved_receipt_on_failed_result,
    _direct_action_finalize_metadata,
    _finalize_direct_reserved_action_receipt,
    _receipt_for_idempotency,
    _reserve_running_action_receipt,
    _running_receipt_stale_result,
)
from frisket.engine.executor.action_support import (
    _failed_result,
)
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.redaction import redact_text
from frisket.sdk.media import media_text_hash
from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.media_blobs import MediaBlobStore


def supports_typed_enclosure_action(terminal: Any) -> bool:
    return isinstance(terminal, _ProjectAction) and terminal.capabilities == (
        EnclosureMaterializer,
    )


class _EnclosureMaterializer:
    def __init__(self, project, scope, resolved, fetch):
        from frisket.ops.enclosures import download_url
        from frisket.ops.egress_policy import media_egress_policy

        self.project, self.scope, self.resolved = project, scope, resolved
        self.rows: list[dict[str, Any]] = []
        self.network: list[dict[str, Any]] = []
        self.fetch = fetch or partial(download_url, policy=media_egress_policy(project))
        self.called = False

    def _fetch(self, row_id: int, url: str):
        facts = {
            "kind": "enclosure_download_network",
            "sheet_id": self.scope.sheet_id,
            "row_id": row_id,
            "url_hash": media_text_hash(url),
            "status": "failed",
            "ssrf_guarded": True,
            "external_api": True,
            "cost_actual": 0.0,
        }
        self.network.append(facts)
        result = self.fetch(url)
        facts["status"] = "error" if result[3] is not None else "downloaded"
        return result

    def materialize(self, *, force: bool = False) -> MaterializedEnclosures:
        from frisket.ops.enclosures import materialize_enclosure_row

        if type(force) is not bool:
            raise ValueError("force must be a boolean")
        if self.called:
            raise ValueError(
                "enclosure materialization may be called only once per invocation"
            )
        self.called = True
        for row_id in self.scope.row_ids:
            url = self.resolved["urls"][row_id]
            result = materialize_enclosure_row(
                self.project,
                sheet_id=self.scope.sheet_id,
                row_id=row_id,
                force=_media_enclosure_force_download(
                    self.project,
                    sheet_id=self.scope.sheet_id,
                    row_id=row_id,
                    requested_force=force,
                    enclosure_url=url,
                ),
                fetch=partial(self._fetch, row_id),
                expected_url=url,
            )
            self.rows.append(
                _media_enclosure_row_result(
                    self.project,
                    sheet_id=self.scope.sheet_id,
                    row_id=row_id,
                    url=url,
                    result=result,
                )
            )
        return MaterializedEnclosures(
            rows=tuple(
                MaterializedEnclosure(
                    row_id=item["row_id"], status=item["status"], error=item["error"]
                )
                for item in self.rows
            )
        )


def run_typed_enclosure_action(
    project: Project,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    enclosure_fetcher: Any | None = None,
) -> ActionResult:
    terminal = bound.action.definition.run
    if not supports_typed_enclosure_action(terminal):
        raise TypeError("enclosure execution requires an EnclosureMaterializer action")
    action = _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=bound.params.model_dump(mode="json", exclude_unset=True),
    )
    scope = bound.request.scope
    assert isinstance(scope, SheetRows) and scope.row_ids is not None

    params_hash = typed_request_hash(bound)
    existing = _receipt_for_idempotency(project, action.idempotency_key)
    if existing is not None:
        return _media_enclosure_result_from_existing_receipt(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
        )

    resolved = _resolve_media_enclosure_inputs(project, action, scope)
    if isinstance(resolved, ActionError):
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=resolved,
        )

    reservation = _reserve_running_action_receipt(
        project,
        action,
        params_hash=params_hash,
        project_id=project_id,
        reservation_kind="media_enclosure_idempotency_reservation",
        result_from_existing_fn=_media_enclosure_result_from_existing_receipt,
    )
    if isinstance(reservation, ActionResult):
        return reservation

    capability = _EnclosureMaterializer(project, scope, resolved, enclosure_fetcher)
    row_results = capability.rows
    try:
        returned = terminal.handler(bound.params, capability)
        MaterializedEnclosures.model_validate(returned)
        if not capability.called:
            raise ValueError("handler did not materialize the admitted enclosures")
    except Exception as exc:
        error = ActionError(
            code="invalid_input_ref"
            if isinstance(exc, ValueError)
            else "media_download_failed",
            message=redact_text(str(exc)),
            action_kind=action.kind,
            field="scope.row_ids",
        )
        return _cleanup_reserved_receipt_on_failed_result(
            project,
            _write_media_enclosure_receipt(
                project,
                action,
                project_id=project_id,
                action_id=reservation["action_id"],
                receipt_id=reservation["receipt_id"],
                params_hash=params_hash,
                sheet_id=scope.sheet_id,
                row_ids=scope.row_ids,
                row_results=row_results,
                network=capability.network,
                status="partial"
                if any(_is_materialized_media_ref(item) for item in row_results)
                else "failed",
                errors=[error],
            ),
            receipt_id=reservation["receipt_id"],
            action_kind=action.kind,
        )

    errors = [
        ActionError(
            code="media_download_failed",
            message=str(item.get("error") or "media download failed"),
            action_kind=action.kind,
            field="scope.row_ids",
            details={"row_id": item["row_id"]},
        )
        for item in row_results
        if item["status"] == "error"
    ]
    status = "completed"
    if errors:
        status = "failed" if len(errors) == len(row_results) else "partial"
    return _cleanup_reserved_receipt_on_failed_result(
        project,
        _write_media_enclosure_receipt(
            project,
            action,
            project_id=project_id,
            action_id=reservation["action_id"],
            receipt_id=reservation["receipt_id"],
            params_hash=params_hash,
            sheet_id=scope.sheet_id,
            row_ids=scope.row_ids,
            row_results=row_results,
            network=capability.network,
            status=status,
            errors=errors,
        ),
        receipt_id=reservation["receipt_id"],
        action_kind=action.kind,
    )


def _resolve_media_enclosure_inputs(
    project: Project,
    action: _TypedProjectEnvelope,
    scope: SheetRows,
) -> dict[str, Any] | ActionError:
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE id=? AND hidden=0",
        (scope.sheet_id,),
    ).fetchone()
    if sheet is None:
        return ActionError(
            code="invalid_input_ref",
            message="media.enclosure_materialize sheet_id does not identify a visible sheet",
            action_kind=action.kind,
            field="scope.sheet_id",
        )
    if len(set(scope.row_ids)) != len(scope.row_ids):
        return ActionError(
            code="invalid_input_ref",
            message="media.enclosure_materialize row_ids must not contain duplicates",
            action_kind=action.kind,
            field="scope.row_ids",
        )
    enclosure_name = "enclosure_url"
    columns_by_name = {
        str(column["name"]): column for column in project.columns(scope.sheet_id)
    }
    enclosure_column = columns_by_name.get(enclosure_name)
    if enclosure_column is None:
        return ActionError(
            code="invalid_input_ref",
            message="media.enclosure_materialize sheet has no enclosure_url column",
            action_kind=action.kind,
            field="scope.sheet_id",
        )
    if enclosure_column["type"] not in ("link", "text"):
        return ActionError(
            code="invalid_input_ref",
            message=(
                "media.enclosure_materialize enclosure_url column must be link or text"
            ),
            action_kind=action.kind,
            field="scope.sheet_id",
            details={
                "column": enclosure_name,
                "type": enclosure_column["type"],
            },
        )
    enclosure_col = int(enclosure_column["id"])
    placeholders = ",".join("?" for _ in scope.row_ids)
    rows = project.db.execute(
        f"SELECT id FROM rows WHERE sheet_id=? AND hidden=0 AND id IN ({placeholders})",
        [scope.sheet_id, *scope.row_ids],
    ).fetchall()
    found = {int(row["id"]) for row in rows}
    missing = sorted(set(scope.row_ids) - found)
    if missing:
        return ActionError(
            code="invalid_input_ref",
            message="media.enclosure_materialize row_ids must belong to the target sheet",
            action_kind=action.kind,
            field="scope.row_ids",
            details={"missing": missing},
        )
    values = project.get_values(
        scope.sheet_id,
        enclosure_col,
        row_ids=scope.row_ids,
    )
    missing_urls = [row_id for row_id in scope.row_ids if not values.get(row_id)]
    if missing_urls:
        return ActionError(
            code="invalid_input_ref",
            message="media.enclosure_materialize rows must contain enclosure_url values",
            action_kind=action.kind,
            field="scope.row_ids",
            details={"missing_enclosure_url": missing_urls},
        )
    return {
        "columns": {
            name: int(column["id"]) for name, column in columns_by_name.items()
        },
        "urls": {row_id: str(values[row_id]) for row_id in scope.row_ids},
    }


def _media_enclosure_result_from_existing_receipt(
    project: Project,
    existing: Any,
    *,
    params_hash: str,
    project_id: str,
    action: _TypedProjectEnvelope,
) -> ActionResult:
    if existing["params_hash"] != params_hash:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="idempotency_conflict",
                message="idempotency_key was already used with different normalized params",
                action_kind=action.kind,
                field="idempotency_key",
            ),
        )
    stale = _running_receipt_stale_result(
        project,
        existing,
        params_hash=params_hash,
        project_id=project_id,
        action=action,
    )
    if stale is not None:
        return stale
    receipt = Receipt.model_validate(json.loads(existing["body"]))
    if receipt.status == "running":
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="idempotency_in_progress",
                message=(
                    "idempotency_key is already reserved by a running "
                    "media.enclosure_materialize action"
                ),
                action_kind=action.kind,
                field="idempotency_key",
                details={"receipt_id": receipt.receipt_id},
            ),
        )
    replay_error = _media_enclosure_replay_error(project, receipt)
    if replay_error is not None:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=replay_error,
        )
    return _result_from_receipt(receipt)


def _media_enclosure_replay_error(
    project: Project, receipt: Receipt
) -> ActionError | None:
    for output in receipt.outputs:
        ref = output.ref
        if not isinstance(ref, dict):
            continue
        if ref.get("kind") == "media_cell" and ref.get("blob_hash"):
            error = _media_enclosure_cell_replay_error(project, receipt, ref)
            if error is not None:
                return error
        if ref.get("kind") == "media_blob" and ref.get("blob_hash"):
            error = _media_enclosure_blob_replay_error(project, receipt, ref)
            if error is not None:
                return error
    return None


def _media_enclosure_cell_replay_error(
    project: Project, receipt: Receipt, ref: dict[str, Any]
) -> ActionError | None:
    stale = partial(receipt_stale_replay_error, receipt, field="receipt.outputs")
    replay = "media.enclosure_materialize replay "
    sheet_id = ref.get("sheet_id")
    row_id = ref.get("row_id")
    column_id = ref.get("column_id")
    blob_hash = ref.get("blob_hash")
    if (
        type(sheet_id) is not int
        or type(row_id) is not int
        or type(column_id) is not int
        or not isinstance(blob_hash, str)
        or not blob_hash
    ):
        return stale(replay + "media cell ref is incomplete", ref=ref)
    column = project.db.execute(
        "SELECT id FROM columns WHERE id=? AND sheet_id=? AND hidden=0",
        (column_id, sheet_id),
    ).fetchone()
    row = project.db.execute(
        "SELECT id FROM rows WHERE id=? AND sheet_id=? AND hidden=0",
        (row_id, sheet_id),
    ).fetchone()
    if column is None or row is None:
        return stale(
            replay + "target row or media column is missing",
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
        )
    value = project.get_values(sheet_id, column_id, row_ids=[row_id]).get(row_id)
    if not isinstance(value, dict) or value.get("blob") != blob_hash:
        return stale(
            replay + "media cell no longer matches the recorded blob",
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            blob_hash=blob_hash,
        )
    return _media_enclosure_blob_replay_error(project, receipt, ref)


def _media_enclosure_blob_replay_error(
    project: Project, receipt: Receipt, ref: dict[str, Any]
) -> ActionError | None:
    stale = partial(receipt_stale_replay_error, receipt, field="receipt.outputs")
    replay = "media.enclosure_materialize replay "
    blob_hash = ref.get("blob_hash")
    if not isinstance(blob_hash, str) or not blob_hash:
        return stale(replay + "blob ref is incomplete", ref=ref)
    blob = MediaBlobStore(project).blob_row(blob_hash)
    if blob is None:
        return stale(replay + "blob row is missing", blob_hash=blob_hash)
    try:
        with project.materialize_blob(blob_hash):
            pass
    except BlobNotFoundError:
        return stale(replay + "blob file is missing", blob_hash=blob_hash)
    return None


def _write_media_enclosure_receipt(
    project: Project,
    action: _TypedProjectEnvelope,
    *,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    sheet_id: int,
    row_ids: list[int],
    row_results: list[dict[str, Any]],
    network: list[dict[str, Any]],
    status: str,
    errors: list[ActionError],
) -> ActionResult:
    op_ids = [
        int(item["op_id"]) for item in row_results if isinstance(item.get("op_id"), int)
    ]
    refs = _media_enclosure_refs(
        sheet_id=sheet_id,
        row_ids=row_ids,
        row_results=row_results,
        params_hash=params_hash,
    )
    refs["network"] = network
    outputs = _media_enclosure_outputs(refs)
    receipt = _media_enclosure_receipt(
        action=action,
        action_id=action_id,
        project_id=project_id,
        receipt_id=receipt_id,
        params_hash=params_hash,
        op_ids=op_ids,
        refs=refs,
        status=status,
        errors=errors,
    )
    finalize_result = _finalize_direct_reserved_action_receipt(
        project,
        action,
        params_hash=params_hash,
        project_id=project_id,
        receipt=receipt,
        finalize=_direct_action_finalize_metadata(
            action.kind,
            result_from_existing_fn=_media_enclosure_result_from_existing_receipt,
            require_running_status=False,
        ),
    )
    if finalize_result is not None:
        return finalize_result
    return ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status=status,
        project_id=project_id,
        op_ids=op_ids,
        outputs=outputs,
        receipt_id=receipt_id,
        errors=errors,
    )


def _media_enclosure_row_result(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    url: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    media_col = _column_id_by_name(project, sheet_id, "media")
    media = result.get("media")
    if media is None and media_col is not None:
        media = project.get_values(sheet_id, media_col, row_ids=[row_id]).get(row_id)
    blob_hash = media.get("blob") if isinstance(media, dict) else None
    blob_row = None
    if blob_hash:
        blob_row = MediaBlobStore(project).blob_row(blob_hash)
    status = str(result.get("status") or "unknown")
    return {
        "row_id": row_id,
        "url": url,
        "url_hash": media_text_hash(url),
        "status": status,
        "error": result.get("error"),
        "media": media if isinstance(media, dict) else None,
        "media_column_id": media_col,
        "blob": dict(blob_row) if blob_row is not None else None,
        "op_id": result.get("op_id"),
    }


def _column_id_by_name(project: Project, sheet_id: int, name: str) -> int | None:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, name),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _media_enclosure_force_download(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    requested_force: bool,
    enclosure_url: str,
) -> bool:
    if requested_force:
        return True
    media_col = _column_id_by_name(project, sheet_id, "media")
    if media_col is None:
        return False
    media = project.get_values(sheet_id, media_col, row_ids=[row_id]).get(row_id)
    if not isinstance(media, dict):
        return False
    blob_hash = media.get("blob")
    if not isinstance(blob_hash, str) or not blob_hash:
        return False
    blob = MediaBlobStore(project).blob_row(blob_hash)
    if blob is None:
        return True
    try:
        with project.materialize_blob(blob_hash):
            pass
    except BlobNotFoundError:
        return True
    return str(blob["source_url"] or "") != enclosure_url


def _media_enclosure_refs(
    *,
    sheet_id: int,
    row_ids: list[int],
    row_results: list[dict[str, Any]],
    params_hash: str,
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for item in row_results:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    rows_ref = {
        "kind": "enclosure_rows",
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "params_hash": params_hash,
    }
    request_ref = {
        "kind": "enclosure_download_request",
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "row_count": len(row_ids),
        "url_hashes": [item["url_hash"] for item in row_results],
        "ssrf_guarded": True,
        "max_bytes": 512 * 1024 * 1024,
        "params_hash": params_hash,
    }
    media_cells = [
        {
            "kind": "media_cell",
            "sheet_id": sheet_id,
            "row_id": item["row_id"],
            "column_id": item.get("media_column_id"),
            "status": item["status"],
            "error": item.get("error"),
            "blob_hash": (item.get("media") or {}).get("blob")
            if isinstance(item.get("media"), dict)
            else None,
            "mime": (item.get("media") or {}).get("mime")
            if isinstance(item.get("media"), dict)
            else None,
            "filename": (item.get("media") or {}).get("filename")
            if isinstance(item.get("media"), dict)
            else None,
            "op_id": item.get("op_id"),
            "may_feed": ["media.transcribe"]
            if _is_materialized_media_ref(item)
            else [],
        }
        for item in row_results
    ]
    media_blobs = [
        {
            "kind": "media_blob",
            "sheet_id": sheet_id,
            "row_id": item["row_id"],
            "column_id": item.get("media_column_id"),
            "blob_hash": item["blob"]["hash"],
            "filename": item["blob"].get("filename"),
            "mime": item["blob"].get("mime"),
            "size": item["blob"].get("size"),
            "source_url_hash": item["url_hash"],
            "may_feed": ["media.transcribe"],
        }
        for item in row_results
        if item.get("blob") and _is_materialized_media_ref(item)
    ]
    return {
        "rows": rows_ref,
        "request": request_ref,
        "media_cells": media_cells,
        "media_blobs": media_blobs,
        "status_counts": status_counts,
    }


def _is_materialized_media_ref(item: dict[str, Any]) -> bool:
    return (
        item.get("status") in {"downloaded", "already_downloaded"}
        and isinstance(item.get("media"), dict)
        and bool(item["media"].get("blob"))
    )


def _feedable_media_cell_refs(refs: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        ref
        for ref in refs["media_cells"]
        if ref.get("blob_hash") and "media.transcribe" in ref.get("may_feed", [])
    ]


def _media_enclosure_outputs(refs: dict[str, Any]) -> list[ActionOutput]:
    rows_ref = refs["rows"]
    outputs = [
        ActionOutput(
            kind="rows",
            name="enclosure_rows",
            sheet_id=rows_ref["sheet_id"],
            row_ids=list(rows_ref.get("row_ids") or []),
            ref=rows_ref,
        )
    ]
    outputs.extend(
        ActionOutput(
            kind="media_cell",
            name="media",
            sheet_id=ref.get("sheet_id"),
            row_ids=[int(ref["row_id"])],
            ref=ref,
        )
        for ref in _feedable_media_cell_refs(refs)
    )
    outputs.extend(
        ActionOutput(
            kind="media_blob",
            name=ref.get("filename"),
            sheet_id=ref.get("sheet_id"),
            row_ids=[int(ref["row_id"])],
            ref=ref,
        )
        for ref in refs["media_blobs"]
    )
    return outputs


def _media_enclosure_receipt(
    *,
    action: _TypedProjectEnvelope,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    op_ids: list[int],
    refs: dict[str, Any],
    status: str,
    errors: list[ActionError],
) -> Receipt:
    feedable_media_cells = _feedable_media_cell_refs(refs)
    failed_media_cells = [
        ref for ref in refs["media_cells"] if ref not in feedable_media_cells
    ]
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=op_ids,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status=status,
        inputs=[
            ReceiptIO(name="rows", ref=refs["rows"]),
            ReceiptIO(name="request", ref=refs["request"]),
        ],
        outputs=[
            ReceiptIO(name="rows", ref=refs["rows"]),
            *[
                ReceiptIO(name=f"media_cell.{ref['row_id']}", ref=ref)
                for ref in feedable_media_cells
            ],
            *[
                ReceiptIO(name=f"media_blob.{ref['row_id']}", ref=ref)
                for ref in refs["media_blobs"]
            ],
        ],
        provider_use=[
            {
                "provider": "direct_url",
                "service": "enclosure_download",
                "external_api": True,
                "cost_actual": 0.0,
                "status_counts": refs["status_counts"],
            }
        ]
        if refs["network"]
        else [],
        evidence=[
            *[ReceiptEvidence(ref=ref) for ref in failed_media_cells],
            *[ReceiptEvidence(ref=ref) for ref in refs["network"]],
        ],
        errors=errors,
    )

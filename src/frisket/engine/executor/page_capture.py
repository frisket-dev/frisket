"""Page-capture staging, atomic publication, receipts and replay."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from frisket.ops.capture import url as url_capture
from frisket.contracts.action import (
    ActionError,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.actions.file_types import UrlColumn
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.blob_outputs import (
    RowBlobOutput,
    RowBlobPlan,
    commit_row_blob_plan,
    stage_blob_bytes,
)
from frisket.engine.executor.action_families._errors import (
    receipt_stale_replay_error,
)
from frisket.engine.store.evidence import (
    evidence_link_by_stable_id,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.sdk.media import (
    media_output_column_value_hash,
    media_replay_row_ids,
)
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_reservations import (
    _receipt_for_idempotency,
    _reserve_running_action_receipt,
    _reserved_receipt_result_from_existing,
)
from frisket.engine.executor.action_support import (
    _failed_result,
)
from frisket.sdk.replay import _replay_output_column_row
from frisket.engine.store import Project
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.materialization import (
    MaterializedColumnSpec,
    SingleParentChildSheetPlan,
    SingleParentMaterializedRow,
    write_single_parent_child_sheet,
)
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    owned_media_metadata_document,
)
from frisket.engine.store.receipts import ReceiptStore


logger = logging.getLogger("frisket.executor")


_WEB_CAPTURE_PAGE_FAIL_AFTER_STAGING_FOR_TEST = False


@dataclass(frozen=True)
class CapturePlan:
    sheet_id: int
    input_column: str
    row_ids: list[int] | None
    output_name: str
    output_mode: str
    links_sheet_name: str
    render_mode: str
    include_warc: bool
    max_bytes: int
    timeout_ms: int
    full_page: bool
    output_column_type: str
    primary_role: str
    output_names: dict[str, str]
    request: dict[str, Any]


def execute_prepared_capture(
    project: Project,
    action: _TypedProjectEnvelope,
    params: CapturePlan,
    *,
    project_id: str,
    params_hash: str,
    resolved: dict[str, Any],
    url_capture_fetcher: Any | None,
    url_capture_browser: Any | None,
) -> ActionResult:
    existing = _receipt_for_idempotency(project, action.idempotency_key)
    if existing is not None:
        return _web_capture_page_result_from_existing_receipt(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
        )

    reservation = _reserve_running_action_receipt(
        project,
        action,
        params_hash=params_hash,
        project_id=project_id,
        reservation_kind="web_capture_page_idempotency_reservation",
        result_from_existing_fn=_web_capture_page_result_from_existing_receipt,
    )
    if isinstance(reservation, ActionResult):
        return reservation

    row_results: list[dict[str, Any]] = []
    try:
        row_results = _web_capture_page_row_results(
            project,
            action.kind,
            params,
            row_ids=resolved["row_ids"],
            input_column=resolved["input_column"],
            values=resolved["values"],
            value_refs=resolved["value_refs"],
            fetcher=url_capture_fetcher,
            browser_renderer=url_capture_browser,
        )
        if not row_results:
            return _finalize_web_capture_page_failed_receipt(
                project,
                action,
                params_hash=params_hash,
                project_id=project_id,
                action_id=reservation["action_id"],
                receipt_id=reservation["receipt_id"],
                error=ActionError(
                    code="invalid_input_ref",
                    message=f"{action.kind} has no visible target rows",
                    action_kind=action.kind,
                    field="params.row_ids",
                ),
            )

        if _WEB_CAPTURE_PAGE_FAIL_AFTER_STAGING_FOR_TEST:
            return _finalize_web_capture_page_failed_receipt(
                project,
                action,
                params_hash=params_hash,
                project_id=project_id,
                action_id=reservation["action_id"],
                receipt_id=reservation["receipt_id"],
                error=ActionError(
                    code="project_write_failed",
                    message="project write failed after staging page blobs",
                    action_kind=action.kind,
                    details={"stage": "after_staging_before_finalize"},
                ),
            )

        return _finalize_web_capture_page(
            project,
            action,
            params,
            params_hash=params_hash,
            project_id=project_id,
            action_id=reservation["action_id"],
            receipt_id=reservation["receipt_id"],
            row_ids=resolved["row_ids"],
            input_column=resolved["input_column"],
            row_results=row_results,
        )
    finally:
        _cleanup_web_capture_page_staged_blobs(row_results)


def _resolve_web_capture_page_inputs(
    project: Project,
    action_kind: str,
    params: CapturePlan,
    *,
    admission: bool = True,
) -> dict[str, Any] | ActionError:
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE id=? AND hidden=0",
        (params.sheet_id,),
    ).fetchone()
    if sheet is None:
        return ActionError(
            code="invalid_input_ref",
            message=f"{action_kind} sheet_id does not identify a visible sheet",
            action_kind=action_kind,
            field="params.sheet_id",
        )
    column = project.db.execute(
        "SELECT * FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (params.sheet_id, params.input_column),
    ).fetchone()
    if column is None:
        return ActionError(
            code="invalid_input_ref",
            message=f"{action_kind} input column does not exist on the sheet",
            action_kind=action_kind,
            field="params.input_column",
            details={"missing": [params.input_column]},
        )
    accepted_types = UrlColumn.accepted_column_types
    if accepted_types is not None and column["type"] not in accepted_types:
        return ActionError(
            code="invalid_input_ref",
            message=f"{action_kind} input column must be link or text",
            action_kind=action_kind,
            field="params.input_column",
            details={"column": params.input_column, "type": column["type"]},
        )
    if params.row_ids is None:
        row_ids = project.visible_row_ids(params.sheet_id)
    else:
        row_ids = project.visible_row_ids(params.sheet_id, params.row_ids)
        missing_rows = sorted(set(params.row_ids) - set(row_ids))
        if missing_rows:
            return ActionError(
                code="invalid_input_ref",
                message=f"{action_kind} row_ids must belong to the target sheet",
                action_kind=action_kind,
                field="params.row_ids",
                details={"missing": missing_rows},
            )
    if not row_ids:
        return ActionError(
            code="invalid_input_ref",
            message=f"{action_kind} has no visible target rows",
            action_kind=action_kind,
            field="params.row_ids",
        )
    if admission and params.output_mode == "links":
        existing_sheet = project.db.execute(
            "SELECT id FROM sheets WHERE name=? AND hidden=0",
            (params.links_sheet_name,),
        ).fetchone()
        if existing_sheet is not None:
            return ActionError(
                code="duplicate_sheet_name",
                message=f"{action_kind} links output sheet already exists",
                action_kind=action_kind,
                field="params.links_sheet_name",
                details={"sheet_id": int(existing_sheet["id"])},
            )
    elif admission:
        output_column = project.db.execute(
            "SELECT id, type FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
            (params.sheet_id, params.output_name),
        ).fetchone()
        if (
            output_column is not None
            and output_column["type"] != params.output_column_type
        ):
            return ActionError(
                code="output_column_type_mismatch",
                message=f"{action_kind} existing output column has an incompatible type",
                action_kind=action_kind,
                field="params.output_name",
                details={
                    "column": params.output_name,
                    "current_type": output_column["type"],
                    "expected_type": params.output_column_type,
                },
            )
    values, value_refs = project.get_values_with_refs(
        params.sheet_id,
        int(column["id"]),
        row_ids=row_ids,
    )
    return {
        "input_column": {
            "name": params.input_column,
            "id": int(column["id"]),
            "type": str(column["type"]),
            "sheet_id": params.sheet_id,
        },
        "row_ids": row_ids,
        "values": values,
        "value_refs": value_refs,
    }


def _web_capture_page_row_results(
    project: Project,
    action_kind: str,
    params: CapturePlan,
    *,
    row_ids: list[int],
    input_column: dict[str, Any],
    values: dict[int, Any],
    value_refs: dict[int, dict[str, Any]],
    fetcher: Any | None,
    browser_renderer: Any | None,
) -> list[dict[str, Any]]:
    row_results: list[dict[str, Any]] = []
    for row_id in row_ids:
        if project.effective_network_policy() == "off":
            row_results.append(
                _web_capture_page_error_row(
                    row_id=row_id,
                    input_column=input_column,
                    value=values.get(row_id),
                    error="Project network access is disabled",
                    reason="network_disabled",
                    value_ref=value_refs.get(row_id),
                )
            )
            continue
        raw_url = values.get(row_id)
        if not isinstance(raw_url, str) or not raw_url.strip():
            row_results.append(
                _web_capture_page_error_row(
                    row_id=row_id,
                    input_column=input_column,
                    value=raw_url,
                    error="URL cell is empty or not text",
                    reason="missing_or_non_text_url",
                    value_ref=value_refs.get(row_id),
                )
            )
            continue
        url = raw_url.strip()
        if params.render_mode == "playwright":
            capture_kwargs: dict[str, Any] = {
                "capture_screenshot": False,
                "include_warc": params.include_warc,
                "full_page": params.full_page,
                "max_bytes": params.max_bytes,
                "timeout_ms": params.timeout_ms,
                "render": browser_renderer,
            }
            capture = url_capture.capture_playwright_url(url, **capture_kwargs)
        else:
            capture = url_capture.capture_static_url(
                url,
                max_bytes=params.max_bytes,
                timeout_ms=params.timeout_ms,
                fetch=fetcher,
            )
        if capture.status != "captured":
            row_results.append(
                _web_capture_page_error_row(
                    row_id=row_id,
                    input_column=input_column,
                    value=url,
                    error=capture.error or f"{params.render_mode} URL capture failed",
                    reason=capture.reason or "capture_failed",
                    value_ref=value_refs.get(row_id),
                    capture=capture,
                )
            )
            continue
        if params.output_mode == "links":
            assert capture.html_bytes is not None
            row_results.append(
                {
                    "status": "captured",
                    "row_id": row_id,
                    "input_column": input_column,
                    "value_ref": value_refs.get(row_id),
                    "url": url,
                    "url_hash": url_capture.text_hash(url),
                    "host": urlparse(url).netloc,
                    "final_url": capture.final_url,
                    "final_url_hash": url_capture.text_hash(capture.final_url or url),
                    "canonical_url": capture.canonical_url,
                    "canonical_url_hash": url_capture.text_hash(
                        capture.canonical_url or capture.final_url or url
                    ),
                    "title": capture.title,
                    "captured_at": capture.captured_at,
                    "status_code": capture.status_code,
                    "byte_count": capture.byte_count,
                    "content_type": capture.content_type,
                    "network_metadata": capture.network_metadata,
                    "warnings": capture.warnings,
                    "render_mode": capture.render_mode or params.render_mode,
                    "links": list(capture.links),
                }
            )
            continue
        assert capture.html_bytes is not None
        primary_bytes = capture.html_bytes
        primary_mime = "text/html"
        primary_extension = "html"
        primary_metadata = owned_media_metadata_document(
            probe={"kind": "web_page_capture"},
            owner={"capture_kind": "web_page_html"},
        )
        primary = stage_blob_bytes(
            project,
            primary_bytes,
            role=params.primary_role,
            filename=_web_capture_page_filename(
                params.output_name, row_id, primary_extension
            ),
            mime=primary_mime,
            source_url=capture.final_url,
            metadata={
                **primary_metadata,
                "render_mode": params.render_mode,
                "input_url_hash": url_capture.text_hash(url),
                "final_url_hash": url_capture.text_hash(capture.final_url or url),
                "canonical_url": capture.canonical_url,
                "title": capture.title,
                "captured_at": capture.captured_at,
            },
        )
        supplemental: list[RowBlobPlan] = []
        if params.include_warc and capture.warc_bytes is not None:
            supplemental.append(
                stage_blob_bytes(
                    project,
                    capture.warc_bytes,
                    role="warc",
                    filename=_web_capture_page_filename(
                        params.output_name, row_id, "warc"
                    ),
                    mime=capture.warc_mime or url_capture.WARC_MIME,
                    source_url=capture.final_url,
                    metadata=owned_media_metadata_document(
                        probe={"kind": "web_page_capture_warc"},
                        owner={
                            "capture_kind": "web_page_warc",
                            "render_mode": params.render_mode,
                            "input_url_hash": url_capture.text_hash(url),
                            "final_url_hash": url_capture.text_hash(
                                capture.final_url or url
                            ),
                            "canonical_url": capture.canonical_url,
                            "title": capture.title,
                            "captured_at": capture.captured_at,
                        },
                    ),
                )
            )
        row_results.append(
            {
                "status": "captured",
                "row_id": row_id,
                "input_column": input_column,
                "value_ref": value_refs.get(row_id),
                "url": url,
                "url_hash": url_capture.text_hash(url),
                "host": urlparse(url).netloc,
                "final_url": capture.final_url,
                "final_url_hash": url_capture.text_hash(capture.final_url or url),
                "canonical_url": capture.canonical_url,
                "canonical_url_hash": url_capture.text_hash(
                    capture.canonical_url or capture.final_url or url
                ),
                "title": capture.title,
                "author": capture.author,
                "published_at": capture.published_at,
                "captured_at": capture.captured_at,
                "status_code": capture.status_code,
                "byte_count": capture.byte_count,
                "content_type": capture.content_type,
                "network_metadata": capture.network_metadata,
                "warnings": capture.warnings,
                "render_mode": capture.render_mode or params.render_mode,
                "include_warc": params.include_warc,
                "primary_role": params.primary_role,
                "full_page": params.full_page,
                "blob_output": RowBlobOutput(
                    primary=primary,
                    supplemental=tuple(supplemental),
                    facts={"output_name": params.output_name},
                ),
            }
        )
    return row_results


def _cleanup_web_capture_page_staged_blobs(row_results: list[dict[str, Any]]) -> None:
    for row in row_results:
        output = row.get("blob_output")
        if not isinstance(output, RowBlobOutput):
            continue
        for plan in (output.primary, *output.supplemental):
            try:
                Path(plan.staged_path).unlink(missing_ok=True)
            except OSError:
                logger.debug(
                    "web_capture_page_staged_blob_cleanup_failed",
                    exc_info=True,
                    extra={
                        "event": "web_capture_page_staged_blob_cleanup_failed",
                        "path": str(plan.staged_path),
                    },
                )


def _web_capture_page_error_row(
    *,
    row_id: int,
    input_column: dict[str, Any],
    value: Any,
    error: str,
    reason: str,
    value_ref: dict[str, Any] | None,
    capture: url_capture.StaticUrlCaptureResult | None = None,
) -> dict[str, Any]:
    final_url = capture.final_url if capture else None
    canonical_url = capture.canonical_url if capture else None
    return {
        "status": "error",
        "row_id": row_id,
        "input_column": input_column,
        "value_ref": value_ref,
        "url": value,
        "url_hash": url_capture.text_hash(str(value)) if value else None,
        "host": urlparse(str(value)).netloc if isinstance(value, str) else None,
        "final_url": final_url,
        "final_url_hash": url_capture.text_hash(final_url) if final_url else None,
        "canonical_url": canonical_url,
        "canonical_url_hash": (
            url_capture.text_hash(canonical_url) if canonical_url else None
        ),
        "status_code": capture.status_code if capture else None,
        "byte_count": capture.byte_count if capture else None,
        "content_type": capture.content_type if capture else None,
        "network_metadata": capture.network_metadata if capture else {},
        "warnings": capture.warnings if capture else [],
        "render_mode": capture.render_mode if capture else None,
        "error": error,
        "reason": reason,
    }


def _finalize_web_capture_page_failed_receipt(
    project: Project,
    action: _TypedProjectEnvelope,
    *,
    params_hash: str,
    project_id: str,
    action_id: str,
    receipt_id: str,
    error: ActionError,
) -> ActionResult:
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="failed",
        errors=[error],
    )
    try:
        project.db.execute("BEGIN IMMEDIATE")
        updated = ReceiptStore(project).update_body_status(
            receipt,
            require_status="running",
            commit=False,
        )
        if not updated:
            raise RuntimeError(f"{action.kind} running receipt could not be finalized")
        project.db.commit()
    except Exception:
        project.db.rollback()
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action.kind},
        )
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="project_write_failed",
                message="project write failed",
                action_kind=action.kind,
            ),
        )
    return _result_from_receipt(receipt)


def _finalize_web_capture_page(
    project: Project,
    action: _TypedProjectEnvelope,
    params: CapturePlan,
    *,
    params_hash: str,
    project_id: str,
    action_id: str,
    receipt_id: str,
    row_ids: list[int],
    input_column: dict[str, Any],
    row_results: list[dict[str, Any]],
) -> ActionResult:
    captured = [row for row in row_results if row.get("status") == "captured"]
    if not captured:
        details = _capture_page_failure_details(row_results)
        logger.warning(
            "%s failed for every selected URL: %s",
            action.kind,
            details.get("error_examples") or details["status_counts"],
        )
        return _finalize_web_capture_page_failed_receipt(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            error=ActionError(
                code="url_capture_failed",
                message=f"{action.kind} failed for every selected URL",
                action_kind=action.kind,
                details=details,
            ),
        )

    status_counts = _capture_page_status_counts(row_results)
    status = _capture_page_action_status(status_counts)
    errors = _web_capture_page_receipt_errors(action, status_counts)
    if params.output_mode == "links":
        return _finalize_web_capture_page_links(
            project,
            action,
            params,
            params_hash=params_hash,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            row_ids=row_ids,
            input_column=input_column,
            row_results=row_results,
            status=status,
            status_counts=status_counts,
            errors=errors,
        )

    try:
        project.db.execute("BEGIN IMMEDIATE")
        output_column_id = _web_capture_page_add_output_column(project, params)
        blob_refs_by_row: dict[int, dict[str, Any]] = {}
        for row in captured:
            output = row.get("blob_output")
            if not isinstance(output, RowBlobOutput):
                raise RuntimeError("captured row is missing blob output")
            primary_digest = commit_row_blob_plan(project, output.primary, commit=False)
            if primary_digest != output.primary.content_digest:
                raise RuntimeError("primary blob digest changed during commit")
            supplemental_refs = []
            for plan in output.supplemental:
                digest = commit_row_blob_plan(project, plan, commit=False)
                if digest != plan.content_digest:
                    raise RuntimeError("supplemental blob digest changed during commit")
                supplemental_refs.append(_web_capture_page_blob_ref(row, plan, digest))
            blob_refs_by_row[int(row["row_id"])] = {
                "primary": _web_capture_page_blob_ref(
                    row,
                    output.primary,
                    primary_digest,
                    kind="web_capture_page_artifact",
                ),
                "supplemental": supplemental_refs,
            }

        op_id = _web_capture_page_apply_edits_in_txn(
            project,
            action,
            params,
            output_column_id=output_column_id,
            row_results=row_results,
            blob_refs_by_row=blob_refs_by_row,
        )
        output_ref = _web_capture_page_output_ref(
            project,
            params,
            output_column_id=output_column_id,
            row_ids=row_ids,
            params_hash=params_hash,
            row_results=row_results,
        )
        evidence_refs = _write_web_capture_page_evidence(
            project,
            action_kind=action.kind,
            row_results=row_results,
            input_column=input_column,
            op_id=op_id,
            receipt_id=receipt_id,
            blob_refs_by_row=blob_refs_by_row,
        )
        receipt = _web_capture_page_receipt(
            action=action,
            params=params,
            action_id=action_id,
            project_id=project_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            row_ids=row_ids,
            input_column=input_column,
            output_ref=output_ref,
            row_results=row_results,
            evidence_refs=evidence_refs,
            op_id=op_id,
            status=status,
            status_counts=status_counts,
            errors=errors,
        )
        updated = ReceiptStore(project).update_body_status(
            receipt,
            require_status="running",
            commit=False,
        )
        if not updated:
            raise RuntimeError(f"{action.kind} running receipt could not be finalized")
        project.db.commit()
    except Exception:
        project.db.rollback()
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action.kind},
        )
        return _finalize_web_capture_page_failed_receipt(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            error=ActionError(
                code="project_write_failed",
                message="project write failed",
                action_kind=action.kind,
            ),
        )
    return _result_from_receipt(receipt)


def _finalize_web_capture_page_links(
    project: Project,
    action: _TypedProjectEnvelope,
    params: CapturePlan,
    *,
    params_hash: str,
    project_id: str,
    action_id: str,
    receipt_id: str,
    row_ids: list[int],
    input_column: dict[str, Any],
    row_results: list[dict[str, Any]],
    status: str,
    status_counts: dict[str, int],
    errors: list[ActionError],
) -> ActionResult:
    names = {
        key: params.output_names.get(key, key)
        for key in ("url", "anchor_text", "source_url")
    }
    materialized_rows = [
        SingleParentMaterializedRow(
            parent_row_id=int(row["row_id"]),
            values={
                names["url"]: link["url"],
                names["anchor_text"]: link.get("anchor_text", ""),
                names["source_url"]: row.get("final_url") or row["url"],
            },
        )
        for row in row_results
        if row.get("status") == "captured"
        for link in row.get("links", [])
    ]
    try:
        project.db.execute("BEGIN IMMEDIATE")
        existing_sheet = project.db.execute(
            "SELECT id FROM sheets WHERE name=? AND hidden=0",
            (params.links_sheet_name,),
        ).fetchone()
        if existing_sheet is not None:
            raise ValueError("links output sheet already exists")
        op_spec = dict(params.request)
        op_spec["params_hash"] = params_hash
        write = write_single_parent_child_sheet(
            project.db.cursor(),
            SingleParentChildSheetPlan(
                action_kind=action.kind,
                label=f"{action.kind} {params.links_sheet_name}",
                target_sheet_name=params.links_sheet_name,
                parent_sheet_id=params.sheet_id,
                op_spec=op_spec,
                columns=[
                    MaterializedColumnSpec(
                        name=names["url"], type="link", ai_generated=True
                    ),
                    MaterializedColumnSpec(
                        name=names["anchor_text"], type="text", ai_generated=True
                    ),
                    MaterializedColumnSpec(
                        name=names["source_url"], type="link", ai_generated=True
                    ),
                ],
                rows=materialized_rows,
            ),
        )
        receipt = _web_capture_page_links_receipt(
            project=project,
            action=action,
            params=params,
            action_id=action_id,
            project_id=project_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            row_ids=row_ids,
            input_column=input_column,
            row_results=row_results,
            write=write,
            status=status,
            status_counts=status_counts,
            errors=errors,
        )
        updated = ReceiptStore(project).update_body_status(
            receipt,
            require_status="running",
            commit=False,
        )
        if not updated:
            raise RuntimeError(f"{action.kind} running receipt could not be finalized")
        project.db.commit()
    except Exception:
        project.db.rollback()
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action.kind},
        )
        return _finalize_web_capture_page_failed_receipt(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            error=ActionError(
                code="project_write_failed",
                message="project write failed",
                action_kind=action.kind,
            ),
        )
    return _result_from_receipt(receipt)


def _web_capture_page_add_output_column(
    project: Project,
    params: CapturePlan,
) -> int:
    row = project.db.execute(
        "SELECT id, type FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (params.sheet_id, params.output_name),
    ).fetchone()
    if row is not None:
        if row["type"] != params.output_column_type:
            raise ValueError(
                f"output column is {row['type']}, expected {params.output_column_type}"
            )
        return int(row["id"])
    cur = project.db.execute(
        "INSERT INTO columns (sheet_id, name, type, ai_generated, format, hidden, "
        "position) VALUES (?, ?, ?, 1, NULL, 0, "
        "(SELECT COALESCE(MAX(position),0)+1 FROM columns WHERE sheet_id=?))",
        (
            params.sheet_id,
            params.output_name,
            params.output_column_type,
            params.sheet_id,
        ),
    )
    return int(cur.lastrowid)


def _web_capture_page_apply_edits_in_txn(
    project: Project,
    action: _TypedProjectEnvelope,
    params: CapturePlan,
    *,
    output_column_id: int,
    row_results: list[dict[str, Any]],
    blob_refs_by_row: dict[int, dict[str, Any]],
) -> int:
    project.db.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
    cur = project.db.execute(
        "INSERT INTO ops (kind, label, spec, barrier) VALUES (?, ?, ?, 0)",
        (
            action.kind,
            f"{action.kind} {params.output_name}",
            json.dumps(params.request, sort_keys=True),
        ),
    )
    op_id = int(cur.lastrowid)
    project.db.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
    edits = [
        EditCellWrite(
            row_id=int(row["row_id"]),
            column_id=output_column_id,
            value=_web_capture_page_cell(
                row, blob_refs_by_row[int(row["row_id"])]["primary"]
            ),
        )
        for row in row_results
        if row.get("status") == "captured"
    ]
    insert_edits(project.db, op_id=op_id, edits=edits)
    return op_id


def _web_capture_page_cell(
    row: dict[str, Any],
    blob_ref: dict[str, Any],
) -> dict[str, Any]:
    network = dict(row.get("network_metadata") or {})
    network.setdefault("method", "GET")
    network.setdefault("render_mode", row.get("render_mode") or "static")
    network.setdefault("status_code", row.get("status_code"))
    network.setdefault("byte_count", row.get("byte_count"))
    network.setdefault("content_type", row.get("content_type"))
    if row.get("include_warc"):
        network["include_warc"] = True
    return {
        "kind": "web_page_capture",
        "blob": blob_ref["blob_hash"],
        "filename": blob_ref["filename"],
        "mime": blob_ref["mime"],
        "size": blob_ref["size"],
        "final_url": row.get("final_url"),
        "canonical_url": row.get("canonical_url"),
        "title": row.get("title"),
        "author": row.get("author"),
        "published_at": row.get("published_at"),
        "captured_at": row.get("captured_at"),
        "network": network,
    }


def _web_capture_page_blob_ref(
    row: dict[str, Any],
    plan: RowBlobPlan,
    digest: str,
    *,
    kind: str = "web_capture_page_supplemental_artifact",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "role": plan.role,
        "blob_hash": digest,
        "filename": plan.filename,
        "mime": plan.mime,
        "size": Path(plan.staged_path).stat().st_size,
        "row_id": int(row["row_id"]),
        "source_url_hash": row.get("final_url_hash") or row.get("url_hash"),
    }


def _web_capture_page_output_ref(
    project: Project,
    params: CapturePlan,
    *,
    output_column_id: int,
    row_ids: list[int],
    params_hash: str,
    row_results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "kind": "web_capture_page_output_column",
        "sheet_id": params.sheet_id,
        "column_id": output_column_id,
        "column_type": params.output_column_type,
        "name": params.output_name,
        "row_ids": row_ids,
        "captured_rows": [
            int(row["row_id"]) for row in row_results if row.get("status") == "captured"
        ],
        "failed_rows": [
            int(row["row_id"]) for row in row_results if row.get("status") == "error"
        ],
        "value_hash": media_output_column_value_hash(
            project,
            sheet_id=params.sheet_id,
            column_id=output_column_id,
            row_ids=row_ids,
        ),
        "params_hash": params_hash,
    }


def _write_web_capture_page_evidence(
    project: Project,
    *,
    action_kind: str,
    row_results: list[dict[str, Any]],
    input_column: dict[str, Any],
    op_id: int,
    receipt_id: str,
    blob_refs_by_row: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for row in row_results:
        if row.get("status") != "captured":
            continue
        row_id = int(row["row_id"])
        blob_refs = blob_refs_by_row[row_id]
        spans: list[dict[str, Any]] = []
        for rank, blob_ref in enumerate(
            [blob_refs["primary"], *blob_refs["supplemental"]],
            start=1,
        ):
            artifact = record_source_artifact(
                project,
                artifact_kind=_web_capture_page_artifact_kind(blob_ref["role"]),
                media_type=blob_ref["mime"],
                blob_hash=blob_ref["blob_hash"],
                source_url=row.get("final_url") or row.get("url"),
                canonical_url=row.get("canonical_url"),
                title=row.get("title"),
                filename=blob_ref["filename"],
                source_sheet_id=input_column["sheet_id"],
                source_row_id=row_id,
                source_column_id=input_column["id"],
                metadata={
                    "role": blob_ref["role"],
                    "render_mode": row.get("render_mode") or "static",
                    "status_code": row.get("status_code"),
                    "content_type": row.get("content_type"),
                    "byte_count": row.get("byte_count"),
                    "warnings": row.get("warnings") or [],
                },
            )
            span = record_source_span(
                project,
                artifact_id=artifact["id"],
                span_kind=_web_capture_page_span_kind(blob_ref["role"]),
                selector={"blob_hash": blob_ref["blob_hash"]},
                text_layer_hash=(
                    blob_ref["blob_hash"] if blob_ref["role"] == "html" else None
                ),
                snippet=row.get("title"),
                metadata={
                    "role": blob_ref["role"],
                    "mime": blob_ref["mime"],
                    "render_mode": row.get("render_mode") or "static",
                },
            )
            spans.append({"span_id": span["id"], "rank": rank})
            refs.append(
                blob_ref
                | {
                    "artifact_id": artifact["id"],
                    "artifact_stable_id": artifact["stable_id"],
                }
            )
        link = record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref=row.get("value_ref") or {},
            spans=spans,
            sheet_id=input_column["sheet_id"],
            row_id=row_id,
            column_id=input_column["id"],
            op_id=op_id,
            receipt_id=receipt_id,
            link_role="web_capture_page",
            producer={
                "action_kind": action_kind,
                "render_mode": row.get("render_mode") or "static",
                "warnings": row.get("warnings") or [],
            },
            metadata={
                f"{row['primary_role']}_blob_hash": blob_refs["primary"]["blob_hash"],
                "url_hash": row.get("url_hash"),
                "final_url_hash": row.get("final_url_hash"),
                "canonical_url_hash": row.get("canonical_url_hash"),
            },
        )
        refs.append(
            {
                "kind": "web_capture_page_evidence_link",
                "sheet_id": input_column["sheet_id"],
                "row_id": row_id,
                "column_id": input_column["id"],
                "evidence_link_id": link["id"],
                "stable_id": link["stable_id"],
            }
        )
    return refs


def _web_capture_page_artifact_kind(role: str) -> str:
    return {
        "html": "capture_html",
        "warc": "capture_warc",
    }.get(role, f"capture_{role}")


def _web_capture_page_span_kind(role: str) -> str:
    return {
        "html": "html_document",
        "warc": "warc",
    }.get(role, "blob")


def _web_capture_page_links_receipt(
    *,
    project: Project,
    action: _TypedProjectEnvelope,
    params: CapturePlan,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    row_ids: list[int],
    input_column: dict[str, Any],
    row_results: list[dict[str, Any]],
    write: Any,
    status: str,
    status_counts: dict[str, int],
    errors: list[ActionError],
) -> Receipt:
    names = {
        key: params.output_names.get(key, key)
        for key in ("url", "anchor_text", "source_url")
    }
    rows_ref = dict(write.materialized_rows_ref)
    rows_ref["value_hashes"] = {
        name: media_output_column_value_hash(
            project,
            sheet_id=write.sheet_id,
            column_id=int(write.column_ids[names[name]]),
            row_ids=list(write.row_ids),
        )
        for name in ("url", "anchor_text", "source_url")
    }
    outputs = [
        ReceiptIO(
            name=params.links_sheet_name,
            ref=dict(write.materialized_sheet_ref),
        ),
        *[
            ReceiptIO(
                name=names[name],
                ref={**write.materialized_column_refs[names[name]], "role": name},
            )
            for name in ("url", "anchor_text", "source_url")
        ],
        ReceiptIO(name="rows", ref=rows_ref),
    ]
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=[write.op_id],
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status=status,
        inputs=[
            ReceiptIO(
                name="input_rows",
                ref={
                    "kind": "web_capture_page_input_rows",
                    "sheet_id": input_column["sheet_id"],
                    "row_ids": row_ids,
                    "params_hash": params_hash,
                },
            ),
            ReceiptIO(
                name=f"input_column.{input_column['name']}",
                ref={
                    "kind": "web_capture_page_input_column",
                    "sheet_id": input_column["sheet_id"],
                    "column_id": input_column["id"],
                    "name": input_column["name"],
                    "type": input_column["type"],
                },
            ),
        ],
        outputs=outputs,
        provider_use=_web_capture_page_provider_use(row_results),
        evidence=[
            ReceiptEvidence(
                ref={
                    "kind": "web_capture_page_link_extraction",
                    "sheet_id": write.sheet_id,
                    "source_row_count": len(row_ids),
                    "link_row_count": len(write.row_ids),
                    "status_counts": status_counts,
                    "render_mode": params.render_mode,
                    "output_mode": params.output_mode,
                    "include_warc": params.include_warc,
                    "params_hash": params_hash,
                }
            ),
            ReceiptEvidence(
                ref=dict(write.lineage_parent_rows_ref), retention="pinned"
            ),
            *[
                ReceiptEvidence(
                    ref={
                        "kind": "web_capture_page_error",
                        "row_id": int(row["row_id"]),
                        "output_sheet_id": write.sheet_id,
                        "host": row.get("host"),
                        "url_hash": row.get("url_hash"),
                        "reason": row.get("reason"),
                        "error": row.get("error"),
                    }
                )
                for row in row_results
                if row.get("status") == "error"
            ],
        ],
        errors=errors,
    )


def _web_capture_page_receipt(
    *,
    action: _TypedProjectEnvelope,
    params: CapturePlan,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    row_ids: list[int],
    input_column: dict[str, Any],
    output_ref: dict[str, Any],
    row_results: list[dict[str, Any]],
    evidence_refs: list[dict[str, Any]],
    op_id: int,
    status: str,
    status_counts: dict[str, int],
    errors: list[ActionError],
) -> Receipt:
    request_counts: dict[str, Any] = {
        "kind": "web_capture_page_request_counts",
        "sheet_id": output_ref["sheet_id"],
        "row_count": len(row_ids),
        "status_counts": status_counts,
        "render_mode": params.render_mode,
        "output_mode": params.output_mode,
        "include_warc": params.include_warc,
        "ssrf_guarded": True,
        "http_downloader": _capture_page_downloader_name(params),
        "params_hash": params_hash,
    }
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=[op_id],
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status=status,
        inputs=[
            ReceiptIO(
                name="input_rows",
                ref={
                    "kind": "web_capture_page_input_rows",
                    "sheet_id": output_ref["sheet_id"],
                    "row_ids": row_ids,
                    "params_hash": params_hash,
                },
            ),
            ReceiptIO(
                name=f"input_column.{input_column['name']}",
                ref={
                    "kind": "web_capture_page_input_column",
                    "sheet_id": output_ref["sheet_id"],
                    "column_id": input_column["id"],
                    "name": input_column["name"],
                    "type": input_column["type"],
                },
            ),
        ],
        outputs=[ReceiptIO(name=output_ref["name"], ref=output_ref)],
        provider_use=_web_capture_page_provider_use(row_results),
        evidence=[
            ReceiptEvidence(ref=request_counts),
            *[ReceiptEvidence(ref=ref) for ref in evidence_refs],
            *[
                ReceiptEvidence(ref=ref)
                for ref in _web_capture_page_error_refs(row_results, output_ref)
            ],
        ],
        errors=errors,
    )


def _web_capture_page_provider_use(
    row_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    captured = [row for row in row_results if row.get("status") == "captured"]
    if not captured:
        return []
    render_modes = sorted(
        {
            str(row.get("render_mode") or "static")
            for row in captured
            if row.get("render_mode") or row.get("status") == "captured"
        }
    )
    render_mode = render_modes[0] if len(render_modes) == 1 else "mixed"
    return [
        {
            "provider": "url_capture",
            "service": "playwright" if render_mode == "playwright" else "httpx",
            "external_api": True,
            "method": "GET",
            "render_mode": render_mode,
            "request_count": len(captured),
            "cost_actual": 0.0,
            "rows": [
                {
                    "row_id": int(row["row_id"]),
                    "host": row.get("host"),
                    "url_hash": row.get("url_hash"),
                    "final_url_hash": row.get("final_url_hash"),
                    "canonical_url_hash": row.get("canonical_url_hash"),
                    "status_code": row.get("status_code"),
                    "byte_count": row.get("byte_count"),
                    "content_type": row.get("content_type"),
                    "warnings": row.get("warnings") or [],
                    **_web_capture_page_primary_provider_facts(row),
                    **_web_capture_page_supplemental_provider_hashes(row),
                }
                for row in captured
            ],
        }
    ]


def _web_capture_page_primary_provider_facts(
    row: dict[str, Any],
) -> dict[str, Any]:
    output = row.get("blob_output")
    if isinstance(output, RowBlobOutput):
        return {f"{row['primary_role']}_blob_hash": output.primary.content_digest}
    return {"links_extracted": len(row.get("links") or [])}


def _web_capture_page_supplemental_provider_hashes(
    row: dict[str, Any],
) -> dict[str, str]:
    output = row.get("blob_output")
    if not isinstance(output, RowBlobOutput):
        return {}
    return {
        f"{plan.role}_blob_hash": plan.content_digest
        for plan in output.supplemental
        if plan.role == "warc"
    }


def _web_capture_page_error_refs(
    row_results: list[dict[str, Any]],
    output_ref: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "kind": "web_capture_page_error",
            "row_id": int(row["row_id"]),
            "output_column_id": output_ref["column_id"],
            "host": row.get("host"),
            "url_hash": row.get("url_hash"),
            "reason": row.get("reason"),
            "error": row.get("error"),
        }
        for row in row_results
        if row.get("status") == "error"
    ]


def _web_capture_page_receipt_errors(
    action: _TypedProjectEnvelope,
    status_counts: dict[str, int],
) -> list[ActionError]:
    if _capture_page_action_status(status_counts) != "failed":
        return []
    return [
        ActionError(
            code="url_capture_failed",
            message=f"{action.kind} failed for every selected URL",
            action_kind=action.kind,
            details={"status_counts": status_counts},
        )
    ]


def _web_capture_page_result_from_existing_receipt(
    project: Project,
    existing: Any,
    *,
    params_hash: str,
    project_id: str,
    action: _TypedProjectEnvelope,
) -> ActionResult:
    if existing["status"] == "failed" and existing["params_hash"] == params_hash:
        return _result_from_receipt(
            Receipt.model_validate(json.loads(existing["body"]))
        )

    def replay_error(receipt: Receipt) -> ActionError | None:
        return _web_capture_page_replay_error(project, receipt)

    return _reserved_receipt_result_from_existing(
        project,
        existing,
        params_hash=params_hash,
        project_id=project_id,
        action=action,
        replay_error_fn=replay_error,
    )


def _web_capture_page_replay_error(
    project: Project,
    receipt: Receipt,
) -> ActionError | None:
    links_error = _web_capture_page_links_replay_error(project, receipt)
    if links_error is not None:
        return links_error
    stale = partial(receipt_stale_replay_error, receipt)
    replay = f"{receipt.action_kind} replay "
    seen_output = False
    for output in receipt.outputs:
        ref = output.ref
        if ref.get("kind") == "materialized_sheet":
            seen_output = True
            sheet_id = ref.get("sheet_id")
            op_id = ref.get("op_id")
            if not isinstance(sheet_id, int) or not isinstance(op_id, int):
                return stale(replay + "receipt has invalid links-sheet refs")
            sheet = project.db.execute(
                "SELECT id, name, parent_sheet_id FROM sheets WHERE id=? AND hidden=0",
                (sheet_id,),
            ).fetchone()
            if (
                sheet is None
                or sheet["name"] != output.name
                or sheet["parent_sheet_id"] != ref.get("parent_sheet_id")
            ):
                return stale(replay + "links output sheet is missing or renamed")
            op = project.db.execute(
                "SELECT id FROM ops WHERE id=? AND status='applied'", (op_id,)
            ).fetchone()
            if op is None:
                return stale(replay + "links output operation is missing")
            continue
        if ref.get("kind") == "materialized_column":
            expected_type = {
                "url": "link",
                "anchor_text": "text",
                "source_url": "link",
            }.get(output.name)
            column = project.db.execute(
                "SELECT id, type FROM columns WHERE id=? AND sheet_id=? AND hidden=0",
                (ref.get("column_id"), ref.get("sheet_id")),
            ).fetchone()
            if column is None or (expected_type and column["type"] != expected_type):
                return stale(
                    replay + "links output column is missing", output=output.name
                )
            continue
        if ref.get("kind") != "web_capture_page_output_column":
            continue
        seen_output = True
        sheet_id = ref.get("sheet_id")
        column_id = ref.get("column_id")
        if not isinstance(sheet_id, int) or not isinstance(column_id, int):
            return stale(replay + "receipt has invalid output refs", output=output.name)
        row_ids = media_replay_row_ids(ref, receipt=receipt, output_name=output.name)
        if isinstance(row_ids, ActionError):
            return row_ids
        column = _replay_output_column_row(
            project, receipt, column_id=column_id, sheet_id=sheet_id
        )
        expected_type = ref.get("column_type")
        if not isinstance(expected_type, str):
            return stale(
                replay + "receipt output is missing column type",
                column_id=column_id,
            )
        if column is None or column["type"] != expected_type:
            return stale(replay + "output column is missing", column_id=column_id)
        if ref.get("value_hash") != media_output_column_value_hash(
            project,
            sheet_id=sheet_id,
            column_id=column_id,
            row_ids=row_ids,
        ):
            return stale(replay + "output values changed", column_id=column_id)
    for evidence in receipt.evidence:
        ref = evidence.ref
        if ref.get("kind") in {
            "web_capture_page_artifact",
            "web_capture_page_supplemental_artifact",
        }:
            blob_hash = ref.get("blob_hash")
            if not isinstance(blob_hash, str) or not blob_hash:
                return stale(replay + "blob ref is incomplete", ref=ref)
            blob = MediaBlobStore(project).blob_row(blob_hash)
            if blob is None:
                return stale(replay + "capture blob is missing", blob_hash=blob_hash)
            try:
                with project.materialize_blob(blob_hash):
                    pass
            except BlobNotFoundError:
                return stale(replay + "capture blob is missing", blob_hash=blob_hash)
        if ref.get("kind") == "web_capture_page_evidence_link":
            stable_id = ref.get("stable_id")
            if not isinstance(stable_id, str) or not stable_id:
                return stale(replay + "evidence ref is incomplete", ref=ref)
            link = evidence_link_by_stable_id(project, stable_id)
            if link is None:
                return stale(replay + "evidence link is missing", stable_id=stable_id)
    if receipt.status in {"completed", "partial"} and not seen_output:
        return stale(replay + "receipt has no output column ref")
    return None


def _web_capture_page_links_replay_error(
    project: Project,
    receipt: Receipt,
) -> ActionError | None:
    """Validate the complete child-sheet output and its pinned row lineage."""

    stale = partial(receipt_stale_replay_error, receipt)
    sheet_outputs = [
        output
        for output in receipt.outputs
        if output.ref.get("kind") == "materialized_sheet"
    ]
    column_output_list = [
        output
        for output in receipt.outputs
        if output.ref.get("kind") == "materialized_column"
    ]
    column_outputs = {output.ref.get("role"): output for output in column_output_list}
    rows_outputs = [
        output
        for output in receipt.outputs
        if output.ref.get("kind") == "materialized_rows"
    ]
    lineage_evidence = [
        evidence
        for evidence in receipt.evidence
        if evidence.ref.get("kind") == "lineage_parent_rows"
    ]
    if not any((sheet_outputs, column_outputs, rows_outputs, lineage_evidence)):
        return None
    if (
        len(sheet_outputs) != 1
        or len(column_output_list) != 3
        or set(column_outputs) != {"url", "anchor_text", "source_url"}
        or len(rows_outputs) != 1
        or rows_outputs[0].name != "rows"
        or len(lineage_evidence) != 1
        or lineage_evidence[0].retention != "pinned"
    ):
        return stale("links output receipt lacks complete materialized lineage refs")

    sheet_output = sheet_outputs[0]
    sheet_ref = sheet_output.ref
    rows_ref = rows_outputs[0].ref
    lineage_ref = lineage_evidence[0].ref
    sheet_id = _positive_receipt_int(sheet_ref.get("sheet_id"))
    op_id = _positive_receipt_int(sheet_ref.get("op_id"))
    parent_sheet_id = _positive_receipt_int(sheet_ref.get("parent_sheet_id"))
    row_ids = _positive_receipt_int_list(rows_ref.get("row_ids"))
    parent_row_ids = _positive_receipt_int_list(rows_ref.get("parent_row_ids"))
    pairs = lineage_ref.get("pairs")
    value_hashes = rows_ref.get("value_hashes")
    if (
        sheet_id is None
        or op_id is None
        or parent_sheet_id is None
        or rows_ref.get("sheet_id") != sheet_id
        or rows_ref.get("op_id") != op_id
        or lineage_ref.get("child_sheet_id") != sheet_id
        or lineage_ref.get("parent_sheet_id") != parent_sheet_id
        or lineage_ref.get("op_id") != op_id
        or row_ids is None
        or parent_row_ids is None
        or len(row_ids) != len(parent_row_ids)
        or len(set(row_ids)) != len(row_ids)
        or not isinstance(pairs, list)
        or not isinstance(value_hashes, dict)
        or set(value_hashes) != {"url", "anchor_text", "source_url"}
        or any(not isinstance(value, str) for value in value_hashes.values())
    ):
        return stale("links output receipt has invalid materialized lineage refs")
    expected_pairs = [
        {"child_row_id": row_id, "parent_row_id": parent_row_id}
        for row_id, parent_row_id in zip(row_ids, parent_row_ids, strict=True)
    ]
    if pairs != expected_pairs:
        return stale("links output receipt lineage pairs changed")

    sheet = project.db.execute(
        "SELECT id, name, parent_sheet_id, parent_op_id FROM sheets "
        "WHERE id=? AND hidden=0",
        (sheet_id,),
    ).fetchone()
    op = project.db.execute(
        "SELECT id FROM ops WHERE id=? AND status='applied'", (op_id,)
    ).fetchone()
    if (
        sheet is None
        or sheet["name"] != sheet_output.name
        or sheet["parent_sheet_id"] != parent_sheet_id
        or sheet["parent_op_id"] != op_id
        or op is None
    ):
        return stale("links output sheet or operation is missing")

    rows = project.db.execute(
        "SELECT id, parent_row_id FROM rows "
        "WHERE sheet_id=? AND hidden=0 ORDER BY position, id",
        (sheet_id,),
    ).fetchall()
    actual_row_ids = [int(row["id"]) for row in rows]
    actual_parent_row_ids = [
        int(row["parent_row_id"]) if row["parent_row_id"] is not None else None
        for row in rows
    ]
    if actual_row_ids != row_ids or actual_parent_row_ids != parent_row_ids:
        return stale("links output rows or parent lineage changed")

    expected_types = {"url": "link", "anchor_text": "text", "source_url": "link"}
    for name, expected_type in expected_types.items():
        ref = column_outputs[name].ref
        column_id = _positive_receipt_int(ref.get("column_id"))
        if (
            column_id is None
            or ref.get("sheet_id") != sheet_id
            or ref.get("op_id") != op_id
        ):
            return stale("links output column ref is invalid", output=name)
        column = project.db.execute(
            "SELECT id, name, type FROM columns WHERE id=? AND sheet_id=? AND hidden=0",
            (column_id, sheet_id),
        ).fetchone()
        if (
            column is None
            or column["name"] != column_outputs[name].name
            or column["type"] != expected_type
        ):
            return stale("links output column is missing or changed", output=name)
        if value_hashes[name] != media_output_column_value_hash(
            project,
            sheet_id=sheet_id,
            column_id=column_id,
            row_ids=row_ids,
        ):
            return stale("links output values changed", output=name)
    return None


def _positive_receipt_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _positive_receipt_int_list(value: Any) -> list[int] | None:
    if not isinstance(value, list):
        return None
    parsed = [_positive_receipt_int(item) for item in value]
    if any(item is None for item in parsed):
        return None
    return [int(item) for item in parsed if item is not None]


def _web_capture_page_filename(
    output_name: str,
    row_id: int,
    extension: str,
) -> str:
    safe_name = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in output_name
    ).strip("-")
    return f"{safe_name or 'page'}-row-{row_id}.{extension}"


def _capture_page_status_counts(
    row_results: list[dict[str, Any]],
) -> dict[str, int]:
    status_counts: dict[str, int] = {}
    for row in row_results:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return status_counts


def _capture_page_failure_details(
    row_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Status counts plus WHY the rows failed.

    Every error row already carries a ``reason`` and the originating ``error``
    text, but only the counts used to reach the receipt — so an environment
    fault that failed all rows for one nameable cause ("install Playwright")
    surfaced as an unactionable "failed for every selected URL". Reasons are
    counted rather than listed per row so the payload stays bounded, and one
    example message per reason is carried because the reason alone is a slug.
    """

    reason_counts: dict[str, int] = {}
    example_by_reason: dict[str, str] = {}
    for row in row_results:
        if row.get("status") != "error":
            continue
        reason = str(row.get("reason") or "capture_failed")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        message = row.get("error")
        if isinstance(message, str) and message and reason not in example_by_reason:
            example_by_reason[reason] = message
    details: dict[str, Any] = {
        "status_counts": _capture_page_status_counts(row_results)
    }
    if reason_counts:
        details["error_reasons"] = reason_counts
        details["error_examples"] = example_by_reason
    return details


def _capture_page_action_status(status_counts: dict[str, int]) -> str:
    successes = status_counts.get("captured", 0)
    errors = status_counts.get("error", 0)
    if successes <= 0 and errors > 0:
        return "failed"
    if errors > 0:
        return "partial"
    return "completed"


def _capture_page_downloader_name(params: CapturePlan) -> str:
    if params.render_mode == "playwright":
        return "frisket.capture.url.render_playwright_url"
    return "frisket.capture.url.fetch_static_url"

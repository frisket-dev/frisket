"""Export action-family handlers and implementations."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, NamedTuple

from frisket.actions.google_sheets_types import GoogleSheetsExportRequest
from frisket.actions.core import _ProjectAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ColumnTablesExporter,
    ExportedCsvSheetFile,
    ExportedJsonlSheetFile,
    ExportedParquetSheetFile,
    ExportedWorkLog,
)
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    ActionSpec,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_inventory import (
    ExecutorContext,
    ExecutorDeps,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_reservations import (
    _direct_action_finalize_metadata,
    _finalize_direct_reserved_action_receipt,
    _receipt_for_idempotency,
    _reserve_running_action_receipt,
    _reserved_receipt_result_from_existing,
)
from frisket.engine.executor.action_support import (
    _failed_result,
)
from frisket.engine.runner.confirmation_context import (
    ActionScope,
    mint_confirmation_hash,
    scope_confirmation,
)
from frisket.engine.runner.confirmation_echo import refuse_unless_exact_echo
from frisket.server.exports.dataset_formats import (
    DatasetFormatError,
    render_jsonl,
    render_parquet,
)
from frisket.server.exports.plan import (
    EXPORT_PLAN_SCHEMA_VERSION,
    build_sheet_export_plan,
    iter_export_row_batches,
    plan_receipt_payload,
    rowset_hash,
)
from frisket.server.exports.rowset import MAX_EXPLICIT_EXPORT_ROW_IDS, ExportError
from frisket.server.exports.sheet_csv import (
    MAX_FILTERED_SHEET_CSV_EXPORT_ROWS,
    SheetExportLimits,
    iter_export_csv_bytes,
)
from frisket.server.exports.work_log import build_work_log_payload, work_log_markdown
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.ops.integrations.google_sheets import (
    normalize_google_sheets_destination,
    normalize_google_sheets_tabs,
)
from frisket.redaction import redact_text, safe_error


logger = logging.getLogger("frisket.executor")

_EXPORT_GOOGLE_SHEETS_EFFECT_FAMILY = "export_google_sheets_egress"
_EXPORT_GOOGLE_SHEETS_EFFECT_KIND = "export.google_sheets"
_EXPORT_GOOGLE_SHEETS_EFFECT_IDENTITY_SCHEMA = (
    "frisket.export_google_sheets_effect_identity.v2"
)
_EXPORT_GOOGLE_SHEETS_EFFECT_PAYLOAD_SCHEMA = (
    "frisket.export_google_sheets_effect_checkpoint.v1"
)
_EXPORT_GOOGLE_SHEETS_EFFECT_UNIT_KEY = "egress"

_GOOGLE_SHEETS_CONFIRMATION_CLAIMS: tuple[dict[str, str], ...] = (
    {
        "field": "egress_class",
        "display": "Data is sent to Google Sheets, a third-party service.",
    },
    {
        "field": "irreversible_external",
        "display": (
            "Google Sheets may create or replace spreadsheet tabs; Frisket "
            "cannot undo this external write."
        ),
    },
)


@dataclass(frozen=True)
class _GoogleSheetsAdmission:
    client: Any
    connection: dict[str, Any]


def _google_sheets_confirmation_hash(
    action: _TypedProjectEnvelope,
    intent: GoogleSheetsExportRequest,
    *,
    params_hash: str,
) -> str:
    """Bind the public challenge to canonical identity and actual producer intent."""
    return mint_confirmation_hash(
        scope_confirmation(
            family_kind=action.kind,
            scope=ActionScope(action_hash=params_hash),
            bindings={
                "export": intent.model_dump(mode="json"),
                "claims": [dict(claim) for claim in _GOOGLE_SHEETS_CONFIRMATION_CLAIMS],
            },
        )
    )


def google_sheets_admission(
    action: _TypedProjectEnvelope,
    params: GoogleSheetsExportRequest,
    deps: ExecutorDeps,
    *,
    params_hash: str,
    confirmation: str | None,
) -> _GoogleSheetsAdmission | ActionError:
    """Check live account and exact no-money consent without durable writes."""
    client = deps.google_sheets_client
    if client is None:
        return ActionError(
            code="google_sheets_client_unavailable",
            message="No Google Sheets export client is configured",
            action_kind=action.kind,
        )
    resolver = deps.connected_account_resolver
    connection = resolver("google", params.connection_id) if resolver else None
    if (
        not isinstance(connection, Mapping)
        or connection.get("id") != params.connection_id
        or connection.get("provider") != "google"
    ):
        return ActionError(
            code="connected_account_not_found",
            message="No Google connected account matches the export connection",
            action_kind=action.kind,
            field="params.connection_id",
            details={"connection_id": params.connection_id, "provider": "google"},
        )
    claims = [dict(claim) for claim in _GOOGLE_SHEETS_CONFIRMATION_CLAIMS]
    refusal = refuse_unless_exact_echo(
        confirmed=confirmation is not None,
        echoed_hash=confirmation,
        expected_hash=_google_sheets_confirmation_hash(
            action, params, params_hash=params_hash
        ),
        refuse=lambda: ActionError(
            code="irreversible_external_requires_confirmation",
            message="This export needs confirmation: "
            + " ".join(claim["display"] for claim in claims),
            action_kind=action.kind,
            field="confirmation",
            needs_confirmation=True,
            details={
                "reason": "irreversible_external",
                "claims": claims,
                "promise_set_hash": _google_sheets_confirmation_hash(
                    action, params, params_hash=params_hash
                ),
            },
        ),
    )
    return refusal or _GoogleSheetsAdmission(client=client, connection=dict(connection))


@dataclass(frozen=True)
class _GoogleSheetsEffectUnit:
    checkpoint_id: str
    group_key: str
    identity: str
    receipt_id: str
    params_hash: str
    legacy_group_key: str
    legacy_identity: str

    def reservation_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _EXPORT_GOOGLE_SHEETS_EFFECT_PAYLOAD_SCHEMA,
            "receipt_id": self.receipt_id,
            "params_hash": self.params_hash,
        }


@dataclass(frozen=True)
class _GoogleSheetsEffectDispatch:
    disposition: Literal["proceed", "replay_returned", "refuse_ambiguous"]
    checkpoint_id: str
    provider_result: dict[str, Any] | None = None
    error: ActionError | None = None


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _legacy_export_google_sheets_effect_identity(
    params: GoogleSheetsExportRequest,
    tabs: list[dict[str, Any]],
) -> str:
    """The pre-ACTION-06A count-only identity, for read compatibility only."""

    payload = {
        "schema_version": "frisket.export_google_sheets_effect_identity.v1",
        "connection_id": params.connection_id,
        "destination": params.destination.model_dump(mode="json"),
        "write_policy": params.write_policy,
        "tabs": [
            {
                "title": tab["title"],
                "sheet_id": tab["sheet_id"],
                "row_count": tab["row_count"],
                "column_count": len(tab["columns"]),
            }
            for tab in tabs
        ],
    }
    return _canonical_sha256(payload).removeprefix("sha256:")


def _export_google_sheets_effect_unit(
    params: GoogleSheetsExportRequest,
    tabs: list[dict[str, Any]],
    *,
    idempotency_key: str,
    receipt_id: str,
    params_hash: str,
    connection: Mapping[str, Any],
) -> _GoogleSheetsEffectUnit:
    """Canonical identity for the exact provider write, independent of retry key."""

    normalized_tabs = normalize_google_sheets_tabs(tabs)
    identity_payload = {
        "schema_version": _EXPORT_GOOGLE_SHEETS_EFFECT_IDENTITY_SCHEMA,
        "account": {
            "connection_id": params.connection_id,
            "external_subject": connection.get("external_subject"),
        },
        "provider_request": {
            "source": {
                "kind": params.source.kind,
                "sheet_id": params.source.sheet_id,
            },
            "destination": normalize_google_sheets_destination(
                params.destination.model_dump(mode="json")
            ),
            "write_policy": params.write_policy,
            # Exact normalized columns + live typed values sent to Google.
            "tabs": normalized_tabs,
        },
        "source_facts": [
            {
                "sheet_id": tab["sheet_id"],
                "row_ids": list(tab["row_ids"]),
                "query": tab.get("query"),
                "query_hash": tab.get("query_hash"),
            }
            for tab in tabs
        ],
    }
    identity = _canonical_sha256(identity_payload)
    digest = identity.removeprefix("sha256:")
    return _GoogleSheetsEffectUnit(
        checkpoint_id=f"export_google_sheets_egress_{digest}",
        group_key=identity,
        identity=identity,
        receipt_id=receipt_id,
        params_hash=params_hash,
        legacy_group_key=idempotency_key,
        legacy_identity=_legacy_export_google_sheets_effect_identity(params, tabs),
    )


def _google_sheets_secret_values(connection: Mapping[str, Any]) -> tuple[str, ...]:
    sensitive: list[str] = []
    for raw_key, raw_value in connection.items():
        key = str(raw_key).lower().replace("-", "_")
        if not (
            key in {"password", "secret", "token"}
            or key.endswith(("password", "secret", "token", "api_key", "apikey"))
        ):
            continue
        if isinstance(raw_value, str) and raw_value:
            sensitive.append(raw_value)
    return tuple(sensitive)


def _bounded_google_sheets_provider_result(
    provider_result: Any,
    *,
    params: GoogleSheetsExportRequest,
    tabs: list[dict[str, Any]],
    secret_values: tuple[str, ...],
) -> dict[str, Any] | None:
    if not isinstance(provider_result, Mapping):
        return None
    raw_spreadsheet_id = provider_result.get("spreadsheet_id")
    if not isinstance(raw_spreadsheet_id, str) or not raw_spreadsheet_id.strip():
        return None
    raw_spreadsheet_id = raw_spreadsheet_id.strip()
    effective_destination = normalize_google_sheets_destination(
        params.destination.model_dump(mode="json")
    )
    if (
        params.destination.mode == "update_existing"
        and raw_spreadsheet_id != effective_destination["spreadsheet_id"]
    ):
        return None
    spreadsheet_id = redact_text(
        raw_spreadsheet_id,
        secret_values=secret_values,
        max_chars=512,
    )
    if spreadsheet_id != raw_spreadsheet_id:
        return None
    spreadsheet_url_raw = provider_result.get("spreadsheet_url")
    spreadsheet_url = (
        redact_text(
            spreadsheet_url_raw,
            secret_values=secret_values,
            max_chars=2_048,
        )
        if isinstance(spreadsheet_url_raw, str)
        else None
    )
    normalized_tabs = normalize_google_sheets_tabs(tabs)
    return {
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": spreadsheet_url,
        # Provider-owned extras never enter durable recovery. The request we
        # actually sent is the trustworthy source for this bounded summary.
        "updated_tabs": [
            {
                "title": tab["title"],
                "sheet_id": tab.get("sheet_id"),
                "row_count": len(tab["rows"]),
                "column_count": len(tab["columns"]),
            }
            for tab in normalized_tabs
        ],
    }


def _google_sheets_reconciliation_error(
    action: ActionSpec,
    *,
    checkpoint_id: str,
    checkpoint_code: str,
    provider_error: str | None = None,
) -> ActionError:
    details: dict[str, Any] = {
        "checkpoint_code": checkpoint_code,
        "checkpoint_id": checkpoint_id,
        "possible_external_effect": True,
        "reconciliation_required": True,
        "retryable": False,
    }
    if provider_error:
        details["provider_error"] = provider_error
    return ActionError(
        code="external_effect_reconciliation_required",
        message=(
            "Google Sheets may have accepted this export; another provider call "
            "is refused until the effect is reconciled"
        ),
        action_kind=action.kind,
        details=details,
    )


def _google_sheets_returned_result(
    checkpoint: Mapping[str, Any],
    *,
    params: GoogleSheetsExportRequest,
    tabs: list[dict[str, Any]],
    secret_values: tuple[str, ...],
    legacy: bool,
) -> dict[str, Any] | None:
    payload = checkpoint.get("payload")
    if not isinstance(payload, Mapping):
        return None
    if not legacy:
        if (
            payload.get("schema_version") != _EXPORT_GOOGLE_SHEETS_EFFECT_PAYLOAD_SCHEMA
            or not isinstance(payload.get("receipt_id"), str)
            or not isinstance(payload.get("params_hash"), str)
        ):
            return None
    provider_result = payload.get("provider_result")
    if not isinstance(provider_result, Mapping):
        return None
    bounded = _bounded_google_sheets_provider_result(
        provider_result,
        params=params,
        tabs=tabs,
        secret_values=secret_values,
    )
    if bounded is None:
        return None
    if not legacy:
        # Reject corrupt/tampered recovery material rather than silently
        # normalizing it into permission to complete.
        expected_keys = {"spreadsheet_id", "spreadsheet_url", "updated_tabs"}
        if set(provider_result) != expected_keys or dict(provider_result) != bounded:
            return None
    return bounded


def _google_sheets_effect_dispatch(
    store: EffectCheckpointStore,
    unit: _GoogleSheetsEffectUnit,
    *,
    action: ActionSpec,
    params: GoogleSheetsExportRequest,
    tabs: list[dict[str, Any]],
    secret_values: tuple[str, ...],
) -> _GoogleSheetsEffectDispatch:
    candidates = store.find_recovery_candidates(
        family=_EXPORT_GOOGLE_SHEETS_EFFECT_FAMILY,
        action_kind=_EXPORT_GOOGLE_SHEETS_EFFECT_KIND,
        unit_key=_EXPORT_GOOGLE_SHEETS_EFFECT_UNIT_KEY,
        canonical_group_key=unit.group_key,
        legacy_group_key=unit.legacy_group_key,
        legacy_identity=unit.legacy_identity,
        payload_schema_version=_EXPORT_GOOGLE_SHEETS_EFFECT_PAYLOAD_SCHEMA,
        receipt_id=unit.receipt_id,
    )

    # A queue retry owns its original effect even if the live sheet drifted
    # after the first provider call. That drift must refuse, never mint a new
    # effect identity for the same durable receipt.
    receipt_rows = [
        row
        for row in candidates
        if isinstance(row.get("payload"), Mapping)
        and row["payload"].get("schema_version")
        == _EXPORT_GOOGLE_SHEETS_EFFECT_PAYLOAD_SCHEMA
        and row["payload"].get("receipt_id") == unit.receipt_id
    ]
    checkpoint: dict[str, Any] | None
    if receipt_rows:
        checkpoint = receipt_rows[0]
        expected_identity = unit.identity
        legacy = False
    else:
        checkpoint = next(
            (row for row in candidates if row.get("group_key") == unit.group_key),
            None,
        )
        expected_identity = unit.identity
        legacy = False

    if checkpoint is None and unit.legacy_group_key:
        checkpoint = next(
            (
                row
                for row in candidates
                if row.get("group_key") == unit.legacy_group_key
            ),
            None,
        )
        expected_identity = unit.legacy_identity
        legacy = checkpoint is not None

    if checkpoint is None:
        # A legacy cross-key row has only count-level identity. It is too weak
        # to authorize replay, but strong enough to prove a possible duplicate
        # and therefore fail closed until its existing operator lifecycle acts.
        legacy_match = next(
            (
                row
                for row in candidates
                if row.get("identity") == unit.legacy_identity
                and row.get("group_key") != unit.legacy_group_key
            ),
            None,
        )
        if legacy_match is not None:
            error = _google_sheets_reconciliation_error(
                action,
                checkpoint_id=str(legacy_match["id"]),
                checkpoint_code="legacy_cross_key_identity_unproven",
            )
            return _GoogleSheetsEffectDispatch(
                "refuse_ambiguous",
                str(legacy_match["id"]),
                error=error,
            )
        return _GoogleSheetsEffectDispatch("proceed", unit.checkpoint_id)

    checkpoint_id = str(checkpoint["id"])
    if (
        checkpoint.get("action_kind") != _EXPORT_GOOGLE_SHEETS_EFFECT_KIND
        or checkpoint.get("identity") != expected_identity
        or len(receipt_rows) > 1
    ):
        code = (
            "duplicate_receipt_effect" if len(receipt_rows) > 1 else "identity_mismatch"
        )
        error = _google_sheets_reconciliation_error(
            action,
            checkpoint_id=checkpoint_id,
            checkpoint_code=code,
        )
        return _GoogleSheetsEffectDispatch(
            "refuse_ambiguous", checkpoint_id, error=error
        )

    state = checkpoint.get("state")
    if state == "returned":
        provider_result = _google_sheets_returned_result(
            checkpoint,
            params=params,
            tabs=tabs,
            secret_values=secret_values,
            legacy=legacy,
        )
        if provider_result is not None:
            return _GoogleSheetsEffectDispatch(
                "replay_returned",
                checkpoint_id,
                provider_result=provider_result,
            )
        code = "returned_payload_invalid"
    elif state == "reserved":
        code = "ambiguous_reserved"
    else:
        code = "invalid_checkpoint_state"
    error = _google_sheets_reconciliation_error(
        action,
        checkpoint_id=checkpoint_id,
        checkpoint_code=code,
    )
    return _GoogleSheetsEffectDispatch("refuse_ambiguous", checkpoint_id, error=error)


def _export_project_write_failed_error(action: ActionSpec) -> ActionError:
    return ActionError(
        code="project_write_failed",
        message="project write failed",
        action_kind=action.kind,
    )


def _emit_export_notification(project: Project, result: ActionResult) -> None:
    if result.status not in {"completed", "failed"}:
        return
    try:
        from frisket.server.notifications.producers import export_notification_candidate
        from frisket.server.notifications.service import emit_notification_candidate

        artifact_ref = _export_notification_artifact_ref(result)
        errors = [
            str(error.message)
            for error in (result.errors or [])
            if str(error.message).strip()
        ]
        candidate = export_notification_candidate(
            project_id=result.project_id,
            export_kind=_export_notification_kind(result, artifact_ref),
            status=result.status,
            receipt_id=result.receipt_id,
            artifact_ref=artifact_ref,
            error="; ".join(errors) or None,
        )
        if candidate is not None:
            emit_notification_candidate(project, candidate)
    except Exception:  # noqa: BLE001 - export result must remain authoritative
        return


def _export_notification_artifact_ref(result: ActionResult) -> dict[str, Any] | None:
    for output in result.outputs or []:
        if not isinstance(output.ref, dict):
            continue
        ref = dict(output.ref)
        if ref.get("kind") in {
            "export_artifact",
            "export_package",
            "google_sheet",
        } or ref.get("export_kind"):
            return ref
    return None


def _export_notification_kind(
    result: ActionResult,
    artifact_ref: dict[str, Any] | None,
) -> str:
    if artifact_ref is not None:
        export_kind = artifact_ref.get("export_kind")
        if export_kind:
            return str(export_kind)
    return result.action.kind


@dataclass
class _ResolvedExportArtifact:
    artifact_ref: dict[str, Any]
    payload: dict[str, Any]
    output_name: str
    receipt_kind: str
    tmp_path: Path
    destination: Path
    delivery: Any | None = None


def _export_error(exc: ExportError, action_kind: str) -> ActionError:
    return ActionError(
        code=exc.code,
        message=exc.message,
        action_kind=action_kind,
        field=exc.field,
        details=exc.details or None,
    )


def _dataset_error(exc: DatasetFormatError, action_kind: str) -> ActionError:
    return ActionError(
        code=exc.code,
        message=exc.message,
        action_kind=action_kind,
        details=exc.details or None,
    )


def _stage_bytes(
    destination: Path, content: bytes, *, action_kind: str
) -> Path | ActionError:
    tmp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_bytes(content)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        return ActionError(
            code="invalid_export_destination",
            message=f"{action_kind} could not write the destination file",
            action_kind=action_kind,
            field="params.destination.path",
            details={"error": str(exc), "path": str(destination)},
        )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def _resolve_work_log(
    project: Any,
    *,
    path: str,
    include_receipts: bool,
    project_id: str,
) -> _ResolvedExportArtifact | ActionError:
    destination = Path(path)
    destination_error = _precheck_export_destination(
        destination, action_kind="export.work_log"
    )
    if destination_error is not None:
        return destination_error
    destination = destination.parent.resolve() / destination.name

    payload = build_work_log_payload(
        project,
        project_id,
        include_receipts=include_receipts,
    )
    content = work_log_markdown(payload).encode("utf-8")
    staged = _stage_bytes(destination, content, action_kind="export.work_log")
    if isinstance(staged, ActionError):
        return staged
    return _ResolvedExportArtifact(
        artifact_ref={
            "kind": "export_artifact",
            "format": "markdown",
            "content_type": "text/markdown; charset=utf-8",
            "path": str(destination),
            "byte_count": len(content),
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        },
        payload=payload,
        output_name="work_log",
        receipt_kind="work_log",
        tmp_path=staged,
        destination=destination,
    )


def _resolve_sheet_csv(
    project: Any,
    *,
    sheet_id: int,
    path: str,
    query: dict[str, Any] | None,
    formula_policy: str,
    max_rows: int | None,
    check_cancelled: Callable[[], None] | None = None,
) -> _ResolvedExportArtifact | ActionError:
    with project.read_snapshot() as snapshot:
        try:
            plan = build_sheet_export_plan(
                snapshot,
                sheet_id,
                query=query,
                media_policy="references",
                value_policy="display_scalars",
                max_rows=max_rows,
                query_field="params.query",
                streaming_rowset=True,
            )
        except ExportError as exc:
            return _export_error(exc, "export.sheet_csv")

        destination = Path(path)
        destination_error = _precheck_export_destination(
            destination, action_kind="export.sheet_csv"
        )
        if destination_error is not None:
            return destination_error

        # Promotion replaces the leaf itself, but follows parent-directory aliases.
        # Pin that target before staging so later calls compare completed locations,
        # not whatever a historical symlink happens to resolve to now.
        destination = destination.parent.resolve() / destination.name
        tmp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        rowset_digest = hashlib.sha256()
        row_ids: list[int] = []
        row_count = 0
        byte_count = 0
        content_hash = hashlib.sha256()

        def record_row_ids(batch: list[int]) -> None:
            nonlocal row_count
            if check_cancelled is not None:
                check_cancelled()
            for row_id in batch:
                row_count += 1
                rowset_digest.update(f"{row_id},".encode())
                if len(row_ids) <= MAX_EXPLICIT_EXPORT_ROW_IDS:
                    row_ids.append(row_id)

        try:
            with tmp_path.open("wb") as handle:
                for chunk in iter_export_csv_bytes(
                    snapshot,
                    plan,
                    formula_policy=formula_policy,
                    bom=False,
                    on_batch=record_row_ids,
                ):
                    handle.write(chunk)
                    byte_count += len(chunk)
                    content_hash.update(chunk)
            if len(row_ids) > MAX_EXPLICIT_EXPORT_ROW_IDS:
                row_ids = []
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            return ActionError(
                code="invalid_export_destination",
                message="export.sheet_csv could not write the destination file",
                action_kind="export.sheet_csv",
                field="params.destination.path",
                details={"error": str(exc), "path": str(destination)},
            )
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        payload = plan_receipt_payload(plan, row_ids)
        payload["row_count"] = row_count
        payload["rowset_hash"] = "sha256:" + rowset_digest.hexdigest()
        if not row_ids and row_count:
            payload.pop("row_ids", None)
        artifact_ref = {
            "kind": "export_artifact",
            "export_kind": "sheet_csv",
            "format": "csv",
            "content_type": "text/csv; charset=utf-8",
            "path": str(destination),
            "byte_count": byte_count,
            "sha256": "sha256:" + content_hash.hexdigest(),
            "sheet_id": payload["sheet_id"],
            "sheet_name": payload["sheet_name"],
            "row_count": payload["row_count"],
            "column_count": payload["column_count"],
        }
        if plan.query is not None:
            artifact_ref.update(
                {
                    "query": payload["query"],
                    "query_hash": payload["query_hash"],
                    "query_total": payload["query_total"],
                    "query_evaluator": payload["query_evaluator"],
                    **({"row_ids": payload["row_ids"]} if "row_ids" in payload else {}),
                }
            )
        return _ResolvedExportArtifact(
            artifact_ref=artifact_ref,
            payload=payload,
            output_name="sheet_csv",
            receipt_kind="sheet",
            tmp_path=tmp_path,
            destination=destination,
        )


def _resolve_sheet_jsonl(
    project: Any,
    *,
    sheet_id: int,
    path: str,
    query: dict[str, Any] | None,
) -> _ResolvedExportArtifact | ActionError:
    try:
        plan = build_sheet_export_plan(
            project,
            sheet_id,
            query=query,
            media_policy="references",
            value_policy="typed",
            max_rows=MAX_FILTERED_SHEET_CSV_EXPORT_ROWS,
            query_field="params.query",
        )
    except ExportError as exc:
        return _export_error(exc, "export.sheet_jsonl")
    destination = Path(path)
    destination_error = _precheck_export_destination(
        destination, action_kind="export.sheet_jsonl"
    )
    if destination_error is not None:
        return destination_error
    destination = destination.parent.resolve() / destination.name
    try:
        row_ids, content = render_jsonl(project, plan)
    except DatasetFormatError as exc:
        return _dataset_error(exc, "export.sheet_jsonl")
    payload = plan_receipt_payload(plan, row_ids)
    artifact_ref = {
        "kind": "export_artifact",
        "export_kind": "sheet_jsonl",
        "format": "jsonl",
        "content_type": "application/x-ndjson; charset=utf-8",
        "path": str(destination),
        "byte_count": len(content),
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "sheet_id": payload["sheet_id"],
        "sheet_name": payload["sheet_name"],
        "row_count": payload["row_count"],
        "column_count": payload["column_count"],
    }
    if plan.query is not None:
        artifact_ref.update(
            {
                "query": payload["query"],
                "query_hash": payload["query_hash"],
                "query_total": payload["query_total"],
                "query_evaluator": payload["query_evaluator"],
                "row_ids": payload["row_ids"],
            }
        )
    staged = _stage_bytes(destination, content, action_kind="export.sheet_jsonl")
    if isinstance(staged, ActionError):
        return staged
    return _ResolvedExportArtifact(
        artifact_ref=artifact_ref,
        payload=payload,
        output_name="sheet_jsonl",
        receipt_kind="sheet",
        tmp_path=staged,
        destination=destination,
    )


def _resolve_sheet_parquet(
    project: Any,
    *,
    sheet_id: int,
    path: str,
    query: dict[str, Any] | None,
) -> _ResolvedExportArtifact | ActionError:
    try:
        plan = build_sheet_export_plan(
            project,
            sheet_id,
            query=query,
            media_policy="references",
            value_policy="typed",
            max_rows=MAX_FILTERED_SHEET_CSV_EXPORT_ROWS,
            query_field="params.query",
        )
    except ExportError as exc:
        return _export_error(exc, "export.sheet_parquet")
    destination = Path(path)
    destination_error = _precheck_export_destination(
        destination, action_kind="export.sheet_parquet"
    )
    if destination_error is not None:
        return destination_error
    destination = destination.parent.resolve() / destination.name
    try:
        row_ids, content = render_parquet(project, plan)
    except DatasetFormatError as exc:
        return _dataset_error(exc, "export.sheet_parquet")
    payload = plan_receipt_payload(plan, row_ids)
    artifact_ref = {
        "kind": "export_artifact",
        "export_kind": "sheet_parquet",
        "format": "parquet",
        "content_type": "application/vnd.apache.parquet",
        "path": str(destination),
        "byte_count": len(content),
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "sheet_id": payload["sheet_id"],
        "sheet_name": payload["sheet_name"],
        "row_count": payload["row_count"],
        "column_count": payload["column_count"],
    }
    if plan.query is not None:
        artifact_ref.update(
            {
                "query": payload["query"],
                "query_hash": payload["query_hash"],
                "query_total": payload["query_total"],
                "query_evaluator": payload["query_evaluator"],
                "row_ids": payload["row_ids"],
            }
        )
    staged = _stage_bytes(destination, content, action_kind="export.sheet_parquet")
    if isinstance(staged, ActionError):
        return staged
    return _ResolvedExportArtifact(
        artifact_ref=artifact_ref,
        payload=payload,
        output_name="sheet_parquet",
        receipt_kind="sheet",
        tmp_path=staged,
        destination=destination,
    )


def _work_log_result(resolved: _ResolvedExportArtifact) -> ExportedWorkLog:
    ref = resolved.artifact_ref
    return ExportedWorkLog(
        format="markdown",
        path=str(ref["path"]),
        byte_count=int(ref["byte_count"]),
        sha256=str(ref["sha256"]),
    )


def _sheet_csv_result(resolved: _ResolvedExportArtifact) -> ExportedCsvSheetFile:
    ref = resolved.artifact_ref
    return ExportedCsvSheetFile(
        sheet_id=int(ref["sheet_id"]),
        format="csv",
        path=str(ref["path"]),
        byte_count=int(ref["byte_count"]),
        sha256=str(ref["sha256"]),
        row_count=int(ref["row_count"]),
        row_ids=tuple(ref.get("row_ids") or ()),
        query_hash=ref.get("query_hash"),
    )


def _sheet_jsonl_result(resolved: _ResolvedExportArtifact) -> ExportedJsonlSheetFile:
    ref = resolved.artifact_ref
    return ExportedJsonlSheetFile(
        sheet_id=int(ref["sheet_id"]),
        format="jsonl",
        path=str(ref["path"]),
        byte_count=int(ref["byte_count"]),
        sha256=str(ref["sha256"]),
        row_count=int(ref["row_count"]),
        row_ids=tuple(ref.get("row_ids") or ()),
        query_hash=ref.get("query_hash"),
    )


def _sheet_parquet_result(
    resolved: _ResolvedExportArtifact,
) -> ExportedParquetSheetFile:
    ref = resolved.artifact_ref
    return ExportedParquetSheetFile(
        sheet_id=int(ref["sheet_id"]),
        format="parquet",
        path=str(ref["path"]),
        byte_count=int(ref["byte_count"]),
        sha256=str(ref["sha256"]),
        row_count=int(ref["row_count"]),
        row_ids=tuple(ref.get("row_ids") or ()),
        query_hash=ref.get("query_hash"),
    )


def _record_callable_export(invocation: Any, resolved: _ResolvedExportArtifact) -> None:
    if resolved.receipt_kind == "work_log":
        observed = _export_work_log_receipt(
            action=invocation.action,
            action_id=invocation.receipt.action_id,
            project_id=invocation.receipt.project_id,
            receipt_id=invocation.receipt.receipt_id,
            params_hash=invocation.receipt.params_hash,
            artifact_ref=resolved.artifact_ref,
            payload=resolved.payload,
        )
    else:
        observed = _export_sheet_csv_receipt(
            action=invocation.action,
            action_id=invocation.receipt.action_id,
            project_id=invocation.receipt.project_id,
            receipt_id=invocation.receipt.receipt_id,
            params_hash=invocation.receipt.params_hash,
            artifact_ref=resolved.artifact_ref,
            payload=resolved.payload,
            output_name=resolved.output_name,
        )

    def deliver() -> None:
        invocation.context.check_cancelled()
        resolved.delivery = _deliver_export_artifact(
            resolved.tmp_path, resolved.destination
        )

    invocation.record(
        inputs=observed.inputs,
        outputs=observed.outputs,
        evidence=[
            *observed.evidence,
            ReceiptEvidence(ref=dict(resolved.artifact_ref), retention="pinned"),
        ],
        exports=observed.exports,
        before_commit=deliver,
    )


def _callable_export_result(invocation: Any, resolved: _ResolvedExportArtifact) -> None:
    try:
        invocation.context.check_cancelled()
        _record_callable_export(invocation, resolved)
    except BaseException:
        _cleanup(resolved)
        raise
    _finalize(resolved)


class WorkLogExportCapability:
    def __init__(self, invocation: Any):
        self._invocation = invocation

    def write_work_log(self, *, path: str, include_receipts: bool) -> ExportedWorkLog:
        from frisket.actions.exports import LocalFileDestination
        from frisket.engine.executor.callable_action import CallablePrimitiveError

        invocation = self._invocation
        invocation.context.check_cancelled()
        try:
            destination = LocalFileDestination(kind="local_file", path=path)
            if type(include_receipts) is not bool:
                raise ValueError("include_receipts must be a bool")
        except (TypeError, ValueError) as exc:
            raise CallablePrimitiveError(
                ActionError(
                    code="invalid_params",
                    message="The work-log export arguments are invalid.",
                    action_kind=invocation.action.kind,
                )
            ) from exc
        resolved = _resolve_work_log(
            invocation.project,
            path=destination.path,
            include_receipts=include_receipts,
            project_id=invocation.receipt.project_id,
        )
        if isinstance(resolved, ActionError):
            raise CallablePrimitiveError(resolved)
        _callable_export_result(invocation, resolved)
        return _work_log_result(resolved)


class SheetCsvExportCapability:
    def __init__(self, invocation: Any):
        self._invocation = invocation

    def write_sheet_csv(
        self,
        *,
        sheet_id: int,
        path: str,
        query: Mapping[str, Any] | None,
        formula_policy: Literal["escape", "raw"],
    ) -> ExportedCsvSheetFile:
        from frisket.actions.exports import LocalFileDestination
        from frisket.engine.executor.callable_action import CallablePrimitiveError

        invocation = self._invocation
        invocation.context.check_cancelled()
        # Validate the actual operation arguments, never the author's Params.
        if (
            type(sheet_id) is not int
            or sheet_id < 1
            or formula_policy not in ("escape", "raw")
        ):
            raise CallablePrimitiveError(
                ActionError(
                    code="invalid_params",
                    message="The CSV export arguments are invalid.",
                    action_kind=invocation.action.kind,
                )
            )
        try:
            destination = LocalFileDestination(kind="local_file", path=path)
            if query is not None and not isinstance(query, Mapping):
                raise ValueError("query must be a mapping")
        except (TypeError, ValueError) as exc:
            raise CallablePrimitiveError(
                ActionError(
                    code="invalid_params",
                    message="The CSV export arguments are invalid.",
                    action_kind=invocation.action.kind,
                )
            ) from exc
        limits = invocation.deps.sheet_export_limits
        resolved = _resolve_sheet_csv(
            invocation.project,
            sheet_id=sheet_id,
            path=destination.path,
            query=dict(query) if query is not None else None,
            formula_policy=formula_policy,
            max_rows=limits.max_rows if isinstance(limits, SheetExportLimits) else None,
            check_cancelled=invocation.context.check_cancelled,
        )
        if isinstance(resolved, ActionError):
            raise CallablePrimitiveError(resolved)
        _callable_export_result(invocation, resolved)
        return _sheet_csv_result(resolved)


def _validate_dataset_export_args(
    invocation: Any,
    *,
    sheet_id: int,
    path: str,
    query: Mapping[str, Any] | None,
    format_name: str,
) -> tuple[str, dict[str, Any] | None]:
    from frisket.actions.exports import LocalFileDestination
    from frisket.engine.executor.callable_action import CallablePrimitiveError

    try:
        if type(sheet_id) is not int or sheet_id < 1:
            raise ValueError("sheet_id must be positive")
        destination = LocalFileDestination(kind="local_file", path=path)
        if query is not None and not isinstance(query, Mapping):
            raise ValueError("query must be a mapping")
    except (TypeError, ValueError) as exc:
        raise CallablePrimitiveError(
            ActionError(
                code="invalid_params",
                message=f"The {format_name} export arguments are invalid.",
                action_kind=invocation.action.kind,
            )
        ) from exc
    return destination.path, dict(query) if query is not None else None


class SheetJsonlExportCapability:
    def __init__(self, invocation: Any):
        self._invocation = invocation

    def write_sheet_jsonl(
        self, *, sheet_id: int, path: str, query: Mapping[str, Any] | None
    ) -> ExportedJsonlSheetFile:
        from frisket.engine.executor.callable_action import CallablePrimitiveError

        invocation = self._invocation
        invocation.context.check_cancelled()
        path, query_value = _validate_dataset_export_args(
            invocation,
            sheet_id=sheet_id,
            path=path,
            query=query,
            format_name="JSONL",
        )
        resolved = _resolve_sheet_jsonl(
            invocation.project,
            sheet_id=sheet_id,
            path=path,
            query=query_value,
        )
        if isinstance(resolved, ActionError):
            raise CallablePrimitiveError(resolved)
        _callable_export_result(invocation, resolved)
        return _sheet_jsonl_result(resolved)


class SheetParquetExportCapability:
    def __init__(self, invocation: Any):
        self._invocation = invocation

    def write_sheet_parquet(
        self, *, sheet_id: int, path: str, query: Mapping[str, Any] | None
    ) -> ExportedParquetSheetFile:
        from frisket.engine.executor.callable_action import CallablePrimitiveError

        invocation = self._invocation
        invocation.context.check_cancelled()
        path, query_value = _validate_dataset_export_args(
            invocation,
            sheet_id=sheet_id,
            path=path,
            query=query,
            format_name="Parquet",
        )
        resolved = _resolve_sheet_parquet(
            invocation.project,
            sheet_id=sheet_id,
            path=path,
            query=query_value,
        )
        if isinstance(resolved, ActionError):
            raise CallablePrimitiveError(resolved)
        _callable_export_result(invocation, resolved)
        return _sheet_parquet_result(resolved)


def supports_typed_export_action(terminal: _ProjectAction[Any, Any]) -> bool:
    return terminal.capabilities == (ColumnTablesExporter,)


def _cleanup(resolved: _ResolvedExportArtifact) -> None:
    _rollback_export_delivery(
        delivery=resolved.delivery,
        tmp_path=resolved.tmp_path,
        destination=resolved.destination,
    )


def _finalize(resolved: _ResolvedExportArtifact) -> None:
    if resolved.delivery is not None:
        _finalize_export_delivery(resolved.delivery)


def run_typed_export_action(
    project: Any,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    deps: ExecutorDeps | None = None,
) -> ActionResult:
    terminal = bound.action.definition.run
    if not isinstance(terminal, _ProjectAction) or not supports_typed_export_action(
        terminal
    ):
        raise TypeError("typed export executor requires a local export action")
    # The ZIP adapter supports local files and project blobs and owns its
    # destination-specific replay.
    from frisket.engine.executor.action_families.exports_column_tables import (
        _run_typed_export_column_tables,
    )

    return _run_typed_export_column_tables(project, project_id, bound, deps=deps)


def _run_export_google_sheets(
    project: Project,
    action: ActionSpec,
    params: GoogleSheetsExportRequest,
    *,
    project_id: str,
    ctx: ExecutorContext,
    reserved_action_id: str | None = None,
    reserved_receipt_id: str | None = None,
    skip_replay: bool = False,
    params_hash: str,
    confirmation: str | None,
) -> ActionResult:
    if not skip_replay:
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is not None:
            return _reserved_receipt_result_from_existing(
                project,
                existing,
                params_hash=params_hash,
                project_id=project_id,
                action=action,
            )

    # Worker admission uses frozen actual intent, never builtin Params or queue claims.
    admission = google_sheets_admission(
        action,
        params,
        ctx.deps,
        params_hash=params_hash,
        confirmation=confirmation,
    )
    if isinstance(admission, ActionError):
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=admission,
        )
    client = admission.client
    connection_for_client = admission.connection
    connection_metadata = _google_sheets_connection_metadata(
        params.connection_id,
        connection_for_client,
    )

    tabs_or_error = _google_sheets_tabs_for_source(project, action, params)
    if isinstance(tabs_or_error, ActionError):
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=tabs_or_error,
        )
    tabs = tabs_or_error
    # Receipt helpers consume host-observed operation inputs. The independent
    # canonical request hash above still owns idempotency.
    action = _TypedProjectEnvelope(
        kind=action.kind,
        idempotency_key=action.idempotency_key,
        params=params.model_dump(mode="json"),
    )

    if reserved_action_id is not None and reserved_receipt_id is not None:
        action_id = reserved_action_id
        receipt_id = reserved_receipt_id
        finalize_requires_running = True
    else:
        reservation = _reserve_running_action_receipt(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
            reservation_kind="export_google_sheets_idempotency_reservation",
            result_from_existing_fn=_reserved_receipt_result_from_existing,
            params_model=GoogleSheetsExportRequest,
        )
        if isinstance(reservation, ActionResult):
            return reservation
        action_id = reservation["action_id"]
        receipt_id = reservation["receipt_id"]
        finalize_requires_running = True

    def terminal_effect_failure(error: ActionError) -> ActionResult:
        result = ActionResult(
            action=ActionIdentity(
                kind=action.kind,
                action_id=action_id,
            ),
            status="failed",
            project_id=project_id,
            receipt_id=receipt_id,
            errors=[error],
        )
        receipt = _export_google_sheets_failed_receipt(
            action=action,
            action_id=action_id,
            project_id=project_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            error=error,
            tabs=tabs,
            connection=connection_metadata,
        )
        finalized = _finalize_export_google_sheets_receipt(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
            receipt=receipt,
            require_running_status=finalize_requires_running,
        )
        return finalized or result

    secret_values = _google_sheets_secret_values(connection_for_client)
    effect_unit = _export_google_sheets_effect_unit(
        params,
        tabs,
        idempotency_key=action.idempotency_key or "",
        receipt_id=receipt_id,
        params_hash=params_hash,
        connection=connection_metadata,
    )
    checkpoint_store = EffectCheckpointStore(project.db)
    dispatch = _google_sheets_effect_dispatch(
        checkpoint_store,
        effect_unit,
        action=action,
        params=params,
        tabs=tabs,
        secret_values=secret_values,
    )
    if dispatch.disposition == "refuse_ambiguous":
        assert dispatch.error is not None
        return terminal_effect_failure(dispatch.error)

    if dispatch.disposition == "replay_returned":
        assert dispatch.provider_result is not None
        provider_result = dispatch.provider_result
    else:
        reserved = checkpoint_store.reserve(
            effect_unit.checkpoint_id,
            family=_EXPORT_GOOGLE_SHEETS_EFFECT_FAMILY,
            group_key=effect_unit.group_key,
            unit_key=_EXPORT_GOOGLE_SHEETS_EFFECT_UNIT_KEY,
            action_kind=_EXPORT_GOOGLE_SHEETS_EFFECT_KIND,
            identity=effect_unit.identity,
            payload=effect_unit.reservation_payload(),
            claimless_direct_effect=True,
        )
        if not reserved:
            # The unique canonical unit serialized a concurrent fresh-key
            # caller. Reclassify the winner's durable state through the same
            # decision instead of maintaining a reserve-race policy copy.
            dispatch = _google_sheets_effect_dispatch(
                checkpoint_store,
                effect_unit,
                action=action,
                params=params,
                tabs=tabs,
                secret_values=secret_values,
            )
            if dispatch.disposition == "replay_returned":
                assert dispatch.provider_result is not None
                provider_result = dispatch.provider_result
            else:
                error = dispatch.error or _google_sheets_reconciliation_error(
                    action,
                    checkpoint_id=effect_unit.checkpoint_id,
                    checkpoint_code="checkpoint_reservation_lost",
                )
                return terminal_effect_failure(error)
        else:
            provider_result = None

    if dispatch.disposition != "replay_returned" and provider_result is None:
        try:
            raw_provider_result = client.export_tabs(
                connection=connection_for_client,
                destination=params.destination.model_dump(mode="json"),
                tabs=tabs,
                write_policy=params.write_policy,
            )
        except Exception as exc:
            # Any exception after reserve may follow a provider-side write.
            # Keep `reserved`, sanitize once, and make the ambiguity explicit.
            safe = safe_error(
                "google_sheets_provider_error",
                exc,
                secret_values=secret_values,
                max_chars=1_000,
            )
            logger.debug(
                "google_sheets_export_failed",
                extra={
                    "event": "google_sheets_export_failed",
                    "action_kind": action.kind,
                    "error_code": safe.code,
                    "error": safe.detail,
                    "exception_type": safe.exception_type,
                },
                exc_info=False,
            )
            return terminal_effect_failure(
                _google_sheets_reconciliation_error(
                    action,
                    checkpoint_id=effect_unit.checkpoint_id,
                    checkpoint_code="ambiguous_reserved",
                    provider_error=safe.detail,
                )
            )

        provider_result = _bounded_google_sheets_provider_result(
            raw_provider_result,
            params=params,
            tabs=tabs,
            secret_values=secret_values,
        )
        if provider_result is None:
            return terminal_effect_failure(
                _google_sheets_reconciliation_error(
                    action,
                    checkpoint_id=effect_unit.checkpoint_id,
                    checkpoint_code="provider_result_invalid",
                )
            )
        try:
            checkpoint_store.complete(
                effect_unit.checkpoint_id,
                family=_EXPORT_GOOGLE_SHEETS_EFFECT_FAMILY,
                group_key=effect_unit.group_key,
                unit_key=_EXPORT_GOOGLE_SHEETS_EFFECT_UNIT_KEY,
                action_kind=_EXPORT_GOOGLE_SHEETS_EFFECT_KIND,
                identity=effect_unit.identity,
                payload={
                    **effect_unit.reservation_payload(),
                    "provider_result": provider_result,
                },
                claimless_direct_effect=True,
            )
        except Exception as exc:
            # The provider returned but its response did not become durable.
            # That is the same possible-effect ambiguity as a transport timeout.
            safe = safe_error(
                "google_sheets_checkpoint_error",
                exc,
                secret_values=secret_values,
                max_chars=1_000,
            )
            logger.debug(
                "google_sheets_checkpoint_complete_failed",
                extra={
                    "event": "google_sheets_checkpoint_complete_failed",
                    "action_kind": action.kind,
                    "error_code": safe.code,
                    "error": safe.detail,
                    "exception_type": safe.exception_type,
                },
                exc_info=False,
            )
            return terminal_effect_failure(
                _google_sheets_reconciliation_error(
                    action,
                    checkpoint_id=effect_unit.checkpoint_id,
                    checkpoint_code="checkpoint_complete_failed",
                    provider_error=safe.detail,
                )
            )

    if not isinstance(provider_result, dict):
        provider_result = {}
    spreadsheet_id = provider_result.get("spreadsheet_id") or (
        params.destination.spreadsheet_id or ""
    )
    spreadsheet_url = provider_result.get("spreadsheet_url")
    updated_tabs = provider_result.get("updated_tabs")
    if not isinstance(updated_tabs, list):
        updated_tabs = [
            {
                "title": tab["title"],
                "sheet_id": tab["sheet_id"],
                "row_count": tab["row_count"],
                "column_count": len(tab["columns"]),
            }
            for tab in tabs
        ]
    artifact_ref = {
        "kind": "external_export",
        "export_kind": "google_sheets",
        "format": "google_sheets",
        "provider": "google_sheets",
        "connection_id": params.connection_id,
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": spreadsheet_url,
        "source_kind": params.source.kind,
        "destination_mode": params.destination.mode,
        "write_policy": params.write_policy,
        "updated_tabs": updated_tabs,
    }
    result = ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed",
        project_id=project_id,
        outputs=[
            ActionOutput(
                kind="export",
                name="google_sheets",
                ref=artifact_ref,
            )
        ],
        receipt_id=receipt_id,
    )
    receipt = _export_google_sheets_receipt(
        action=action,
        action_id=action_id,
        project_id=project_id,
        receipt_id=receipt_id,
        params_hash=params_hash,
        artifact_ref=artifact_ref,
        tabs=tabs,
        connection=connection_metadata,
    )
    finalized = _finalize_export_google_sheets_receipt(
        project,
        action,
        params_hash=params_hash,
        project_id=project_id,
        receipt=receipt,
        require_running_status=finalize_requires_running,
    )
    return finalized or result


def _finalize_export_google_sheets_receipt(
    project: Project,
    action: ActionSpec,
    *,
    params_hash: str,
    project_id: str,
    receipt: Receipt,
    require_running_status: bool = True,
) -> ActionResult | None:
    return _finalize_direct_reserved_action_receipt(
        project,
        action,
        params_hash=params_hash,
        project_id=project_id,
        receipt=receipt,
        finalize=_direct_action_finalize_metadata(
            action.kind,
            result_from_existing_fn=_reserved_receipt_result_from_existing,
            require_running_status=require_running_status,
        ),
    )


def _google_sheets_connection_metadata(
    connection_id: str,
    connection: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "id": connection_id,
        "connection_id": connection_id,
        "provider": "google",
        "external_subject": connection.get("external_subject"),
        "external_email": connection.get("external_email"),
    }


def _google_sheets_tabs_for_source(
    project: Project,
    action: ActionSpec,
    params: GoogleSheetsExportRequest,
) -> list[dict[str, Any]] | ActionError:
    if params.source.kind == "all_sheets":
        tabs: list[dict[str, Any]] = []
        for sheet in project.sheets():
            tab_or_error = _google_sheets_tab_for_sheet(
                project,
                int(sheet["id"]),
                query=None,
                action_kind=action.kind,
                query_field="params.source.query",
            )
            if isinstance(tab_or_error, ActionError):
                return tab_or_error
            tabs.append(tab_or_error)
        return tabs

    assert params.source.sheet_id is not None
    return_tab = _google_sheets_tab_for_sheet(
        project,
        params.source.sheet_id,
        query=params.source.query if params.source.kind == "current_view" else None,
        action_kind=action.kind,
        query_field="params.source.query",
    )
    if isinstance(return_tab, ActionError):
        return return_tab
    return [return_tab]


def _google_sheets_tab_for_sheet(
    project: Project,
    sheet_id: int,
    *,
    query: dict[str, Any] | None,
    action_kind: str,
    query_field: str,
) -> dict[str, Any] | ActionError:
    try:
        plan = build_sheet_export_plan(
            project,
            sheet_id,
            query=query,
            media_policy="references",
            value_policy="typed",
            max_rows=MAX_FILTERED_SHEET_CSV_EXPORT_ROWS,
            query_field=query_field,
        )
    except ExportError as exc:
        return _export_error_to_action_error(exc, action_kind)

    columns = list(plan.column_names)
    exported_row_ids: list[int] = []
    data_rows: list[list[Any]] = []
    for batch in iter_export_row_batches(project, plan):
        exported_row_ids.extend(batch.row_ids)
        # native typed row values; the provider client renders them for RAW write
        data_rows.extend(list(row) for row in batch.rows)
    tab: dict[str, Any] = {
        "sheet_id": plan.sheet_id,
        "title": plan.sheet_name,
        "columns": columns,
        "rows": data_rows,
        "row_ids": exported_row_ids,
        "row_count": len(exported_row_ids),
        "column_count": len(columns),
    }
    if plan.query is not None:
        tab.update(
            {
                "query": plan.query,
                "query_hash": plan.query_hash,
                "query_total": plan.query_total,
                "query_evaluator": plan.evaluator,
            }
        )
    return tab


def _export_error_to_action_error(exc: ExportError, action_kind: str) -> ActionError:
    """Map a typed export-core error onto the action error contract."""
    return ActionError(
        code=exc.code,
        message=exc.message,
        action_kind=action_kind,
        field=exc.field,
        details=exc.details or None,
    )


def _precheck_export_destination(
    destination: Path, *, action_kind: str
) -> ActionError | None:
    parent = destination.parent
    if not parent.exists() or not parent.is_dir():
        return ActionError(
            code="invalid_export_destination",
            message=f"{action_kind} destination parent does not exist",
            action_kind=action_kind,
            field="params.destination.path",
            details={"path": str(destination)},
        )
    if destination.exists() and destination.is_dir():
        return ActionError(
            code="invalid_export_destination",
            message=f"{action_kind} destination path is a directory",
            action_kind=action_kind,
            field="params.destination.path",
            details={"path": str(destination)},
        )
    return None


def _precheck_export_package_destination(
    destination: Path, *, action_kind: str
) -> ActionError | None:
    parent = destination.parent
    if not parent.exists() or not parent.is_dir():
        return ActionError(
            code="invalid_export_destination",
            message=f"{action_kind} destination parent does not exist",
            action_kind=action_kind,
            field="params.destination.path",
            details={"path": str(destination)},
        )
    if destination.exists() and not destination.is_dir():
        return ActionError(
            code="invalid_export_destination",
            message=f"{action_kind} destination path is not a directory",
            action_kind=action_kind,
            field="params.destination.path",
            details={"path": str(destination)},
        )
    return None


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


class _ExportDelivery(NamedTuple):
    backup_path: Path | None


def _deliver_export_artifact(tmp_path: Path, destination: Path) -> _ExportDelivery:
    backup_path: Path | None = None
    try:
        if destination.exists():
            backup_path = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.bak"
            )
            destination.replace(backup_path)
        tmp_path.replace(destination)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        if backup_path is not None and backup_path.exists():
            destination.unlink(missing_ok=True)
            backup_path.replace(destination)
        raise
    return _ExportDelivery(backup_path=backup_path)


def _rollback_export_delivery(
    *,
    delivery: _ExportDelivery | None,
    tmp_path: Path,
    destination: Path,
) -> None:
    tmp_path.unlink(missing_ok=True)
    if delivery is None:
        return
    destination.unlink(missing_ok=True)
    if delivery.backup_path is not None and delivery.backup_path.exists():
        delivery.backup_path.replace(destination)


def _finalize_export_delivery(delivery: _ExportDelivery) -> None:
    if delivery.backup_path is None:
        return
    try:
        delivery.backup_path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "export_backup_cleanup_failed",
            exc_info=True,
            extra={"event": "export_backup_cleanup_failed"},
        )


def _export_artifact_replay_error(receipt: Receipt) -> ActionError | None:
    action_kind = receipt.action_kind
    artifact_refs = [
        ref
        for ref in receipt.exports
        if isinstance(ref, dict) and ref.get("kind") == "export_artifact"
    ]
    if not artifact_refs:
        artifact_refs = [
            item.ref
            for item in receipt.outputs
            if item.ref.get("kind") == "export_artifact"
        ]
    if not artifact_refs:
        return ActionError(
            code="export_artifact_missing",
            message=f"{action_kind} replay receipt does not include an artifact ref",
            action_kind=action_kind,
            details={"receipt_id": receipt.receipt_id},
        )

    for ref in artifact_refs:
        path_raw = ref.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            return ActionError(
                code="export_artifact_missing",
                message=f"{action_kind} replay artifact path is missing",
                action_kind=action_kind,
                details={"receipt_id": receipt.receipt_id},
            )
        path = Path(path_raw)
        try:
            actual_bytes, actual_sha256 = _export_artifact_digest(path)
        except OSError as exc:
            return ActionError(
                code="export_artifact_missing",
                message=f"{action_kind} replay artifact is missing",
                action_kind=action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "path": str(path),
                    "error": str(exc),
                },
            )

        expected_bytes = ref.get("byte_count")
        if isinstance(expected_bytes, int) and expected_bytes != actual_bytes:
            return ActionError(
                code="export_artifact_mismatch",
                message=f"{action_kind} replay artifact byte count changed",
                action_kind=action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "path": str(path),
                    "expected_byte_count": expected_bytes,
                    "actual_byte_count": actual_bytes,
                },
            )

        expected_sha256 = ref.get("sha256")
        if isinstance(expected_sha256, str) and expected_sha256 != actual_sha256:
            return ActionError(
                code="export_artifact_mismatch",
                message=f"{action_kind} replay artifact hash changed",
                action_kind=action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "path": str(path),
                    "expected_sha256": expected_sha256,
                    "actual_sha256": actual_sha256,
                },
            )
    return None


def _export_artifact_digest(
    path: Path, *, chunk_size: int = 64 * 1024
) -> tuple[int, str]:
    """Read a replayed artifact in bounded chunks rather than loading it whole."""
    total = 0
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            total += len(chunk)
            digest.update(chunk)
    return total, "sha256:" + digest.hexdigest()


def _export_work_log_receipt(
    *,
    action: ActionSpec,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    artifact_ref: dict[str, Any],
    payload: dict[str, Any],
) -> Receipt:
    sheet_refs = [
        {"name": sheet["name"], "rows": sheet["rows"], "columns": sheet["columns"]}
        for sheet in payload["sheets"]
    ]
    operation_refs = [
        {"op_id": op["id"], "kind": op["kind"], "status": op["status"]}
        for op in payload["operations"]
    ]
    receipt_refs = [
        {
            "receipt_id": receipt["id"],
            "action_kind": receipt["action_kind"],
            "outputs": receipt["outputs"],
            "evidence": receipt["evidence"],
        }
        for receipt in payload.get("receipts", [])
    ]
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[
            ReceiptIO(
                name="project",
                ref={
                    "kind": "project_state",
                    "project_id": project_id,
                    "sheet_count": len(sheet_refs),
                    "operation_count": len(operation_refs),
                    "receipt_count": len(receipt_refs),
                },
            )
        ],
        outputs=[ReceiptIO(name="work_log", ref=artifact_ref)],
        evidence=[
            ReceiptEvidence(
                ref={"kind": "exported_sheets", "sheets": sheet_refs},
                retention="pinned",
            ),
            ReceiptEvidence(
                ref={"kind": "exported_operations", "operations": operation_refs},
                retention="pinned",
            ),
            ReceiptEvidence(
                ref={"kind": "exported_receipts", "receipts": receipt_refs},
                retention="pinned",
            ),
        ],
        exports=[artifact_ref],
    )


def _exported_rows_evidence_ref(payload: dict[str, Any]) -> dict[str, Any]:
    """Bound the exported-rows evidence for large exports.

    Small exports keep an explicit ``exported_rows`` row-id list (back-compat).
    Above ``MAX_EXPLICIT_EXPORT_ROW_IDS`` the receipt records a bounded
    ``exported_rowset`` summary (count + rowset hash + columns + query hash)
    instead of an unbounded row-id array.
    """
    if payload["row_count"] > MAX_EXPLICIT_EXPORT_ROW_IDS:
        column_ids = [
            column["column_id"]
            if column["column_id"] is not None
            else column["source_column_id"]
            for column in payload["columns"]
        ]
        return {
            "kind": "exported_rowset",
            "sheet_id": payload["sheet_id"],
            "row_count": payload["row_count"],
            "rowset_hash": payload.get("rowset_hash")
            or rowset_hash(
                row_ids=payload.get("row_ids", []),
                column_names=[column["name"] for column in payload["columns"]],
                query_hash=payload.get("query_hash"),
                schema_version=payload.get(
                    "schema_version", EXPORT_PLAN_SCHEMA_VERSION
                ),
            ),
            "column_ids": column_ids,
            "query_hash": payload.get("query_hash"),
        }
    return {
        "kind": "exported_rows",
        "sheet_id": payload["sheet_id"],
        "row_ids": payload.get("row_ids", []),
        "row_count": payload["row_count"],
    }


def _export_sheet_csv_receipt(
    *,
    action: ActionSpec,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    artifact_ref: dict[str, Any],
    payload: dict[str, Any],
    output_name: str = "sheet_csv",
) -> Receipt:
    sheet_ref = {
        "kind": "sheet",
        "sheet_id": payload["sheet_id"],
        "sheet_name": payload["sheet_name"],
        "row_count": payload["row_count"],
        "column_count": payload["column_count"],
    }
    column_refs = [
        {
            "kind": "column",
            "sheet_id": payload["sheet_id"],
            **column,
        }
        for column in payload["columns"]
    ]
    evidence = [
        ReceiptEvidence(
            ref={**sheet_ref, "kind": "exported_sheet"},
            retention="pinned",
        ),
        ReceiptEvidence(
            ref={"kind": "exported_columns", "columns": column_refs},
            retention="pinned",
        ),
        ReceiptEvidence(
            ref=_exported_rows_evidence_ref(payload),
            retention="pinned",
        ),
    ]
    if payload.get("query") is not None:
        evidence.append(
            ReceiptEvidence(
                ref={
                    "kind": "exported_query",
                    "sheet_id": payload["sheet_id"],
                    "query": payload["query"],
                    "query_hash": payload["query_hash"],
                    **({"row_ids": payload["row_ids"]} if "row_ids" in payload else {}),
                    "row_count": payload["row_count"],
                    "total": payload["query_total"],
                    "evaluator": payload["query_evaluator"],
                },
                retention="pinned",
            )
        )
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[ReceiptIO(name="sheet", ref=sheet_ref)],
        outputs=[ReceiptIO(name=output_name, ref=artifact_ref)],
        evidence=evidence,
        exports=[artifact_ref],
    )


def _export_google_sheets_receipt(
    *,
    action: ActionSpec,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    artifact_ref: dict[str, Any],
    tabs: list[dict[str, Any]],
    connection: dict[str, Any],
) -> Receipt:
    tab_refs = [
        {
            "kind": "google_sheets_tab_export",
            "sheet_id": tab["sheet_id"],
            "title": tab["title"],
            "row_ids": tab["row_ids"],
            "row_count": tab["row_count"],
            "column_count": len(tab["columns"]),
        }
        for tab in tabs
    ]
    evidence = [
        ReceiptEvidence(
            ref={"kind": "exported_google_sheets_tabs", "tabs": tab_refs},
            retention="pinned",
        )
    ]
    for tab in tabs:
        if tab.get("query") is None:
            continue
        evidence.append(
            ReceiptEvidence(
                ref={
                    "kind": "exported_query",
                    "sheet_id": tab["sheet_id"],
                    "query": tab["query"],
                    "query_hash": tab["query_hash"],
                    "row_ids": tab["row_ids"],
                    "row_count": tab["row_count"],
                    "total": tab["query_total"],
                    "evaluator": tab["query_evaluator"],
                },
                retention="pinned",
            )
        )
    provider_use = [
        {
            "provider": "google_sheets",
            "connection_id": artifact_ref["connection_id"],
            "external_subject": connection.get("external_subject"),
            "external_email": connection.get("external_email"),
        }
    ]
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[
            ReceiptIO(
                name="export_source",
                ref={
                    "kind": "project_sheets",
                    "project_id": project_id,
                    "source": action.params.get("source"),
                    "tabs": tab_refs,
                },
            )
        ],
        outputs=[ReceiptIO(name="google_sheets", ref=artifact_ref)],
        provider_use=provider_use,
        evidence=evidence,
        exports=[artifact_ref],
    )


def _export_google_sheets_failed_receipt(
    *,
    action: ActionSpec,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    error: ActionError,
    tabs: list[dict[str, Any]],
    connection: dict[str, Any],
) -> Receipt:
    tab_refs = [
        {
            "kind": "google_sheets_tab_export",
            "sheet_id": tab["sheet_id"],
            "title": tab["title"],
            "row_ids": tab["row_ids"],
            "row_count": tab["row_count"],
            "column_count": len(tab["columns"]),
        }
        for tab in tabs
    ]
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="failed",
        inputs=[
            ReceiptIO(
                name="export_source",
                ref={
                    "kind": "project_sheets",
                    "project_id": project_id,
                    "source": action.params.get("source"),
                    "tabs": tab_refs,
                },
            )
        ],
        provider_use=[
            {
                "provider": "google_sheets",
                "connection_id": action.params.get("connection_id"),
                "external_subject": connection.get("external_subject"),
                "external_email": connection.get("external_email"),
            }
        ],
        evidence=[
            ReceiptEvidence(
                ref={"kind": "attempted_google_sheets_tabs", "tabs": tab_refs},
                retention="pinned",
            )
        ],
        errors=[error],
    )

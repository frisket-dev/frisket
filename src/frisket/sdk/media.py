"""SDK media-op facts: the shared receipt/replay suite for `media.*` ops.

Every media op (ocr, transcribe, to_markdown, extract_faces, video_frames,
fetch_url, capture_url, ytdlp_download, extract_pdf_tables, extract_metadata,
enclosure_materialize) re-expresses the same reserved-maprunner shape with an
override-heavy resolve/precheck/replay/write suite. Historically every module
stamped its own byte-identical (or near-identical, parameterized) copy of the
run-status decision, receipt-error extraction, provider grouping, value-hash,
replay row-id validation, and the blob-input resolver. Those copies are policy
re-applied per action; a drift in one is a substitution slip nobody catches.

This module owns that copied suite as an SDK capability surface, so an
SDK-authored media op never reaches into executor internals for it. Each media
op keeps only its genuinely op-unique logic (output-column roles, receipt
evidence kinds, named-result routes, per-op replay tails) and calls these
helpers for the shared behavior. Parameterization points are named explicitly:
`action_kind`, the error `code`, the engine `aliases` map, the receipt ref
`kind`, and the resolve `selected` source/`blob_kind`/`missing_blob_message`.

`media_output_column_value_hash` is deliberately NOT `sdk.replay.
output_column_value_hash`: media receipts at rest carry this hash shape, so
replaying them must keep computing it byte-identically.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from frisket.engine.store.runs import FAILURE_OUTCOMES, outcome_sql_list

from frisket.ai.models.metadata import model_calls_cost_actual
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    ActionSpec,
    Receipt,
)
from frisket.contracts.actions.source_inputs import SelectedSource
from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.sdk.maprunner import overwrite_exempt_collisions
from frisket.sdk.replay import replay_output_column_identity


class _MediaPrecheckParams(Protocol):
    sheet_id: int


def media_text_hash(text: str) -> str:
    """Stable ``sha256:`` digest of a text value (source URLs, cell text)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def media_action_status(run: Any) -> str:
    """Map a completed run's row stats to an action status.

    Whole-run failure or every-row-failed collapses to ``failed``; any partial
    failure to ``partial``; otherwise ``completed``.
    """
    run_status = str(run["status"] or "")
    total_rows = int(run["total_rows"] or 0)
    failed_rows = int(run["failed_rows"] or 0)
    if run_status == "failed":
        return "failed"
    if total_rows > 0 and failed_rows >= total_rows:
        return "failed"
    if failed_rows > 0 or run_status == "partial":
        return "partial"
    return "completed"


def media_run_failed_errors(
    action: ActionSpec | str,
    run: Any,
    *,
    status: str,
    code: str,
    message: str | None = None,
) -> list[ActionError]:
    """Return the single typed receipt error for a failed media run."""
    if status != "failed":
        return []
    action_kind = action if isinstance(action, str) else action.kind
    return [
        ActionError(
            code=code,
            message=message or f"{action_kind} failed for every target row",
            action_kind=action_kind,
            details={
                "total_rows": int(run["total_rows"] or 0),
                "completed_rows": int(run["completed_rows"] or 0),
                "failed_rows": int(run["failed_rows"] or 0),
            },
        )
    ]


def media_halt_error(action: ActionSpec | str, run: Any) -> ActionError | None:
    """The typed receipt error for a run the RECIPE halted, or ``None`` when
    the run carries no halt marker (an operator cancellation).

    One construction, shared by every routed media action. A terminal
    generation is immutable, and a halt before any cell publication has no
    exact source head from which ``run.backfill`` can reconstruct an action.
    The receipt therefore reports the halt honestly without advertising the
    deleted in-place recovery contract.
    """
    from frisket.ops.base import persisted_recipe_invocation_halt

    halt = persisted_recipe_invocation_halt(run["params"])
    if halt is None:
        return None
    code, detail = halt
    return ActionError(
        code=code,
        message=detail,
        action_kind=action if isinstance(action, str) else action.kind,
        details={
            "run_id": int(run["id"]),
            "completed_rows": int(run["completed_rows"] or 0),
            "total_rows": int(run["total_rows"] or 0),
        },
    )


def media_provider_for_engine(engine: str, aliases: dict[str, str]) -> str:
    """Resolve a symbolic engine name to a provider via the op's alias map.

    Unknown engines fall back to the ``provider/model`` prefix, else ``unknown``.
    """
    if engine in aliases:
        return aliases[engine]
    return engine.split("/", 1)[0] if "/" in engine else "unknown"


def media_provider_use(
    model_calls: list[Any],
    *,
    engine: str,
    run: Any,
    provider_for_engine: Any,
) -> list[dict[str, Any]]:
    """Group model calls into per-provider receipt provider_use rows.

    ``provider_for_engine`` maps the op's symbolic engine to a provider for the
    zero-call (local engine) case.
    """
    if not model_calls:
        return [
            {
                "provider": provider_for_engine(engine),
                "model": engine,
                "model_call_count": 0,
                "cost_actual": model_calls_cost_actual(model_calls),
            }
        ]
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for call in model_calls:
        key = (str(call["provider"]), str(call["engine"]))
        item = grouped.setdefault(
            key,
            {
                "provider": str(call["provider"]),
                "model": str(call["engine"]),
                "model_call_count": 0,
                "_calls": [],
            },
        )
        item["model_call_count"] += 1
        item["_calls"].append(call)
    provider_use: list[dict[str, Any]] = []
    for item in grouped.values():
        item["cost_actual"] = model_calls_cost_actual(item.pop("_calls"))
        provider_use.append(item)
    return provider_use


def media_successful_result_row_ids(
    project: Project,
    *,
    run_id: int,
    column_id: int,
    row_ids: list[int],
) -> list[int]:
    """Row ids that produced a non-error, non-null result for a column."""
    if not row_ids:
        return []
    placeholders = ",".join("?" for _ in row_ids)
    successful = {
        int(row["row_id"])
        for row in project.db.execute(
            "SELECT row_id FROM results "
            f"WHERE run_id=? AND column_id=? AND row_id IN ({placeholders}) "
            f"AND outcome NOT IN ({outcome_sql_list(FAILURE_OUTCOMES)}) "
            "AND value IS NOT NULL",
            [run_id, column_id, *row_ids],
        ).fetchall()
    }
    return [row_id for row_id in row_ids if row_id in successful]


def precheck_media_outputs(
    project: Project,
    params: _MediaPrecheckParams,
    runner_spec: dict[str, Any],
    *,
    action_kind: str,
    no_output_fields_code: str | None = None,
) -> ActionError | None:
    """Reject output collisions and configured missing-output failures."""
    from frisket.ops.builtin import get_recipe

    output_names = [
        field["name"] for field in get_recipe(action_kind).output_fields(runner_spec)
    ]
    if not output_names and no_output_fields_code is not None:
        return ActionError(
            code=no_output_fields_code,
            message=f"{action_kind} recipe returned no output fields",
            action_kind=action_kind,
            field="params.output_name",
        )
    placeholders = ",".join("?" for _ in output_names)
    rows = project.db.execute(
        f"SELECT name, ai_generated FROM columns "
        f"WHERE sheet_id=? AND hidden=0 AND name IN ({placeholders})",
        [params.sheet_id, *output_names],
    ).fetchall()
    rows = overwrite_exempt_collisions(rows, runner_spec)
    if rows:
        return ActionError(
            code="output_column_exists",
            message=f"{action_kind} output would overwrite an existing column",
            action_kind=action_kind,
            field="params.output_name",
            details={"columns": sorted(row["name"] for row in rows)},
        )
    return None


def media_output_refs_for_role(
    project: Project,
    params: Any,
    runner_spec: dict[str, Any],
    *,
    action_kind: str,
    failure_code: str,
    output_ref_kind: str,
    role: str,
    blob_count_field: str,
    blob_refs_field: str,
    blob_ref_builder: Callable[..., list[dict[str, Any]]],
    run_id: int,
    op_id: int,
    row_ids: list[int],
    extra_fields: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]] | ActionError:
    """Build output-column refs for the extract-faces/video-frames pair."""
    from frisket.ops.builtin import get_recipe

    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? AND current_run_id=?",
            (params.sheet_id, run_id),
        ).fetchall()
    }
    refs: list[dict[str, Any]] = []
    for field in get_recipe(action_kind).output_fields(runner_spec):
        column = columns.get(field["name"])
        if column is None:
            return ActionError(
                code=failure_code,
                message=f"{action_kind} run did not create an expected output column",
                action_kind=action_kind,
                details={"column": field["name"]},
            )
        blob_refs = blob_ref_builder(
            project,
            params,
            column_id=int(column["id"]),
            row_ids=row_ids,
        )
        ref = {
            "kind": output_ref_kind,
            "name": field["name"],
            "role": role,
            "sheet_id": params.sheet_id,
            "column_id": int(column["id"]),
            "run_id": run_id,
            "op_id": op_id,
            "row_ids": row_ids,
            "type": column["type"],
            "schema": field.get("schema") or {},
        }
        if extra_fields is not None:
            ref.update(extra_fields)
        ref.update(
            {
                blob_count_field: len(blob_refs),
                "value_hash": media_output_column_value_hash(
                    project,
                    sheet_id=params.sheet_id,
                    column_id=int(column["id"]),
                    row_ids=row_ids,
                ),
                blob_refs_field: blob_refs,
            }
        )
        refs.append(ref)
    return refs


def media_named_result_refs_for_role(
    project: Project,
    output_refs: list[dict[str, Any]],
    *,
    source_action_kind: str,
    role: str,
    schema_name: str,
    run_id: int,
    row_ids: list[int],
) -> list[dict[str, Any]]:
    """Build feedable named-result refs for one array-valued media role."""
    from frisket.engine.executor.recordsets import (
        DERIVE_TABLE_FROM_LIST,
        feedable_named_result_ref,
    )

    refs: list[dict[str, Any]] = []
    for ref in output_refs:
        if ref.get("role") != role:
            continue
        schema = ref.get("schema") or {}
        if schema.get("type") != "array":
            continue
        route = str(ref["name"])
        refs.append(
            feedable_named_result_ref(
                source_action_kind=source_action_kind,
                sheet_id=ref["sheet_id"],
                column_id=ref["column_id"],
                run_id=ref["run_id"],
                op_id=ref["op_id"],
                route=route,
                schema_name=schema_name,
                schema_json=schema,
                row_ids=media_successful_result_row_ids(
                    project,
                    run_id=run_id,
                    column_id=int(ref["column_id"]),
                    row_ids=row_ids,
                ),
                may_feed=[DERIVE_TABLE_FROM_LIST],
            )
        )
    return refs


def build_media_write_override(
    *,
    action_kind: str,
    failure_code: str,
    output_refs_fn: Callable[..., list[dict[str, Any]] | ActionError],
    receipt_fn: Callable[..., Receipt],
    extra_receipt_kwargs: Mapping[str, str],
) -> Callable[[Any, dict[str, Any]], Callable[..., ActionResult]]:
    """Build the shared URL-acquisition receipt write orchestration.

    ``extra_receipt_kwargs`` maps opaque receipt keyword names to keys in the
    op's resolved facts. The shared owner forwards those values without
    interpreting their op-specific meaning.
    """
    receipt_fact_names = dict(extra_receipt_kwargs)

    def build_write(_decl: Any, holder: dict[str, Any]) -> Callable[..., ActionResult]:
        def write_fn(
            project: Project,
            action: ActionSpec,
            params: Any,
            *,
            resolved: Any,
            runner_spec: dict[str, Any],
            params_hash: str,
            project_id: str,
            action_id: str,
            receipt_id: str,
            run_id: int,
        ) -> ActionResult:
            from frisket.engine.executor.action_reservations import (
                _direct_action_finalize_metadata,
                _finalize_direct_reserved_action_receipt,
                _reserved_maprunner_result_from_existing,
            )
            from frisket.engine.executor.action_support import _failed_result

            facts = resolved.facts
            input_column = facts["input_column"]
            row_ids = facts["row_ids"]
            source_url_refs = facts["source_url_refs"]
            invalid_source_url_refs = facts["invalid_source_url_refs"]
            forwarded_receipt_kwargs = {
                keyword: facts[fact_name]
                for keyword, fact_name in receipt_fact_names.items()
            }

            run = project.db.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                return _failed_result(
                    project_id=project_id,
                    action_kind=action.kind,
                    error=ActionError(
                        code=failure_code,
                        message=f"{action_kind} did not create a run",
                        action_kind=action.kind,
                    ),
                )
            if run["status"] not in {"completed", "partial", "failed"}:
                return _failed_result(
                    project_id=project_id,
                    action_kind=action.kind,
                    error=ActionError(
                        code=failure_code,
                        message=f"{action_kind} run did not complete",
                        action_kind=action.kind,
                        details={"run_status": run["status"]},
                    ),
                )
            op_id = int(run["op_id"])
            result_row_ids = [
                int(row["row_id"])
                for row in project.db.execute(
                    "SELECT DISTINCT row_id FROM results "
                    "WHERE run_id=? ORDER BY row_id",
                    (run_id,),
                ).fetchall()
            ]
            if result_row_ids:
                row_ids = result_row_ids
            output_refs = output_refs_fn(
                project,
                params,
                runner_spec,
                run_id=run_id,
                op_id=op_id,
                row_ids=row_ids,
            )
            if isinstance(output_refs, ActionError):
                return _failed_result(
                    project_id=project_id,
                    action_kind=action.kind,
                    error=output_refs,
                )
            media_blob_refs = [
                blob_ref
                for output_ref in output_refs
                for blob_ref in output_ref.pop("_media_blob_refs", [])
            ]
            outputs = [
                ActionOutput(
                    kind="column",
                    name=ref["name"],
                    sheet_id=params.sheet_id,
                    column_id=ref["column_id"],
                    row_ids=row_ids,
                    ref=ref,
                )
                for ref in output_refs
            ]
            receipt = receipt_fn(
                action=action,
                action_id=action_id,
                project_id=project_id,
                receipt_id=receipt_id,
                params_hash=params_hash,
                op_id=op_id,
                run=run,
                row_ids=row_ids,
                input_column=input_column,
                output_refs=output_refs,
                source_url_refs=source_url_refs,
                invalid_source_url_refs=invalid_source_url_refs,
                media_blob_refs=media_blob_refs,
                runner_spec=runner_spec,
                **forwarded_receipt_kwargs,
            )
            finalize_result = _finalize_direct_reserved_action_receipt(
                project,
                action,
                params_hash=params_hash,
                project_id=project_id,
                receipt=receipt,
                finalize=_direct_action_finalize_metadata(
                    action_kind,
                    result_from_existing_fn=_reserved_maprunner_result_from_existing(
                        holder["spec"]
                    ),
                    require_running_status=False,
                ),
            )
            if finalize_result is not None:
                return finalize_result
            return ActionResult(
                action=ActionIdentity(kind=action.kind, action_id=action_id),
                status=receipt.status,
                project_id=project_id,
                run_id=run_id,
                op_ids=[op_id],
                outputs=outputs,
                receipt_id=receipt_id,
                errors=receipt.errors,
                warnings=receipt.warnings,
            )

        return write_fn

    return build_write


def media_output_column_value_hash(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
) -> str:
    """Deterministic value hash of an output column's cells for replay checks."""
    values = project.get_values(sheet_id, column_id, row_ids=row_ids)
    payload = {str(row_id): values.get(row_id) for row_id in row_ids}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def media_replay_row_ids(
    ref: dict[str, Any],
    *,
    receipt: Receipt,
    output_name: str,
) -> list[int] | ActionError:
    """Validate a receipt output ref's stored row ids for value-hash replay."""
    row_ids = ref.get("row_ids")
    if (
        not isinstance(row_ids, list)
        or not row_ids
        or any(
            not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0
            for row_id in row_ids
        )
        or len(set(row_ids)) != len(row_ids)
    ):
        return ActionError(
            code="stale_replay",
            message=f"{receipt.action_kind} replay receipt has invalid row refs",
            action_kind=receipt.action_kind,
            details={"receipt_id": receipt.receipt_id, "output": output_name},
        )
    return row_ids


def media_blob_replay_error(
    project: Project,
    receipt: Receipt,
    *,
    action_kind: str,
    media_blob_ref_kind: str,
) -> ActionError | None:
    """Validate replay-owned media blob evidence."""
    for evidence in receipt.evidence:
        ref = evidence.ref
        if ref.get("kind") != media_blob_ref_kind:
            continue
        blob_hash = ref.get("blob_hash")
        if not isinstance(blob_hash, str) or not blob_hash:
            return ActionError(
                code="stale_replay",
                message=f"{action_kind} replay blob ref is incomplete",
                action_kind=receipt.action_kind,
                details={"receipt_id": receipt.receipt_id, "ref": ref},
            )
        blob = MediaBlobStore(project).blob_row(blob_hash)
        if blob is None:
            return ActionError(
                code="stale_replay",
                message=f"{action_kind} replay media blob is missing",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "blob_hash": blob_hash,
                },
            )
        try:
            with project.materialize_blob(blob_hash):
                pass
        except BlobNotFoundError:
            return ActionError(
                code="stale_replay",
                message=f"{action_kind} replay media blob is missing",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "blob_hash": blob_hash,
                },
            )
    return None


def media_replay_error(
    project: Project,
    receipt: Receipt,
    *,
    action_kind: str,
    output_column_ref_kind: str,
    media_blob_ref_kind: str | None = None,
) -> ActionError | None:
    """Validate replay-owned media outputs and optional blob evidence."""
    seen_output = False
    for output in receipt.outputs:
        ref = output.ref
        if ref.get("kind") != output_column_ref_kind:
            continue
        seen_output = True
        column_id = ref.get("column_id")
        sheet_id = ref.get("sheet_id")
        row = replay_output_column_identity(
            project,
            receipt,
            output,
            action_kind=action_kind,
        )
        if isinstance(row, ActionError):
            return row
        run_id = ref.get("run_id")
        if isinstance(run_id, int) and row["current_run_id"] != run_id:
            return ActionError(
                code="stale_replay",
                message=f"{action_kind} replay output column changed runs",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "output": output.name,
                    "column_id": column_id,
                    "expected_run_id": run_id,
                    "current_run_id": row["current_run_id"],
                },
            )
        row_ids = media_replay_row_ids(
            ref,
            receipt=receipt,
            output_name=output.name,
        )
        if isinstance(row_ids, ActionError):
            return row_ids
        value_hash = media_output_column_value_hash(
            project,
            sheet_id=sheet_id,
            column_id=column_id,
            row_ids=row_ids,
        )
        if ref.get("value_hash") != value_hash:
            return ActionError(
                code="stale_replay",
                message=f"{action_kind} replay output values changed",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "output": output.name,
                    "column_id": column_id,
                },
            )
    if media_blob_ref_kind is not None:
        blob_error = media_blob_replay_error(
            project,
            receipt,
            action_kind=action_kind,
            media_blob_ref_kind=media_blob_ref_kind,
        )
        if blob_error is not None:
            return blob_error
    if not seen_output:
        return ActionError(
            code="stale_replay",
            message=f"{action_kind} replay receipt has no output column ref",
            action_kind=receipt.action_kind,
            details={"receipt_id": receipt.receipt_id},
        )
    return None


def resolve_media_input_rows(
    project: Project,
    params: Any,
    *,
    action_kind: str,
    selected: SelectedSource,
) -> dict[str, Any] | ActionError:
    """Resolve a visible media input column and its target row scope."""
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
    (input_name,) = selected.names()
    column = project.db.execute(
        "SELECT * FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (params.sheet_id, input_name),
    ).fetchone()
    if column is None:
        return ActionError(
            code="invalid_input_ref",
            message=f"{action_kind} input column does not exist on the sheet",
            action_kind=action_kind,
            field="params.input_columns",
            details={"missing": [input_name]},
        )
    accepted_types = selected.source.accepted_types
    if accepted_types is not None and column["type"] not in accepted_types:
        if len(accepted_types) == 1:
            type_label = accepted_types[0]
        elif len(accepted_types) == 2:
            type_label = " or ".join(accepted_types)
        else:
            type_label = f"{', '.join(accepted_types[:-1])}, or {accepted_types[-1]}"
        return ActionError(
            code="invalid_input_ref",
            message=f"{action_kind} input column must be {type_label}",
            action_kind=action_kind,
            field="params.input_columns",
            details={"column": input_name, "type": column["type"]},
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
    return {
        "input_column": {
            "name": input_name,
            "id": int(column["id"]),
            "type": str(column["type"]),
        },
        "row_ids": row_ids,
    }


def resolve_media_blob_inputs(
    project: Project,
    params: Any,
    *,
    action_kind: str,
    selected: SelectedSource,
    blob_kind: str,
    missing_blob_message: str,
) -> dict[str, Any] | ActionError:
    """Resolve blob-backed media cells after shared input/row validation."""
    resolved = resolve_media_input_rows(
        project,
        params,
        action_kind=action_kind,
        selected=selected,
    )
    if isinstance(resolved, ActionError):
        return resolved
    input_column = resolved["input_column"]
    input_name = input_column["name"]
    column_id = input_column["id"]
    row_ids = resolved["row_ids"]
    values = project.get_values(params.sheet_id, column_id, row_ids=row_ids)
    blob_refs: list[dict[str, Any]] = []
    blob_row_ids: list[int] = []
    for row_id in row_ids:
        value = values.get(row_id)
        # An empty cell is scope, not an error: a media column routinely has
        # rows whose download failed or never ran, and those must not veto
        # the blob-backed rest. Only a present-but-unmaterialized value (a
        # raw URL string in a media cell) refuses the launch — that is the
        # case the missing_blob_message describes.
        if value is None or value == "":
            continue
        if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
            return ActionError(
                code="invalid_input_ref",
                message=missing_blob_message,
                action_kind=action_kind,
                field="params.input_columns",
                details={"row_id": row_id, "column": input_name},
            )
        digest = value["blob"]
        blob = MediaBlobStore(project).blob_row(digest)
        if blob is None:
            return ActionError(
                code="invalid_input_ref",
                message=f"{action_kind} input blob row is missing from the project",
                action_kind=action_kind,
                field="params.input_columns",
                details={"row_id": row_id, "blob_hash": digest},
            )
        source_url = str(blob["source_url"] or "")
        blob_row_ids.append(row_id)
        blob_refs.append(
            {
                "kind": blob_kind,
                "sheet_id": params.sheet_id,
                "row_id": row_id,
                "column_id": column_id,
                "source_column": input_name,
                "blob_hash": digest,
                "mime": value.get("mime") or blob["mime"],
                "filename": value.get("filename") or blob["filename"],
                "size": blob["size"],
                "source_url_hash": media_text_hash(source_url) if source_url else None,
            }
        )
    if not blob_row_ids:
        return ActionError(
            code="invalid_input_ref",
            message=f"{action_kind} found no blob-backed media cells in the target rows",
            action_kind=action_kind,
            field="params.input_columns",
            details={"column": input_name},
        )
    return {
        "input_column": input_column,
        "row_ids": blob_row_ids,
        "blob_refs": blob_refs,
    }

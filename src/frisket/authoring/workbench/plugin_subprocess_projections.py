"""Subprocess lifecycle for plugin projection contributions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from frisket.contracts import plugin_rpc
from frisket.engine.store import Project
from frisket.plugins.process_client import (
    PluginProcessError,
)

from frisket.authoring.workbench.plugin_subprocess import (
    _plugin_process_client,
    _project_plugin_env,
)

TIMELINE_PROJECTION_ARTIFACT_SCHEMA_VERSION = "frisket.timeline_projection_artifact.v1"


@dataclass(frozen=True)
class _TimelineProjectionSource:
    sheet_id: int
    target: dict[str, Any]
    params: dict[str, Any]
    source_generation: str
    target_key: str
    rows: list[dict[str, Any]]
    metrics: dict[str, int]


def run_plugin_projection_status_subprocess(
    project: Project,
    *,
    binding: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    projection_kind = str(payload.get("projectionKind") or binding.kind)
    metadata = dict(getattr(binding, "metadata", {}) or {})
    execution = metadata.get("execution") if isinstance(metadata, dict) else None
    if isinstance(execution, dict) and execution.get("mode") == "runtime_plan":
        return _run_plugin_projection_plan_subprocess(
            project,
            binding=binding,
            payload=payload,
            mode="status",
        )
    source = _resolve_timeline_projection_source(
        project,
        projection_kind=projection_kind,
        target=_payload_object(payload, "target"),
        params=_payload_object(payload, "params"),
    )
    row = _latest_runtime_projection_artifact(
        project,
        projection_kind=projection_kind,
        target_key=source.target_key,
    )
    if row is None:
        return _runtime_projection_status_payload(
            projection_kind=projection_kind,
            status="missing",
            freshness="missing",
            generation=source.source_generation,
            artifact_id=None,
            metrics=source.metrics,
        )
    artifact_id = str(row["artifact_id"])
    metrics = _json_loads_dict(row["metrics_json"])
    metrics.setdefault("sourceRowCount", source.metrics["sourceRowCount"])
    if str(row["source_generation"]) != source.source_generation:
        return _runtime_projection_status_payload(
            projection_kind=projection_kind,
            status="stale",
            freshness="stale",
            generation=source.source_generation,
            artifact_id=artifact_id,
            metrics=metrics,
        )
    return _runtime_projection_status_payload(
        projection_kind=projection_kind,
        status="ready",
        freshness="fresh",
        generation=source.source_generation,
        artifact_id=artifact_id,
        metrics=metrics,
    )


def run_plugin_projection_build_subprocess(
    project: Project,
    *,
    binding: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    projection_kind = str(payload.get("projectionKind") or binding.kind)
    project_id = str(payload.get("projectId") or "")
    mode = str(payload.get("mode") or "refresh")
    if mode not in {"refresh", "rebuild"}:
        raise _projection_error(
            "invalid_runtime_projection_request",
            "runtime projection build mode must be refresh or rebuild",
            projection_kind=projection_kind,
            field="mode",
        )
    metadata = dict(getattr(binding, "metadata", {}) or {})
    execution = metadata.get("execution") if isinstance(metadata, dict) else None
    if isinstance(execution, dict) and execution.get("mode") == "runtime_plan":
        return _run_plugin_projection_plan_subprocess(
            project,
            binding=binding,
            payload=payload,
            mode="build",
        )
    source = _resolve_timeline_projection_source(
        project,
        projection_kind=projection_kind,
        target=_payload_object(payload, "target"),
        params=_payload_object(payload, "params"),
    )
    current = _latest_runtime_projection_artifact(
        project,
        projection_kind=projection_kind,
        target_key=source.target_key,
    )
    if (
        mode == "refresh"
        and current is not None
        and str(current["source_generation"]) == source.source_generation
    ):
        return _runtime_projection_build_payload(
            projection_kind=projection_kind,
            status="noop",
            operation="noop",
            idempotency_key=_projection_build_idempotency_key(
                projection_kind=projection_kind,
                generation=source.source_generation,
                mode=mode,
            ),
            artifact_id=str(current["artifact_id"]),
            metrics=_json_loads_dict(current["metrics_json"]),
        )

    module_path = str(metadata.get("module_path") or "")
    plugin_root = str(metadata.get("plugin_root") or "")
    if not module_path or not plugin_root:
        raise _projection_error(
            "plugin_projection_entrypoint_missing",
            "Plugin projection binding is missing subprocess entrypoint metadata",
            projection_kind=projection_kind,
        )

    env = _project_plugin_env(project, plugin_id=binding.plugin, metadata=metadata)
    if isinstance(env, dict) and "_error" in env:
        error = env["_error"]
        errors = error.get("errors") if isinstance(error, dict) else None
        first = errors[0] if isinstance(errors, list) and errors else {}
        raise _projection_error(
            str(first.get("code") or "plugin_env_missing"),
            str(first.get("message") or "Configure required plugin env vars"),
            projection_kind=projection_kind,
            field="plugin_env",
        )
    assert isinstance(env, dict)

    request = {
        "pluginId": binding.plugin,
        "handlerKey": binding.handler_key,
        "projectionKind": projection_kind,
        "modulePath": module_path,
        "target": source.target,
        "params": source.params,
        "context": {
            "projectId": project_id,
            "pluginId": binding.plugin,
            "handlerKey": binding.handler_key,
            "projectionKind": projection_kind,
            "capabilities": [
                "projection.build",
                "projection.artifact.write",
            ],
        },
        "rows": source.rows,
    }
    child = _run_child_projection(
        plugin_root=plugin_root,
        request=request,
        env=env,
    )
    if child.get("status") != "completed":
        errors = child.get("errors")
        first = errors[0] if isinstance(errors, list) and errors else {}
        raise _projection_error(
            str(first.get("code") or "plugin_projection_failed"),
            str(first.get("message") or "Trusted-local plugin projection failed"),
            projection_kind=projection_kind,
        )
    items = _validated_timeline_projection_items(
        child.get("items"),
        source_row_ids={int(row["rowId"]) for row in source.rows},
        projection_kind=projection_kind,
    )
    metrics: dict[str, Any] = {
        **source.metrics,
        "timelineItemCount": len(items),
    }
    child_metrics = child.get("metrics")
    if isinstance(child_metrics, dict):
        for key, value in child_metrics.items():
            if isinstance(value, (int, float, str, bool)) or value is None:
                metrics[str(key)] = value
    artifact_id = _projection_artifact_id(
        projection_kind=projection_kind,
        target_key=source.target_key,
        generation=source.source_generation,
    )
    body = {
        "schemaVersion": TIMELINE_PROJECTION_ARTIFACT_SCHEMA_VERSION,
        "projectionKind": projection_kind,
        "artifactId": artifact_id,
        "generation": source.source_generation,
        "target": source.target,
        "params": source.params,
        "columns": {
            "dateColumnId": source.target["dateColumnId"],
            "titleColumnId": source.target["titleColumnId"],
            **(
                {"caseColumnId": source.target["caseColumnId"]}
                if source.target.get("caseColumnId") is not None
                else {}
            ),
        },
        "metrics": metrics,
        "items": items,
    }
    _upsert_runtime_projection_artifact(
        project,
        projection_kind=projection_kind,
        plugin_id=str(binding.plugin),
        artifact_id=artifact_id,
        source=source,
        body=body,
        metrics=metrics,
    )
    return _runtime_projection_build_payload(
        projection_kind=projection_kind,
        status="accepted",
        operation=mode,
        idempotency_key=_projection_build_idempotency_key(
            projection_kind=projection_kind,
            generation=source.source_generation,
            mode=mode,
        ),
        artifact_id=artifact_id,
        metrics=metrics,
    )


def _run_plugin_projection_plan_subprocess(
    project: Project,
    *,
    binding: Any,
    payload: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    projection_kind = str(payload.get("projectionKind") or binding.kind)
    metadata = dict(getattr(binding, "metadata", {}) or {})
    module_path = str(metadata.get("module_path") or "")
    plugin_root = str(metadata.get("plugin_root") or "")
    if not module_path or not plugin_root:
        raise _projection_error(
            "plugin_projection_entrypoint_missing",
            "Plugin projection binding is missing subprocess entrypoint metadata",
            projection_kind=projection_kind,
        )
    env = _project_plugin_env(project, plugin_id=binding.plugin, metadata=metadata)
    if isinstance(env, dict) and "_error" in env:
        error = env["_error"]
        errors = error.get("errors") if isinstance(error, dict) else None
        first = errors[0] if isinstance(errors, list) and errors else {}
        raise _projection_error(
            str(first.get("code") or "plugin_env_missing"),
            str(first.get("message") or "Configure required plugin env vars"),
            projection_kind=projection_kind,
            field="plugin_env",
        )
    assert isinstance(env, dict)
    request = {
        "pluginId": binding.plugin,
        "handlerKey": binding.handler_key,
        "projectionKind": projection_kind,
        "modulePath": module_path,
        "operation": mode,
        "target": _payload_object(payload, "target"),
        "params": _payload_object(payload, "params"),
        "context": {
            "projectId": str(payload.get("projectId") or ""),
            "pluginId": binding.plugin,
            "handlerKey": binding.handler_key,
            "projectionKind": projection_kind,
            "capabilities": [
                "projection.status" if mode == "status" else "projection.build"
            ],
        },
        "rows": [],
    }
    child = _run_child_projection(
        plugin_root=plugin_root,
        request=request,
        env=env,
    )
    if child.get("status") != "completed":
        errors = child.get("errors")
        first = errors[0] if isinstance(errors, list) and errors else {}
        raise _projection_error(
            str(first.get("code") or "plugin_projection_failed"),
            str(first.get("message") or "Trusted-local plugin projection failed"),
            projection_kind=projection_kind,
        )
    plan = child.get("plan")
    if not isinstance(plan, dict):
        raise _projection_error(
            "plugin_projection_invalid_response",
            "Plugin projection plan response is missing",
            projection_kind=projection_kind,
        )
    return plan


def read_plugin_projection_artifact(
    project: Project,
    *,
    projection_kind: str,
    artifact_id: str,
    target: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    source = _resolve_timeline_projection_source(
        project,
        projection_kind=projection_kind,
        target=target,
        params=params,
    )
    row = project.db.execute(
        "SELECT * FROM runtime_projection_artifacts "
        "WHERE artifact_id=? AND projection_kind=?",
        (artifact_id, projection_kind),
    ).fetchone()
    if row is None:
        raise _projection_error(
            "runtime_projection_artifact_missing",
            "runtime projection artifact was not found",
            projection_kind=projection_kind,
            field="artifactId",
        )
    if str(row["target_key"]) != source.target_key:
        raise _projection_error(
            "runtime_projection_artifact_target_mismatch",
            "runtime projection artifact does not match the requested target",
            projection_kind=projection_kind,
            field="target",
        )
    body = json.loads(str(row["body_json"]))
    if not isinstance(body, dict):
        raise _projection_error(
            "runtime_projection_artifact_invalid",
            "runtime projection artifact is invalid",
            projection_kind=projection_kind,
            field="artifactId",
        )
    return body


def _run_child_projection(
    *,
    plugin_root: str,
    request: dict[str, Any],
    env: dict[str, str],
) -> dict[str, Any]:
    try:
        typed_request = plugin_rpc.ProjectionRequest.model_validate(request)
        client = _plugin_process_client(plugin_root)
        return _projection_result_from_frames(client.projection(typed_request, env))
    except PluginProcessError as exc:
        return exc.failure.as_dict()


def _projection_result_from_frames(
    frames: Iterator[plugin_rpc.ProjectionFrame],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    plan: dict[str, Any] | None = None
    for frame in frames:
        if isinstance(frame, plugin_rpc.ProjectionTimelineItemFrame):
            items.append(frame.item)
        elif isinstance(frame, plugin_rpc.ProjectionPlanFrame):
            plan = frame.plan
        elif isinstance(frame, plugin_rpc.ProjectionDoneFrame):
            metrics = dict(frame.metrics)
        elif isinstance(frame, plugin_rpc.ProjectionErrorFrame):
            return {
                "status": "failed",
                "errors": [
                    {
                        "code": frame.error.code,
                        "message": frame.error.message,
                    }
                ],
            }
    return {
        "status": "completed",
        "items": items,
        "plan": plan,
        "metrics": metrics,
        "errors": [],
        "warnings": [],
    }


def _payload_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise _projection_error(
            "invalid_runtime_projection_request",
            f"runtime projection {key} must be an object",
            projection_kind=str(payload.get("projectionKind") or ""),
            field=key,
        )
    return dict(value)


def _resolve_timeline_projection_source(
    project: Project,
    *,
    projection_kind: str,
    target: dict[str, Any],
    params: dict[str, Any],
) -> _TimelineProjectionSource:
    sheet_id = _required_positive_int(
        target.get("sheetId", target.get("sheet_id")),
        field="target.sheetId",
        projection_kind=projection_kind,
    )
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise _projection_error(
            "invalid_runtime_projection_target",
            "runtime projection target sheet does not exist",
            projection_kind=projection_kind,
            field="target.sheetId",
        )
    columns = project.columns(sheet_id)
    if not columns:
        raise _projection_error(
            "invalid_runtime_projection_target",
            "runtime projection target sheet has no visible columns",
            projection_kind=projection_kind,
            field="target.sheetId",
        )
    date_column_id = _projection_column_id(
        columns,
        target.get("dateColumnId", target.get("date_column_id")),
        role="date",
        required_type="date",
        projection_kind=projection_kind,
        field="target.dateColumnId",
        fallback=lambda: _first_column_id(columns, type_="date"),
    )
    title_column_id = _projection_column_id(
        columns,
        target.get("titleColumnId", target.get("title_column_id")),
        role="title",
        required_type=None,
        projection_kind=projection_kind,
        field="target.titleColumnId",
        fallback=lambda: _first_text_column_id(columns, exclude={date_column_id}),
    )
    case_column_id = _projection_column_id(
        columns,
        target.get("caseColumnId", target.get("case_column_id")),
        role="case",
        required_type=None,
        projection_kind=projection_kind,
        field="target.caseColumnId",
        fallback=lambda: _first_case_column_id(
            columns, exclude={date_column_id, title_column_id}
        ),
        optional=True,
    )
    row_ids = project.visible_row_ids(sheet_id)
    date_values = project.get_values(sheet_id, date_column_id, row_ids=row_ids)
    title_values = project.get_values(sheet_id, title_column_id, row_ids=row_ids)
    case_values = (
        project.get_values(sheet_id, case_column_id, row_ids=row_ids)
        if case_column_id is not None
        else {}
    )
    rows = [
        {
            "rowId": row_id,
            "inputs": {
                "date": date_values.get(row_id),
                "title": title_values.get(row_id),
                **(
                    {"case_id": case_values.get(row_id)}
                    if case_column_id is not None
                    else {}
                ),
            },
        }
        for row_id in row_ids
    ]
    normalized_target = {
        "sheetId": sheet_id,
        "dateColumnId": date_column_id,
        "titleColumnId": title_column_id,
        **({"caseColumnId": case_column_id} if case_column_id is not None else {}),
    }
    generation = _sha256_json(
        {
            "projectionKind": projection_kind,
            "target": normalized_target,
            "params": params,
            "rows": rows,
        }
    )
    target_key = _sha256_json(
        {
            "projectionKind": projection_kind,
            "target": normalized_target,
            "params": params,
        }
    )
    return _TimelineProjectionSource(
        sheet_id=sheet_id,
        target=normalized_target,
        params=dict(params),
        source_generation=generation,
        target_key=target_key,
        rows=rows,
        metrics={"sourceRowCount": len(rows)},
    )


def _required_positive_int(
    value: Any,
    *,
    field: str,
    projection_kind: str,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise _projection_error(
            "invalid_runtime_projection_target",
            f"{field} must be a positive integer",
            projection_kind=projection_kind,
            field=field,
        ) from None
    if parsed <= 0:
        raise _projection_error(
            "invalid_runtime_projection_target",
            f"{field} must be a positive integer",
            projection_kind=projection_kind,
            field=field,
        )
    return parsed


def _projection_column_id(
    columns: list[Any],
    value: Any,
    *,
    role: str,
    required_type: str | None,
    projection_kind: str,
    field: str,
    fallback: Callable[[], int | None],
    optional: bool = False,
) -> int | None:
    column_by_id = {int(column["id"]): column for column in columns}
    if value is None:
        resolved = fallback()
        if resolved is not None or optional:
            return resolved
        raise _projection_error(
            "invalid_runtime_projection_target",
            f"runtime projection target is missing a {role} column",
            projection_kind=projection_kind,
            field=field,
        )
    column_id = _required_positive_int(
        value, field=field, projection_kind=projection_kind
    )
    column = column_by_id.get(column_id)
    if column is None:
        raise _projection_error(
            "invalid_runtime_projection_target",
            f"runtime projection {role} column is not visible on the sheet",
            projection_kind=projection_kind,
            field=field,
        )
    if required_type is not None and str(column["type"]) != required_type:
        raise _projection_error(
            "invalid_runtime_projection_target",
            f"runtime projection {role} column must be type {required_type}",
            projection_kind=projection_kind,
            field=field,
        )
    return column_id


def _first_column_id(columns: list[Any], *, type_: str) -> int | None:
    for column in columns:
        if str(column["type"]) == type_:
            return int(column["id"])
    return None


def _first_text_column_id(columns: list[Any], *, exclude: set[int]) -> int | None:
    for column in columns:
        column_id = int(column["id"])
        if column_id not in exclude and str(column["type"]) == "text":
            return column_id
    for column in columns:
        column_id = int(column["id"])
        if column_id not in exclude:
            return column_id
    return None


def _first_case_column_id(columns: list[Any], *, exclude: set[int]) -> int | None:
    for column in columns:
        column_id = int(column["id"])
        if column_id in exclude:
            continue
        name = re.sub(r"[^a-z0-9]+", "_", str(column["name"]).strip().lower())
        if name in {"case", "case_id", "case_number", "id"}:
            return column_id
    return None


def _latest_runtime_projection_artifact(
    project: Project,
    *,
    projection_kind: str,
    target_key: str,
) -> Any | None:
    return project.db.execute(
        "SELECT * FROM runtime_projection_artifacts "
        "WHERE projection_kind=? AND target_key=? "
        "ORDER BY updated_at DESC, artifact_id DESC LIMIT 1",
        (projection_kind, target_key),
    ).fetchone()


def _runtime_projection_status_payload(
    *,
    projection_kind: str,
    status: str,
    freshness: str,
    generation: str,
    artifact_id: str | None,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schemaVersion": "frisket.runtime_projection_status.v1",
        "status": status,
        "freshness": {
            "state": freshness,
            "generation": generation,
            "transient": False,
        },
        "outputs": {
            "artifactRefs": _projection_artifact_refs(
                projection_kind=projection_kind, artifact_id=artifact_id
            ),
            "metrics": metrics,
        },
        "warnings": [],
    }


def _runtime_projection_build_payload(
    *,
    projection_kind: str,
    status: str,
    operation: str,
    idempotency_key: str,
    artifact_id: str | None,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schemaVersion": "frisket.runtime_projection_build_plan.v1",
        "status": status,
        "build": {
            "operation": operation,
            "idempotencyKey": idempotency_key,
        },
        "outputs": {
            "artifactRefs": _projection_artifact_refs(
                projection_kind=projection_kind, artifact_id=artifact_id
            ),
            "metrics": metrics,
        },
        "warnings": [],
    }


def _projection_artifact_refs(
    *, projection_kind: str, artifact_id: str | None
) -> list[dict[str, Any]]:
    if artifact_id is None:
        return []
    return [
        {
            "kind": "projection_artifact",
            "projectionKind": projection_kind,
            "artifactId": artifact_id,
        }
    ]


def _projection_build_idempotency_key(
    *,
    projection_kind: str,
    generation: str,
    mode: str,
) -> str:
    return f"plugin-projection:{projection_kind}:{mode}@sha256:{generation[:24]}"


def _projection_artifact_id(
    *,
    projection_kind: str,
    target_key: str,
    generation: str,
) -> str:
    safe_kind = re.sub(r"[^A-Za-z0-9_.:-]+", "_", projection_kind).strip("_")
    return f"projection://{safe_kind}/{target_key[:24]}/{generation[:24]}"


def _validated_timeline_projection_items(
    raw_items: Any,
    *,
    source_row_ids: set[int],
    projection_kind: str,
) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        raise _projection_error(
            "plugin_projection_invalid_response",
            "Plugin projection must return timeline items",
            projection_kind=projection_kind,
            field="items",
        )
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise _projection_error(
                "plugin_projection_invalid_response",
                "Plugin projection timeline items must be objects",
                projection_kind=projection_kind,
                field="items",
            )
        source_row_id = _required_positive_int(
            raw.get("sourceRowId", raw.get("source_row_id")),
            field="items.sourceRowId",
            projection_kind=projection_kind,
        )
        if source_row_id not in source_row_ids:
            raise _projection_error(
                "plugin_projection_invalid_response",
                "Plugin projection item referenced a row outside the source set",
                projection_kind=projection_kind,
                field="items.sourceRowId",
            )
        date = str(raw.get("date") or "").strip()
        if not date:
            raise _projection_error(
                "plugin_projection_invalid_response",
                "Plugin projection item date is required",
                projection_kind=projection_kind,
                field="items.date",
            )
        item = {
            "sourceRowId": source_row_id,
            "date": date,
            "title": str(raw.get("title") or "").strip(),
        }
        case_id = raw.get("caseId", raw.get("case_id"))
        if case_id is not None and str(case_id).strip():
            item["caseId"] = str(case_id).strip()
        items.append(item)
    return sorted(
        items,
        key=lambda item: (
            str(item["date"]),
            str(item["title"]),
            int(item["sourceRowId"]),
        ),
    )


def _upsert_runtime_projection_artifact(
    project: Project,
    *,
    projection_kind: str,
    plugin_id: str,
    artifact_id: str,
    source: _TimelineProjectionSource,
    body: dict[str, Any],
    metrics: dict[str, Any],
) -> None:
    project.db.execute(
        "INSERT OR REPLACE INTO runtime_projection_artifacts ("
        "artifact_id, projection_kind, plugin_id, target_key, "
        "source_generation, target_json, params_json, body_json, metrics_json, "
        "created_at, updated_at"
        ") VALUES ("
        "?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "COALESCE((SELECT created_at FROM runtime_projection_artifacts "
        "WHERE artifact_id=?), datetime('now')), datetime('now')"
        ")",
        (
            artifact_id,
            projection_kind,
            plugin_id,
            source.target_key,
            source.source_generation,
            json.dumps(source.target, separators=(",", ":"), sort_keys=True),
            json.dumps(source.params, separators=(",", ":"), sort_keys=True),
            json.dumps(body, separators=(",", ":"), sort_keys=True),
            json.dumps(metrics, separators=(",", ":"), sort_keys=True),
            artifact_id,
        ),
    )
    project.db.commit()


def _sha256_json(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _json_loads_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _projection_error(
    code: str,
    message: str,
    *,
    projection_kind: str,
    field: str | None = None,
) -> Exception:
    from frisket.engine.projections.runtime import ProjectionRuntimeError

    return ProjectionRuntimeError(
        code,
        message,
        projection_kind=projection_kind or None,
        field=field,
    )

"""Subprocess lifecycle for plugin operator contributions."""

from __future__ import annotations

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


def run_plugin_operator_subprocess(
    project: Project,
    *,
    binding: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    metadata = dict(getattr(binding, "metadata", {}) or {})
    module_path = str(metadata.get("module_path") or "")
    plugin_root = str(metadata.get("plugin_root") or "")
    if not module_path or not plugin_root:
        raise RuntimeError("plugin operator binding is missing subprocess metadata")

    secret_values = _project_plugin_env(
        project, plugin_id=binding.plugin, metadata=metadata
    )
    if isinstance(secret_values, dict) and "_error" in secret_values:
        raise RuntimeError("plugin operator env vars are not configured")
    assert isinstance(secret_values, dict)

    sheet_id = int(payload["sheetId"])
    candidate_row_ids = [int(row_id) for row_id in payload["candidateRowIds"]]
    column = payload["column"]
    column_id = int(column["id"])
    values = project.get_values(sheet_id, column_id, row_ids=candidate_row_ids)
    rows = [
        {"rowId": row_id, "value": values.get(row_id)} for row_id in candidate_row_ids
    ]
    request = {
        "pluginId": binding.plugin,
        "handlerKey": binding.handler_key,
        "operatorKind": payload["operatorKind"],
        "modulePath": module_path,
        "target": {
            "sheetId": sheet_id,
            "column": {
                "id": column_id,
                "name": str(column["name"]),
                "type": str(column["type"]),
            },
        },
        "params": dict(metadata.get("params") or {}),
        "value": payload.get("value"),
        "context": {
            "projectId": str(payload.get("projectId") or ""),
            "pluginId": binding.plugin,
            "handlerKey": binding.handler_key,
            "operatorKind": payload["operatorKind"],
            "capabilities": ["operator.filter"],
            "requiresSecrets": [
                str(name)
                for name in metadata.get("requires_secrets", [])
                if isinstance(name, str) and name
            ],
            "secretValues": secret_values,
        },
        "rows": rows,
    }
    child = _run_child_operator(plugin_root=plugin_root, request=request)
    if child.get("status") != "completed":
        raise RuntimeError("trusted-local plugin operator failed")
    plan = child.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("trusted-local plugin operator returned invalid data")
    return plan


def _run_child_operator(
    *,
    plugin_root: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    try:
        typed_request = plugin_rpc.OperatorRequest.model_validate(request)
        client = _plugin_process_client(plugin_root)
        return client.response_payload(client.operator(typed_request))
    except PluginProcessError as exc:
        return exc.failure.as_dict()

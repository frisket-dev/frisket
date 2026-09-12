"""Subprocess lifecycle for plugin importer contributions."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from typing import Any

from frisket.authoring import column_types
from frisket.contracts.action import (
    MAX_IMPORT_ROWS_COLUMNS,
    canonical_column_type,
)
from frisket.contracts import plugin_rpc
from frisket.engine.store import Project
from frisket.actions.types import TableError
from frisket.engine.store.artifact_timeline import TimelineError
from frisket.features.temporal_ingress import validate_project_contextual_value
from frisket.plugins.process_client import (
    PluginProcessError,
)

from frisket.authoring.workbench.plugin_subprocess import (
    _failed,
    _plugin_process_client,
    _project_plugin_secrets,
    _required_plugin_env_names,
)


def run_plugin_importer_subprocess(
    project: Project,
    *,
    source: dict[str, Any],
    handler_params: dict[str, Any],
    project_id: str,
    binding: Any,
    row_limit: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if row_limit is not None and (isinstance(row_limit, bool) or row_limit < 1):
        raise ValueError("runtime importer row_limit must be positive")
    if cancelled is not None and cancelled():
        raise TableError("action_cancelled", "Importer read was cancelled.")
    metadata = dict(getattr(binding, "metadata", {}) or {})
    # Bounded reads are admitted without secrets before reaching this adapter.
    secret_values = (
        {}
        if row_limit is not None
        else _project_plugin_secrets(project, plugin_id=binding.plugin, metadata=metadata)
    )
    if isinstance(secret_values, dict) and "_error" in secret_values:
        return secret_values["_error"]
    assert isinstance(secret_values, dict)

    module_path = str(metadata.get("module_path") or "")
    plugin_root = str(metadata.get("plugin_root") or "")
    if not module_path or not plugin_root:
        return _failed(
            "plugin_importer_entrypoint_missing",
            "Plugin importer binding is missing subprocess entrypoint metadata",
        )

    request = {
        "pluginId": binding.plugin,
        "handlerKey": binding.handler_key,
        "importerKind": binding.kind,
        "modulePath": module_path,
        "source": source,
        "handlerParams": handler_params,
        "context": {
            "projectId": project_id,
            "pluginId": binding.plugin,
            "handlerKey": binding.handler_key,
            "importerKind": binding.kind,
            "capabilities": [] if row_limit is not None else ["project:write"],
            "requiresSecrets": []
            if row_limit is not None
            else _required_plugin_env_names(metadata),
            "secretValues": secret_values,
        },
    }
    return _run_child_importer(
        project,
        plugin_root=plugin_root,
        request=request,
        binding=binding,
        source_label=source.get("label"),
        row_limit=row_limit,
        cancelled=cancelled,
    )


def _run_child_importer(
    project: Project,
    *,
    plugin_root: str,
    request: dict[str, Any],
    binding: Any,
    source_label: str,
    row_limit: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    builder = _ImporterPlanBuilder(
        project,
        binding=binding,
        source_label=source_label,
    )
    try:
        typed_request = plugin_rpc.ImporterRequest.model_validate(request)
        client = _plugin_process_client(plugin_root)
        frames = client.importer(typed_request, should_cancel=cancelled)
        prefix = False
        try:
            for frame in frames:
                if cancelled is not None and cancelled():
                    raise TableError("action_cancelled", "Importer read was cancelled.")
                builder.handle_frame(client.frame_payload(frame))
                if row_limit is not None and builder.row_count >= row_limit:
                    prefix = not builder.saw_done
                    break
        finally:
            frames.close()
        if cancelled is not None and cancelled():
            raise TableError("action_cancelled", "Importer read was cancelled.")
        return builder.finish(prefix=prefix)
    except PluginProcessError as exc:
        builder.close()
        if cancelled is not None and cancelled():
            raise TableError(
                "action_cancelled", "Importer read was cancelled."
            ) from exc
        return exc.failure.as_dict()
    except BaseException:
        builder.close()
        raise


class _SpooledImporterRows:
    """Own the validated spool until consumption or explicit abandonment."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def __iter__(self):
        return self

    def __next__(self):
        if self.stream.closed:
            raise StopIteration
        line = self.stream.readline()
        if not line:
            self.close()
            raise StopIteration
        return json.loads(line)

    def close(self) -> None:
        self.stream.close()


class _ImporterPlanBuilder:
    def __init__(self, project: Project, *, binding: Any, source_label: str) -> None:
        self.project = project
        self.binding = binding
        self.source_label = source_label
        self.columns: list[dict[str, Any]] | None = None
        self.column_types_by_name: dict[str, str] = {}
        self.diagnostics: list[dict[str, Any]] = []
        self.saw_done = False
        self.row_count = 0
        self.frame_types: list[str] = []
        self.error: dict[str, Any] | None = None
        self.stream = tempfile.SpooledTemporaryFile(mode="w+t", max_size=1_000_000)

    def handle_frame(self, frame: dict[str, Any]) -> None:
        if self.error is not None or self.saw_done:
            return
        frame_type = str(frame["type"])
        self.frame_types.append(frame_type)
        if frame_type == "schema":
            self._handle_schema_frame(frame)
            return
        if frame_type == "diagnostic":
            diagnostic = _sanitize_importer_diagnostic(frame.get("diagnostic"))
            if diagnostic is not None:
                self.diagnostics.append(diagnostic)
            return
        if frame_type == "error":
            self._handle_error_frame(frame)
            return
        if frame_type == "row":
            self._handle_row_frame(frame)
            return
        if frame_type == "done":
            self.saw_done = True
            return
        raise AssertionError(f"unhandled validated importer frame {frame_type!r}")

    def close(self) -> None:
        self.stream.close()

    def finish(self, *, prefix: bool = False) -> dict[str, Any]:
        handed_off = False
        try:
            if self.error is not None:
                return self.error
            if self.columns is None:
                return _failed(
                    "plugin_importer_schema_missing",
                    "Plugin importer must emit a schema frame",
                    field="response.schema",
                )
            if not self.saw_done and not prefix:
                return _failed(
                    "plugin_subprocess_invalid_response",
                    "Trusted-local plugin importer did not finish the row stream",
                    field="response",
                )
            self.stream.seek(0)
            rows = _SpooledImporterRows(self.stream)
            handed_off = True
            return {
                "schemaVersion": "frisket.runtime_importer_plan.v1",
                "columns": self.columns,
                "rows": rows,
                "warnings": [
                    str(item.get("message") or item.get("code"))
                    for item in self.diagnostics
                    if item.get("level") == "warning"
                ],
                "diagnostics": self.diagnostics,
                "streaming": {
                    "kind": "plugin_importer_stream",
                    "schema_version": "frisket.plugin_importer_stream_evidence.v1",
                    "plugin_id": self.binding.plugin,
                    "importer_kind": self.binding.kind,
                    "handler_key": self.binding.handler_key,
                    "source_label": self.source_label,
                    "spooled": True,
                    "frame_types": self.frame_types,
                    "row_count": self.row_count,
                    "complete": self.saw_done,
                },
            }
        finally:
            if not handed_off:
                self.stream.close()

    def _handle_schema_frame(self, frame: dict[str, Any]) -> None:
        if self.columns is not None or self.row_count:
            self.error = _failed(
                "plugin_importer_schema_invalid",
                "Plugin importer schema must be the first frame",
                field="response.schema",
            )
            return
        parsed_columns = _parse_importer_columns(self.project, frame.get("columns"))
        if isinstance(parsed_columns, dict):
            self.error = parsed_columns
            return
        self.columns = parsed_columns
        self.column_types_by_name = {
            str(column["name"]): canonical_column_type(str(column["type"]))
            for column in self.columns
        }

    def _handle_error_frame(self, frame: dict[str, Any]) -> None:
        error = frame.get("error") if isinstance(frame.get("error"), dict) else {}
        self.diagnostics.extend(
            _sanitize_importer_diagnostics(
                error.get("details", {}).get("diagnostics")
                if isinstance(error.get("details"), dict)
                else None
            )
        )
        self.error = _failed(
            str(error.get("code") or "plugin_importer_failed"),
            str(error.get("message") or "Plugin importer failed"),
            field="params.importer_kind",
            details={"diagnostics": self.diagnostics} if self.diagnostics else None,
        )

    def _handle_row_frame(self, frame: dict[str, Any]) -> None:
        if self.columns is None:
            self.error = _failed(
                "plugin_importer_schema_missing",
                "Plugin importer must emit a schema frame before rows",
                field="response.schema",
            )
            return
        self.row_count += 1
        parsed_row = _parse_importer_row(
            frame.get("row"),
            project=self.project,
            column_types_by_name=self.column_types_by_name,
            row_number=self.row_count,
        )
        if isinstance(parsed_row, dict) and "_error" in parsed_row:
            self.error = parsed_row["_error"]
            return
        assert isinstance(parsed_row, dict)
        self.stream.write(json.dumps(parsed_row, separators=(",", ":"), sort_keys=True))
        self.stream.write("\n")


def _parse_importer_columns(
    project: Project,
    raw_columns: Any,
) -> list[dict[str, Any]] | dict[str, Any]:
    from frisket.authoring.workbench.plugin_runtime_capabilities import (
        project_allows_plugin_column_type,
    )

    if (
        not isinstance(raw_columns, list)
        or not raw_columns
        or len(raw_columns) > MAX_IMPORT_ROWS_COLUMNS
    ):
        return _failed(
            "plugin_importer_schema_invalid",
            "Plugin importer schema must declare one or more columns",
            field="response.columns",
        )
    columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_columns):
        if not isinstance(raw, dict):
            return _failed(
                "plugin_importer_schema_invalid",
                "Plugin importer columns must be objects",
                field=f"response.columns[{index}]",
            )
        name = str(raw.get("name") or "").strip()
        type_name = canonical_column_type(str(raw.get("type") or "text"))
        if not name or name in seen:
            return _failed(
                "plugin_importer_schema_invalid",
                "Plugin importer column names must be unique",
                field=f"response.columns[{index}].name",
            )
        type_spec = column_types.get_column_type(type_name)
        if type_spec is None or not project_allows_plugin_column_type(
            project, type_spec
        ):
            return _failed(
                "plugin_importer_schema_invalid",
                "Plugin importer column type is not enabled for this project",
                field=f"response.columns[{index}].type",
                details={"type": type_name},
            )
        seen.add(name)
        column: dict[str, Any] = {"name": name, "type": type_name}
        if isinstance(raw.get("format"), str):
            column["format"] = raw["format"]
        if isinstance(raw.get("hidden"), bool):
            column["hidden"] = raw["hidden"]
        columns.append(column)
    return columns


def _parse_importer_row(
    raw_row: Any,
    *,
    project: Project | None,
    column_types_by_name: dict[str, str],
    row_number: int,
) -> dict[str, Any]:
    if not isinstance(raw_row, dict) or set(raw_row) != set(column_types_by_name):
        return {
            "_error": _failed(
                "plugin_importer_row_invalid",
                "Plugin importer row shape does not match its schema",
                field="response.row",
                details={"row": row_number},
            )
        }
    parsed: dict[str, Any] = {}
    for name, type_name in column_types_by_name.items():
        try:
            value = column_types.parse_value(type_name, raw_row[name])
        except Exception:
            return {
                "_error": _failed(
                    "plugin_importer_row_invalid",
                    "Plugin importer row value could not be parsed",
                    field=f"response.row.{name}",
                    details={
                        "diagnostics": [
                            {
                                "level": "error",
                                "code": "plugin_importer_row_value_invalid",
                                "message": "Imported row value could not be parsed",
                                "attach": {
                                    "kind": "imported_row",
                                    "row": row_number,
                                    "field": name,
                                },
                            }
                        ]
                    },
                )
            }
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            valid = False
        else:
            valid = column_types.validate_value(type_name, value)
        if not valid:
            return {
                "_error": _failed(
                    "plugin_importer_row_invalid",
                    "Plugin importer row value failed validation",
                    field=f"response.row.{name}",
                    details={
                        "diagnostics": [
                            {
                                "level": "error",
                                "code": "plugin_importer_row_value_invalid",
                                "message": "Imported row value failed validation",
                                "attach": {
                                    "kind": "imported_row",
                                    "row": row_number,
                                    "field": name,
                                },
                            }
                        ]
                    },
                )
            }
        try:
            value = validate_project_contextual_value(
                project,
                type_name=type_name,
                value=value,
            )
        except TimelineError as exc:
            return {
                "_error": _failed(
                    "plugin_importer_row_invalid",
                    "Plugin importer row temporal anchor failed validation",
                    field=f"response.row.{name}",
                    details={
                        "diagnostics": [
                            {
                                "level": "error",
                                "code": "plugin_importer_row_temporal_anchor_invalid",
                                "message": (
                                    "Imported row temporal anchor could not be "
                                    "resolved in the target project"
                                ),
                                "attach": {
                                    "kind": "imported_row",
                                    "row": row_number,
                                    "field": name,
                                    "reason": exc.code,
                                },
                            }
                        ]
                    },
                )
            }
        parsed[name] = value
    return parsed


def _sanitize_importer_diagnostics(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    diagnostics: list[dict[str, Any]] = []
    for item in raw:
        diagnostic = _sanitize_importer_diagnostic(item)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return diagnostics


def _sanitize_importer_diagnostic(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    level = str(raw.get("level") or "")
    if level not in {"warning", "error"}:
        return None
    code = str(raw.get("code") or "").strip()
    message = str(raw.get("message") or "").strip()
    if not code or not message:
        return None
    diagnostic: dict[str, Any] = {
        "level": level,
        "code": code[:80],
        "message": message[:500],
    }
    attach = raw.get("attach")
    if isinstance(attach, dict):
        clean_attach: dict[str, Any] = {}
        for key in ("kind", "line", "field", "object", "row"):
            value = attach.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                clean_attach[key] = value
        if clean_attach:
            diagnostic["attach"] = clean_attach
    return diagnostic

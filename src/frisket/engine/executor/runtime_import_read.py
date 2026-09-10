"""Project-admitted runtime importer reads, without project publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from itertools import islice
from typing import Any

from frisket.actions.import_runtime import RuntimeImportParams
from frisket.actions.types import (
    DynamicOutput,
    DynamicTableResult,
    RuntimeImportSource,
    TableColumn,
    TableError,
    TableRow,
)
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    project_runtime_binding,
    runtime_importer_dispatch_error,
)
from frisket.authoring.workbench.plugin_subprocess_importers import (
    _parse_importer_columns,
    _parse_importer_row,
    _sanitize_importer_diagnostics,
    run_plugin_importer_subprocess,
)
from frisket.contracts.action import ActionError
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.store import Project


class AdmittedRuntimeImporter:
    def __init__(
        self,
        project: Project,
        *,
        project_id: str,
        sheet_name: str,
        action_id: str,
        row_limit: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if row_limit is not None and (isinstance(row_limit, bool) or row_limit < 1):
            raise ValueError("runtime importer row_limit must be positive")
        self._project = project
        self._project_id = project_id
        self._sheet_name = sheet_name
        self._action_id = action_id
        self._resources = ExitStack()
        self._closed = False
        self._facts: list[dict[str, Any]] = []
        self._row_limit = row_limit
        self._cancelled = cancelled

    def _check_cancelled(self) -> None:
        if self._cancelled is not None and self._cancelled():
            raise TableError("action_cancelled", "Importer read was cancelled.")

    @property
    def facts(self) -> list[dict[str, Any]]:
        return deepcopy(self._facts)

    @property
    def source_summary(self) -> dict[str, Any] | None:
        if len(self._facts) != 1:
            return None
        fact = self._facts[0]
        return {
            "kind": "inline",
            "label": fact["declared_source"]["label"],
            "importer": fact["binding_kind"],
        }

    def close(self) -> None:
        self._closed = True
        self._resources.close()

    def _failed(self, plan: Mapping[str, Any]) -> None:
        errors = plan.get("errors")
        first = errors[0] if isinstance(errors, list) and errors else {}
        first = first if isinstance(first, dict) else {}
        raise TableReadRefused(
            ActionError(
                code=str(first.get("code") or "runtime_importer_handler_failed"),
                message=str(first.get("message") or "Trusted importer failed"),
                action_kind=self._action_id,
                field=str(first.get("field") or "params.importer_kind"),
                details=deepcopy(first.get("details"))
                if isinstance(first.get("details"), dict)
                else {},
            )
        )

    def read(
        self,
        importer_kind: str,
        *,
        source: RuntimeImportSource,
        handler_params: Mapping[str, Any],
    ) -> DynamicTableResult:
        if self._closed:
            raise ValueError("runtime importer is closed")
        if not isinstance(source, RuntimeImportSource) or not isinstance(
            handler_params, Mapping
        ):
            raise ValueError(
                "runtime importer requires typed source and JSON handler arguments"
            )
        actual = RuntimeImportParams(
            importer_kind=importer_kind,
            source=source,
            handler_params=dict(handler_params),
        )
        binding = project_runtime_binding(
            self._project, binding_type="importers", kind=actual.importer_kind
        )
        if binding is None:
            raise TableError(
                "unsupported_runtime_importer",
                "No trusted importer runtime binding exists for importer_kind",
            )
        error = runtime_importer_dispatch_error(
            self._project, binding, bounded=self._row_limit is not None
        )
        if error is not None:
            self._failed({"errors": [error]})
        self._check_cancelled()
        # Capture arguments before trusted handler code can mutate its payload.
        declared_source = actual.source.model_dump(mode="json")
        actual_handler_params = deepcopy(actual.handler_params)
        subprocess = binding.handler_api == "plugin_importer_subprocess"
        if subprocess:
            plan = run_plugin_importer_subprocess(
                self._project,
                source=deepcopy(declared_source),
                handler_params=deepcopy(actual_handler_params),
                project_id=self._project_id,
                binding=binding,
                row_limit=self._row_limit,
                cancelled=self._cancelled,
            )
        else:
            try:
                plan = binding.handler(
                    {
                        "schemaVersion": "frisket.runtime_importer_request.v1",
                        "projectId": self._project_id,
                        "pluginId": binding.plugin,
                        "handlerKey": binding.handler_key,
                        "importerKind": binding.kind,
                        "sheetName": self._sheet_name,
                        "source": deepcopy(declared_source),
                        "handlerParams": deepcopy(actual_handler_params),
                    }
                )
            except Exception as exc:
                raise TableError(
                    "runtime_importer_handler_failed", "Trusted importer handler failed"
                ) from exc
        if isinstance(plan, dict) and plan.get("status") == "failed":
            self._failed(plan)
        if not isinstance(plan, dict):
            raise TableError(
                "invalid_runtime_importer_plan", "Importer plan must be an object"
            )
        raw_rows = plan.get("rows")
        if callable(close := getattr(raw_rows, "close", None)):
            self._resources.callback(close)
        self._check_cancelled()
        if (
            set(plan)
            - {
                "schemaVersion",
                "columns",
                "rows",
                "warnings",
                "diagnostics",
                "streaming",
            }
            or plan.get("schemaVersion") != "frisket.runtime_importer_plan.v1"
            or (not subprocess and not isinstance(raw_rows, list))
        ):
            raise TableError(
                "invalid_runtime_importer_plan", "Importer returned an invalid plan"
            )
        columns = _parse_importer_columns(self._project, plan.get("columns"))
        if isinstance(columns, dict):
            self._failed(columns)
        schema = tuple(
            TableColumn(
                key=column["name"],
                type=column["type"],
                format=column.get("format"),
                hidden=column.get("hidden", False),
            )
            for column in columns
        )
        diagnostics = _sanitize_importer_diagnostics(plan.get("diagnostics"))
        warnings = plan.get("warnings", [])
        if not isinstance(warnings, list) or any(
            not isinstance(item, str) for item in warnings
        ):
            raise TableError(
                "invalid_runtime_importer_plan", "Importer warnings must be strings"
            )
        fact = {
            "kind": "workbench_runtime_binding",
            "schema_version": "frisket.workbench_runtime_binding_evidence.v1",
            "binding_type": "importers",
            "binding_kind": binding.kind,
            "plugin": binding.plugin,
            "handler_key": binding.handler_key,
            "declared_source": deepcopy(declared_source),
            "source_fingerprint_verified": False,
            "handler_params_sha256": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    actual_handler_params, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "row_count": 0,
            "diagnostics": deepcopy(diagnostics),
        }
        if subprocess:
            # Only our frame adapter constructs these observations. An in-process
            # handler's optional streaming claims are not host measurements.
            fact["streaming"] = deepcopy(plan["streaming"])
        self._facts.append(fact)

        def rows():
            # Lists may have been computed eagerly by trusted code. Bound host
            # consumption, without promising to interrupt that precomputation.
            for number, raw in enumerate(islice(raw_rows, self._row_limit), start=1):
                self._check_cancelled()
                if not subprocess:
                    raw = _parse_importer_row(
                        raw,
                        project=self._project,
                        column_types_by_name={
                            column.key: column.type for column in schema
                        },
                        row_number=number,
                    )
                    if "_error" in raw:
                        self._failed(raw["_error"])
                fact["row_count"] = number
                yield TableRow(output=DynamicOutput(raw))

        return DynamicTableResult(schema=schema, rows=rows(), warnings=tuple(warnings))

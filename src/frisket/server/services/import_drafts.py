"""Concrete server-owned analysis and execution for pasted rows."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from typing import Any, Sequence

from frisket.actions.imports import ImportRowsParams, UpdateRowsParams
from frisket.csv_values import parse_csv_value
from frisket.engine.executor import ExecutorDeps
from frisket.engine.store.artifact_timeline import TimelineError
from frisket.server.route_errors import RouteError
from frisket.server.services.action_runs import v1_action_result_http_status
from frisket.server.services.import_inference import infer_column_type, sniff_markdown
from frisket.server.services.import_tabular import (
    decode_destination_mapping,
    materialized_sheet_output,
    mutation_row_count,
    plan_tabular_update,
    run_tabular_action,
    update_preview_payload,
    workload_limit_result,
)
from frisket.server.workspace import Workspace


class ImportDraftRouteError(RouteError):
    pass


@dataclass(frozen=True)
class PastedRowsAnalysis:
    normalized: str
    headers: list[str]
    rows: list[dict[str, str]]
    columns: list[dict[str, Any]]
    warnings: list[str]
    draft_id: str
    fingerprint: str
    line_count: int


@dataclass(frozen=True)
class ImportDraftConfirmResponse:
    status_code: int
    payload: dict[str, Any]
    force_json_response: bool = False


def _unique_import_column_names(headers: list[str]) -> list[str]:
    used: set[str] = set()
    names: list[str] = []
    for index, header in enumerate(headers, start=1):
        base = str(header or "").strip() or f"column_{index}"
        candidate, suffix = base, 1
        while candidate in used:
            suffix += 1
            candidate = f"{base}_{suffix}"
        used.add(candidate)
        names.append(candidate)
    return names


def _paste_delimiter(normalized: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(normalized[:4096], delimiters=",\t")
        if dialect.delimiter in {",", "\t"}:
            return dialect.delimiter
    except csv.Error:
        pass
    return "\t" if "\t" in normalized.split("\n", 1)[0] else ","


def analyze_pasted_rows(raw: str) -> PastedRowsAnalysis:
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    headers: list[str] = []
    records: list[dict[str, str]] = []
    warnings: list[str] = []
    header_values: list[str] | None = None
    for line_number, values in enumerate(
        csv.reader(io.StringIO(normalized), delimiter=_paste_delimiter(normalized)),
        start=1,
    ):
        if header_values is None:
            if not any(str(value).strip() for value in values):
                continue
            header_values = [str(value) for value in values]
            headers = _unique_import_column_names(header_values)
            continue
        if not any(str(value).strip() for value in values):
            continue
        if len(values) > len(headers):
            warnings.append(
                f"Row {line_number} has {len(values) - len(headers)} extra value(s); extra values were ignored."
            )
        records.append(
            {
                name: str(values[index]).strip() if index < len(values) else ""
                for index, name in enumerate(headers)
            }
        )
    if header_values is None:
        raise ValueError("paste at least one header row and one data row")
    if not records:
        raise ValueError("paste at least one data row")
    columns: list[dict[str, Any]] = []
    for name in headers:
        samples = [record[name] for record in records[:50]]
        kind = infer_column_type(name, samples)
        column: dict[str, Any] = {
            "key": name,
            "name": name,
            "type": kind,
            "include": True,
            "sample_values": [value for value in samples[:5] if value],
        }
        if kind == "text" and sniff_markdown(samples):
            column["format"] = "markdown"
        columns.append(column)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return PastedRowsAnalysis(
        normalized,
        headers,
        records,
        columns,
        warnings,
        f"paste@sha256:{digest}",
        f"sha256:{digest}",
        len(normalized.splitlines()) or 1,
    )


def _reviewed_entries(
    reviewed_columns: Sequence[Any], headers: Sequence[str]
) -> list[dict[str, Any]]:
    entries = [
        entry.model_dump(exclude_unset=True)
        if hasattr(entry, "model_dump")
        else dict(entry)
        for entry in reviewed_columns
    ]
    names = [entry.get("source_name") for entry in entries]
    if (
        len(entries) != len(headers)
        or set(names) != set(headers)
        or len(names) != len(set(names))
    ):
        raise ValueError("import mapping must review every source column")
    for entry in entries:
        if not isinstance(entry.get("name"), str) and entry.get("name") is not None:
            raise ValueError("import mapping is invalid")
    return entries


def prepare_pasted_rows(
    analysis: PastedRowsAnalysis,
    reviewed_columns: Sequence[Any],
    *,
    project: Any | None = None,
    destination_sheet_id: int | None = None,
    key_columns: Sequence[str] | None = None,
    keep_existing_on_blank: bool = False,
) -> ImportRowsParams | UpdateRowsParams:
    reviewed = _reviewed_entries(reviewed_columns, analysis.headers)
    if destination_sheet_id is None:
        selected = [entry for entry in reviewed if entry["name"] is not None]
        columns = [
            {
                key: value
                for key, value in entry.items()
                if key in {"name", "source_name", "type", "format"}
            }
            for entry in selected
        ]
        target_by_source = {entry["source_name"]: entry["name"] for entry in selected}
        types = {entry["source_name"]: entry["type"] for entry in selected}
    else:
        if project is None:
            raise ValueError("destination mapping needs a project")
        mapping = decode_destination_mapping(
            project,
            sheet_id=destination_sheet_id,
            source_names=analysis.headers,
            column_mapping={entry["source_name"]: entry["name"] for entry in reviewed},
            key_columns=key_columns,
        )
        columns = [column.model_dump(exclude_unset=True) for column in mapping.columns]
        target_by_source = {
            column.source_name or column.name: column.name for column in mapping.columns
        }
        types = {
            column.source_name or column.name: column.type for column in mapping.columns
        }
    rows = []
    for record in analysis.rows:
        row = {
            target: parse_csv_value(record[source], types[source])
            for source, target in target_by_source.items()
        }
        rows.append(row)
    source = {
        "kind": "inline",
        "label": "pasted table",
        "fingerprint": analysis.fingerprint,
        "line_count": analysis.line_count,
    }
    values: dict[str, Any] = {"columns": columns, "rows": rows, "source": source}
    if destination_sheet_id is not None and key_columns is not None:
        values.update(
            key_columns=list(key_columns), keep_existing_on_blank=keep_existing_on_blank
        )
        return UpdateRowsParams.model_validate(values)
    return ImportRowsParams.model_validate(values)


class ImportDraftService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def executor_deps_for_request(self, project_id: str, request: Any) -> ExecutorDeps:
        factory = self._workspace.executor_deps_factory
        return (
            factory(project_id, request) if factory is not None else None
        ) or ExecutorDeps()

    def paste_draft(self, project_id: str, raw: str) -> dict[str, Any]:
        self._workspace.get(project_id)
        try:
            analysis = analyze_pasted_rows(raw)
        except ValueError as exc:
            raise ImportDraftRouteError(400, str(exc)) from exc
        return {
            "schema_version": "frisket.import_draft.v2",
            "draft_id": analysis.draft_id,
            "source_kind": "paste",
            "sheet_name": "Pasted rows",
            "row_count": len(analysis.rows),
            "columns": analysis.columns,
            "preview_rows": analysis.rows[:20],
            "warnings": analysis.warnings,
            "source": {
                "kind": "inline",
                "label": "pasted table",
                "fingerprint": analysis.fingerprint,
                "line_count": analysis.line_count,
            },
        }

    def _prepare(
        self, project_id: str, body: Any
    ) -> tuple[Any, PastedRowsAnalysis, Any]:
        project = self._workspace.get(project_id)
        try:
            analysis = analyze_pasted_rows(body.raw)
            if analysis.draft_id != body.draft_id:
                raise ImportDraftRouteError(409, "stale import draft")
            params = prepare_pasted_rows(
                analysis,
                body.columns,
                project=project,
                destination_sheet_id=body.destination_sheet_id,
                key_columns=body.key_columns,
                keep_existing_on_blank=body.keep_existing_on_blank,
            )
        except ImportDraftRouteError:
            raise
        except (ValueError, TimelineError) as exc:
            raise ImportDraftRouteError(400, str(exc)) from exc
        return project, analysis, params

    def preview_update(
        self, project_id: str, body: Any, *, deps: ExecutorDeps | None = None
    ) -> dict[str, Any]:
        project, _analysis, params = self._prepare(project_id, body)
        if (
            not isinstance(params, UpdateRowsParams)
            or body.destination_sheet_id is None
        ):
            raise ImportDraftRouteError(
                400, "paste update preview requires destination and key columns"
            )
        try:
            max_rows = (
                deps.import_workload_limits.max_rows
                if deps and deps.import_workload_limits
                else None
            )
            plan = plan_tabular_update(
                project,
                sheet_id=body.destination_sheet_id,
                columns=params.columns,
                key_columns=params.key_columns,
                rows=params.rows,
                keep_existing_on_blank=params.keep_existing_on_blank,
                max_rows=max_rows,
            )
        except OverflowError as exc:
            raise ImportDraftRouteError(
                400,
                f"import.update_rows row count exceeds the deployment limit of {max_rows}",
            ) from exc
        except (LookupError, TimelineError, ValueError) as exc:
            raise ImportDraftRouteError(400, str(exc)) from exc
        return update_preview_payload(plan)

    def confirm(
        self, project_id: str, body: Any, *, deps: ExecutorDeps | None = None
    ) -> ImportDraftConfirmResponse:
        project, analysis, params = self._prepare(project_id, body)
        executor_deps = deps or ExecutorDeps()
        update = body.destination_sheet_id is not None and body.key_columns is not None
        if body.destination_sheet_id is None and body.key_columns is not None:
            raise ImportDraftRouteError(400, "key columns require a destination sheet")
        if update and not body.confirmation:
            raise ImportDraftRouteError(400, "import update requires confirmation")
        if not update and body.confirmation is not None:
            raise ImportDraftRouteError(400, "confirmation requires an update")
        max_rows = (
            executor_deps.import_workload_limits.max_rows
            if executor_deps.import_workload_limits
            else None
        )
        action_kind = (
            "import.update_rows"
            if update
            else "import.append_rows"
            if body.destination_sheet_id is not None
            else "import.rows"
        )
        if max_rows is not None and len(analysis.rows) > max_rows:
            result = workload_limit_result(
                project_id=project_id,
                action_kind=action_kind,
                row_count=len(analysis.rows),
                max_rows=max_rows,
            )
            return ImportDraftConfirmResponse(
                v1_action_result_http_status(result),
                result.model_dump(mode="json"),
                True,
            )
        canonical = {
            "draft_id": analysis.draft_id,
            "sheet_name": body.sheet_name,
            "destination_sheet_id": body.destination_sheet_id,
            "columns": [
                column.model_dump(mode="json", exclude_unset=True)
                for column in params.columns
            ],
            "key_columns": list(params.key_columns)
            if isinstance(params, UpdateRowsParams)
            else [],
            "keep_existing_on_blank": body.keep_existing_on_blank,
        }
        key = (
            "paste@sha256:"
            + hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        result = run_tabular_action(
            project,
            project_id=project_id,
            params=params,
            idempotency_key=key,
            deps=executor_deps,
            sheet_name=body.sheet_name or "Pasted rows",
            destination_sheet_id=body.destination_sheet_id,
            confirmation=body.confirmation,
            router=self._workspace.router_for(project),
        )
        if result.status != "completed":
            return ImportDraftConfirmResponse(
                v1_action_result_http_status(result),
                result.model_dump(mode="json"),
                True,
            )
        if body.destination_sheet_id is None:
            output = materialized_sheet_output(result)
            if (
                output is None
                or output.sheet_id is None
                or not isinstance(output.ref.get("row_count"), int)
            ):
                raise ImportDraftRouteError(
                    500, "import.rows did not return a sheet output"
                )
            return ImportDraftConfirmResponse(
                200,
                {
                    "sheet_id": output.sheet_id,
                    "rows": output.ref["row_count"],
                    "columns": [column.name for column in params.columns],
                },
            )
        rows = mutation_row_count(
            result, output_name="updated_rows" if update else "appended_rows"
        )
        if rows is None:
            raise ImportDraftRouteError(500, "import.rows did not return a row count")
        return ImportDraftConfirmResponse(
            200,
            {
                "sheet_id": body.destination_sheet_id,
                "rows": rows,
                "columns": [column.name for column in params.columns],
            },
        )

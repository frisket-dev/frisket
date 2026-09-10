"""XLSX upload service over one borrowed, admitted request source."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, BinaryIO

from openpyxl import load_workbook

from frisket.actions.import_xlsx import ImportXlsxParams, UpdateXlsxParams, import_xlsx
from frisket.actions.types import TableError
from frisket.engine.executor import BoundLocalFile, ExecutorDeps
from frisket.engine.executor.import_sources import (
    bind_import_sources,
    open_local_file_reader,
)
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
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.workspace import Workspace


@dataclass(frozen=True)
class ImportXlsxUploadResponse:
    status_code: int
    payload: dict[str, Any]
    force_json_response: bool = False


class ImportXlsxRouteError(RouteError):
    pass


@dataclass(frozen=True)
class XlsxFileScan:
    source: BinaryIO
    logical_path: str
    headers: list[str]
    sample_records: list[dict[str, str]]
    row_count: int
    columns: list[dict[str, Any]]


def _source_path(upload: AdmittedUpload) -> str:
    return f"admitted://xlsx/{upload.sha256}/{upload.filename}"


def scan_xlsx_stream(source: BinaryIO, *, logical_path: str) -> XlsxFileScan:
    """Inspect the first worksheet while leaving the caller's source rewound."""

    try:
        source.seek(0)
        workbook = load_workbook(source, read_only=True, data_only=True)
        try:
            sheet = workbook.active
            if sheet is None:
                raise ImportXlsxRouteError(400, "workbook has no worksheet")
            rows = sheet.iter_rows(values_only=True)
            header = next(rows, None)
            if header is None:
                raise ImportXlsxRouteError(400, "empty xlsx")
            headers = [
                str(value) if value is not None else f"col{index}"
                for index, value in enumerate(header)
            ]
            if not headers or len(headers) != len(set(headers)):
                raise ImportXlsxRouteError(400, "XLSX headers must be unique")
            samples: list[dict[str, str]] = []
            row_count = 0
            for row in rows:
                if row is None or all(value is None for value in row):
                    continue
                record = {
                    headers[index]: _xlsx_cell(row[index]) if index < len(row) else ""
                    for index in range(len(headers))
                }
                row_count += 1
                if len(samples) < 50:
                    samples.append(record)
            if row_count == 0:
                raise ImportXlsxRouteError(400, "empty xlsx")
        finally:
            workbook.close()
    except ImportXlsxRouteError:
        raise
    except Exception as exc:  # parser errors are user input failures
        raise ImportXlsxRouteError(400, f"invalid xlsx: {exc}") from exc
    finally:
        source.seek(0)
    columns = []
    for name in headers:
        values = [record.get(name) for record in samples]
        kind = infer_column_type(name, values)
        column: dict[str, Any] = {"name": name, "type": kind}
        if kind == "text" and sniff_markdown(values):
            column["format"] = "markdown"
        columns.append(column)
    return XlsxFileScan(source, logical_path, headers, samples, row_count, columns)


def _xlsx_params(
    scan: XlsxFileScan,
    *,
    source_path: str,
    columns: list[dict[str, Any]] | None = None,
    key_columns: tuple[str, ...] | None = None,
    keep_existing_on_blank: bool = False,
) -> ImportXlsxParams | UpdateXlsxParams:
    values: dict[str, Any] = {
        "source": {"kind": "file", "path": source_path, "label": scan.logical_path},
        "columns": columns or scan.columns,
        "header": "present",
    }
    if key_columns is not None:
        values.update(
            key_columns=list(key_columns), keep_existing_on_blank=keep_existing_on_blank
        )
        return UpdateXlsxParams.model_validate(values)
    return ImportXlsxParams.model_validate(values)


class ImportXlsxUploadService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def executor_deps_for_request(self, project_id: str, request: Any) -> ExecutorDeps:
        factory = self._workspace.executor_deps_factory
        return (
            factory(project_id, request) if factory is not None else None
        ) or ExecutorDeps()

    def _scan(self, upload: AdmittedUpload) -> XlsxFileScan:
        return scan_xlsx_stream(
            upload.source, logical_path=upload.filename or "imported.xlsx"
        )

    def preview_xlsx(
        self, project_id: str, *, upload: AdmittedUpload
    ) -> dict[str, Any]:
        self._workspace.get(project_id)
        scan = self._scan(upload)
        preview_rows: list[dict[str, Any]] = []
        with open_local_file_reader(
            {
                _source_path(upload): BoundLocalFile(
                    upload.source, f"sha256:{upload.sha256}"
                )
            }
        ) as reader:
            params = _xlsx_params(scan, source_path=_source_path(upload))
            assert isinstance(params, ImportXlsxParams)
            for row in import_xlsx(params, reader).rows:
                if len(preview_rows) >= 20:
                    break
                preview_rows.append(row.output.root)
        return {
            "encoding": "binary",
            "delimiter": "worksheet",
            "decimal_separator": ".",
            "row_count": scan.row_count,
            "columns": scan.columns,
            "preview_rows": preview_rows,
            "truncated": scan.row_count > len(preview_rows),
        }

    def preview_xlsx_update(
        self,
        project_id: str,
        *,
        upload: AdmittedUpload,
        destination_sheet_id: int,
        column_mapping: str,
        key_columns: str,
        keep_existing_on_blank: bool,
        deps: ExecutorDeps | None = None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        scan = self._scan(upload)
        try:
            mapping = decode_destination_mapping(
                project,
                sheet_id=destination_sheet_id,
                source_names=scan.headers,
                column_mapping=column_mapping,
                key_columns=key_columns,
            )
            params = _xlsx_params(
                scan,
                source_path=_source_path(upload),
                columns=[
                    column.model_dump(exclude_unset=True) for column in mapping.columns
                ],
                key_columns=mapping.key_columns,
                keep_existing_on_blank=keep_existing_on_blank,
            )
            assert isinstance(params, UpdateXlsxParams)
            with open_local_file_reader(
                {
                    _source_path(upload): BoundLocalFile(
                        upload.source, f"sha256:{upload.sha256}"
                    )
                }
            ) as reader:
                produced = import_xlsx(params, reader)
                max_rows = (
                    deps.import_workload_limits.max_rows
                    if deps and deps.import_workload_limits
                    else None
                )
                plan = plan_tabular_update(
                    project,
                    sheet_id=destination_sheet_id,
                    columns=params.columns,
                    key_columns=params.key_columns,
                    rows=(row.output.root for row in produced.rows),
                    keep_existing_on_blank=keep_existing_on_blank,
                    max_rows=max_rows,
                )
        except OverflowError as exc:
            max_rows = (
                deps.import_workload_limits.max_rows
                if deps and deps.import_workload_limits
                else None
            )
            raise ImportXlsxRouteError(
                400,
                f"import.update_xlsx row count exceeds the deployment limit of {max_rows}",
            ) from exc
        except (TableError, LookupError, TimelineError, ValueError) as exc:
            raise ImportXlsxRouteError(400, str(exc)) from exc
        return update_preview_payload(plan)

    def upload_xlsx(
        self,
        project_id: str,
        *,
        upload: AdmittedUpload,
        sheet_name: str | None,
        deps: ExecutorDeps | None = None,
        destination_sheet_id: int | None = None,
        append_request_key: str | None = None,
        column_mapping: str | None = None,
        update_key_columns: str | None = None,
        keep_existing_on_blank: bool = False,
        confirmation: str | None = None,
    ) -> ImportXlsxUploadResponse:
        project = self._workspace.get(project_id)
        executor_deps = deps or ExecutorDeps()
        scan = self._scan(upload)
        update = update_key_columns is not None
        action_kind = (
            "import.update_xlsx"
            if update
            else "import.append_xlsx"
            if destination_sheet_id is not None
            else "import.xlsx"
        )
        max_rows = (
            executor_deps.import_workload_limits.max_rows
            if executor_deps.import_workload_limits
            else None
        )
        if max_rows is not None and scan.row_count > max_rows:
            result = workload_limit_result(
                project_id=project_id,
                action_kind=action_kind,
                row_count=scan.row_count,
                max_rows=max_rows,
            )
            return ImportXlsxUploadResponse(
                v1_action_result_http_status(result),
                result.model_dump(mode="json"),
                True,
            )
        name = sheet_name or upload.filename.rsplit(".", 1)[0]
        if destination_sheet_id is not None:
            if not append_request_key:
                raise ImportXlsxRouteError(400, "XLSX append requires a request key")
            request_key = (
                "xlsx-update:" if update else "xlsx-append:"
            ) + append_request_key
            try:
                mapping = decode_destination_mapping(
                    project,
                    sheet_id=destination_sheet_id,
                    source_names=scan.headers,
                    column_mapping=column_mapping or "",
                    key_columns=update_key_columns,
                )
            except ValueError as exc:
                raise ImportXlsxRouteError(400, str(exc)) from exc
            columns = [
                column.model_dump(exclude_unset=True) for column in mapping.columns
            ]
            keys = mapping.key_columns if update else None
        else:
            basis = json.dumps(
                {
                    "filename": upload.filename,
                    "sha256": upload.sha256,
                    "sheet_name": name,
                    "columns": scan.columns,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            request_key = (
                "http-import-xlsx@sha256:" + hashlib.sha256(basis.encode()).hexdigest()
            )
            columns, keys = None, None
        params = _xlsx_params(
            scan,
            source_path=_source_path(upload),
            columns=columns,
            key_columns=keys,
            keep_existing_on_blank=keep_existing_on_blank,
        )
        result = run_tabular_action(
            project,
            project_id=project_id,
            params=params,
            idempotency_key=request_key,
            deps=bind_import_sources(
                executor_deps,
                local_files={
                    _source_path(upload): BoundLocalFile(
                        upload.source, f"sha256:{upload.sha256}"
                    )
                },
            ),
            sheet_name=name if destination_sheet_id is None else None,
            destination_sheet_id=destination_sheet_id,
            confirmation=confirmation,
            router=self._workspace.router_for(project),
        )
        if result.status != "completed":
            return ImportXlsxUploadResponse(
                v1_action_result_http_status(result),
                result.model_dump(mode="json"),
                True,
            )
        if destination_sheet_id is None:
            output = materialized_sheet_output(result)
            if (
                output is None
                or output.sheet_id is None
                or not isinstance(output.ref.get("row_count"), int)
            ):
                raise ImportXlsxRouteError(
                    500, "import.xlsx did not return a sheet output"
                )
            sheet_id, rows = output.sheet_id, output.ref["row_count"]
        else:
            rows = mutation_row_count(
                result, output_name="updated_rows" if update else "appended_rows"
            )
            if rows is None:
                raise ImportXlsxRouteError(
                    500, "import.xlsx did not return a row count"
                )
            sheet_id = destination_sheet_id
        return ImportXlsxUploadResponse(
            200, {"sheet_id": sheet_id, "rows": rows, "columns": scan.headers}
        )


def _xlsx_cell(value: Any) -> str:
    return "" if value is None else str(value)

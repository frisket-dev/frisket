"""CSV upload service over one borrowed, admitted request source."""

from __future__ import annotations

import hashlib
import csv
import io
import json
from dataclasses import dataclass
from typing import Any

from frisket.actions.imports import UpdateCsvParams, import_csv
from frisket.actions.types import TableError
from frisket.engine.executor import BoundLocalFile, ExecutorDeps
from frisket.engine.executor.import_sources import open_local_file_reader
from frisket.engine.store.artifact_timeline import TimelineError
from frisket.server.route_errors import RouteError
from frisket.server.services import import_csv_analysis, import_csv_execute
from frisket.server.services.action_runs import v1_action_result_http_status
from frisket.server.services.import_tabular import (
    decode_destination_mapping,
    materialized_sheet_output,
    mutation_row_count,
    plan_tabular_update,
    update_preview_payload,
    workload_limit_result,
)
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.workspace import Workspace


@dataclass(frozen=True)
class ImportCsvUploadResponse:
    status_code: int
    payload: dict[str, Any]
    force_json_response: bool = False


class ImportCsvRouteError(RouteError):
    pass


def _source_path(upload: AdmittedUpload) -> str:
    return f"admitted://csv/{upload.sha256}/{upload.filename}"


def _request_key(
    upload: AdmittedUpload,
    scan: import_csv_analysis.CsvFileScan,
    *,
    sheet_name: str,
    encoding: str | None,
    force_text_columns: bool,
) -> str:
    basis = json.dumps(
        {
            "sha256": upload.sha256,
            "filename": upload.filename,
            "sheet_name": sheet_name,
            "explicit_encoding": encoding,
            "source_encoding": scan.source_encoding,
            "delimiter": scan.delimiter,
            "decimal_separator": scan.decimal_separator,
            "headers": scan.fieldnames,
            "force_text_columns": force_text_columns,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "direct:" + hashlib.sha256(basis.encode()).hexdigest()


def _preview_rows(
    source: Any, scan: import_csv_analysis.CsvFileScan
) -> list[dict[str, str]]:
    """Project lexical CSV values for the preview without buffering the upload."""

    source.seek(0)
    wrapper = io.TextIOWrapper(source, encoding=scan.source_encoding, newline="")
    try:
        reader = csv.DictReader(
            wrapper, fieldnames=scan.fieldnames, delimiter=scan.delimiter
        )
        next(reader, None)
        return [
            {name: value or "" for name, value in row.items() if name is not None}
            for _, row in zip(range(20), reader, strict=False)
        ]
    finally:
        wrapper.detach()
        source.seek(0)


class ImportCsvUploadService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def executor_deps_for_request(self, project_id: str, request: Any) -> ExecutorDeps:
        factory = self._workspace.executor_deps_factory
        return (
            factory(project_id, request) if factory is not None else None
        ) or ExecutorDeps()

    def _scan(
        self,
        upload: AdmittedUpload,
        *,
        encoding: str | None,
        force_text_columns: bool = False,
    ) -> import_csv_analysis.CsvFileScan:
        upload.source.seek(0)
        scan = import_csv_analysis.scan_csv_stream(
            upload.source,
            logical_path=upload.filename or "imported.csv",
            encoding=encoding,
            force_text_columns=force_text_columns,
        )
        upload.source.seek(0)
        return scan

    def preview_csv(
        self, project_id: str, *, upload: AdmittedUpload, encoding: str | None = None
    ) -> dict[str, Any]:
        self._workspace.get(project_id)
        scan = self._scan(upload, encoding=encoding)
        preview_rows = _preview_rows(upload.source, scan)
        return {
            "encoding": scan.source_encoding,
            "delimiter": scan.delimiter,
            "decimal_separator": scan.decimal_separator,
            "row_count": scan.row_count,
            "columns": scan.columns,
            "preview_rows": preview_rows,
            "truncated": scan.row_count > len(preview_rows),
        }

    def preview_csv_update(
        self,
        project_id: str,
        *,
        upload: AdmittedUpload,
        destination_sheet_id: int,
        column_mapping: str,
        key_columns: str,
        keep_existing_on_blank: bool,
        encoding: str | None = None,
        deps: ExecutorDeps | None = None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        scan = self._scan(upload, encoding=encoding)
        try:
            mapping = decode_destination_mapping(
                project,
                sheet_id=destination_sheet_id,
                source_names=scan.fieldnames,
                column_mapping=column_mapping,
                key_columns=key_columns,
            )
            params = import_csv_execute.params_from_scans(
                [scan],
                source_paths=[_source_path(upload)],
                columns_override=[
                    column.model_dump(exclude_unset=True) for column in mapping.columns
                ],
                key_columns=mapping.key_columns,
                keep_existing_on_blank=keep_existing_on_blank,
            )
            assert isinstance(params, UpdateCsvParams)
            with open_local_file_reader(
                {
                    _source_path(upload): BoundLocalFile(
                        upload.source, f"sha256:{upload.sha256}"
                    )
                }
            ) as reader:
                produced = import_csv(params, reader)
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
            raise ImportCsvRouteError(
                400,
                f"import.update_csv row count exceeds the deployment limit of {max_rows}",
            ) from exc
        except (TableError, LookupError, TimelineError, ValueError) as exc:
            raise ImportCsvRouteError(400, str(exc)) from exc
        return update_preview_payload(plan)

    def upload_csv(
        self,
        project_id: str,
        *,
        upload: AdmittedUpload,
        sheet_name: str | None,
        encoding: str | None = None,
        force_text_columns: bool = False,
        destination_sheet_id: int | None = None,
        append_request_key: str | None = None,
        column_mapping: str | None = None,
        update_key_columns: str | None = None,
        keep_existing_on_blank: bool = False,
        confirmation: str | None = None,
        deps: ExecutorDeps | None = None,
    ) -> ImportCsvUploadResponse:
        project = self._workspace.get(project_id)
        executor_deps = deps or ExecutorDeps()
        scan = self._scan(
            upload, encoding=encoding, force_text_columns=force_text_columns
        )
        update = update_key_columns is not None
        action_kind = (
            "import.update_csv"
            if update
            else "import.append_csv"
            if destination_sheet_id is not None
            else "import.csv"
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
            return ImportCsvUploadResponse(
                v1_action_result_http_status(result),
                result.model_dump(mode="json"),
                True,
            )
        name = sheet_name or upload.filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if destination_sheet_id is not None:
            if not append_request_key:
                raise ImportCsvRouteError(400, "CSV append requires a request key")
            request_key = (
                "csv-update:" if update else "csv-append:"
            ) + append_request_key
            try:
                mapping = decode_destination_mapping(
                    project,
                    sheet_id=destination_sheet_id,
                    source_names=scan.fieldnames,
                    column_mapping=column_mapping or "",
                    key_columns=update_key_columns,
                )
            except ValueError as exc:
                raise ImportCsvRouteError(400, str(exc)) from exc
            columns = [
                column.model_dump(exclude_unset=True) for column in mapping.columns
            ]
            keys = mapping.key_columns if update else None
        else:
            request_key = _request_key(
                upload,
                scan,
                sheet_name=name,
                encoding=encoding,
                force_text_columns=force_text_columns,
            )
            columns, keys = None, None
        result = import_csv_execute.run_scanned_csv_import(
            project,
            project_id=project_id,
            sheet_name=name if destination_sheet_id is None else None,
            scans=[scan],
            source_paths=[_source_path(upload)],
            source_sha256=[upload.sha256],
            source_column=None,
            request_key=request_key,
            deps=executor_deps,
            columns_override=columns,
            destination_sheet_id=destination_sheet_id,
            key_columns=keys,
            keep_existing_on_blank=keep_existing_on_blank,
            confirmation=confirmation,
            router=self._workspace.router_for(project),
        )
        if result.status != "completed":
            return ImportCsvUploadResponse(
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
                raise ImportCsvRouteError(
                    500, "CSV import did not return a sheet output"
                )
            sheet_id, rows = output.sheet_id, output.ref["row_count"]
        else:
            rows = mutation_row_count(
                result, output_name="updated_rows" if update else "appended_rows"
            )
            if rows is None:
                raise ImportCsvRouteError(500, "CSV import did not return a row count")
            sheet_id = destination_sheet_id
        return ImportCsvUploadResponse(
            200,
            {
                "sheet_id": sheet_id,
                "rows": rows,
                "columns": scan.fieldnames,
                "encoding": scan.source_encoding,
            },
        )

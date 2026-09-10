"""CSV parameter construction from caller-owned admitted scans."""

from __future__ import annotations

from typing import Any, Sequence

from frisket.actions.imports import ImportCsvParams, UpdateCsvParams
from frisket.engine.executor import BoundLocalFile, ExecutorDeps
from frisket.engine.executor.import_sources import bind_import_sources
from frisket.server.services.import_csv_analysis import CsvFileScan, widen_csv_scans
from frisket.server.services.import_tabular import run_tabular_action


def params_from_scans(
    scans: Sequence[CsvFileScan],
    *,
    source_paths: Sequence[str],
    source_label_column: str | None = None,
    columns_override: Sequence[dict[str, Any]] | None = None,
    key_columns: Sequence[str] | None = None,
    keep_existing_on_blank: bool = False,
) -> ImportCsvParams | UpdateCsvParams:
    """Keep CSV's source metadata, widening, and optional source label concrete."""

    columns = [
        dict(column) for column in (columns_override or widen_csv_scans(list(scans)))
    ]
    if source_label_column is not None:
        columns.append({"name": source_label_column, "type": "text"})
    sources = [
        {
            "path": path,
            "label": scan.logical_path,
            "encoding": scan.source_encoding,
            "delimiter": scan.delimiter,
            "decimal_separator": scan.decimal_separator,
            "headers": scan.fieldnames,
        }
        for scan, path in zip(scans, source_paths, strict=True)
    ]
    values: dict[str, Any] = {
        "sources": sources,
        "columns": columns,
        "source_label_column": source_label_column,
    }
    if key_columns is not None:
        values.update(
            key_columns=list(key_columns), keep_existing_on_blank=keep_existing_on_blank
        )
        return UpdateCsvParams.model_validate(values)
    return ImportCsvParams.model_validate(values)


def run_scanned_csv_import(
    project: Any,
    *,
    project_id: str,
    sheet_name: str | None,
    scans: Sequence[CsvFileScan],
    source_paths: Sequence[str],
    source_sha256: Sequence[str],
    source_column: str | None,
    request_key: str,
    deps: ExecutorDeps,
    columns_override: Sequence[dict[str, Any]] | None = None,
    destination_sheet_id: int | None = None,
    key_columns: Sequence[str] | None = None,
    keep_existing_on_blank: bool = False,
    confirmation: str | None = None,
    router: Any | None = None,
):
    """Bind borrowed scans and delegate action envelope policy to the common runner."""

    params = params_from_scans(
        scans,
        source_paths=source_paths,
        source_label_column=source_column,
        columns_override=columns_override,
        key_columns=key_columns,
        keep_existing_on_blank=keep_existing_on_blank,
    )
    files = {
        path: BoundLocalFile(stream=scan.source, sha256=f"sha256:{digest}")
        for scan, path, digest in zip(scans, source_paths, source_sha256, strict=True)
    }
    return run_tabular_action(
        project,
        project_id=project_id,
        params=params,
        idempotency_key=request_key,
        deps=bind_import_sources(deps, local_files=files),
        sheet_name=sheet_name,
        destination_sheet_id=destination_sheet_id,
        confirmation=confirmation,
        router=router,
    )

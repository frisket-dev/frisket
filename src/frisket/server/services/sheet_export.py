"""Sheet export services."""

from __future__ import annotations

import csv
import io
import json
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from openpyxl import Workbook

from frisket.server.exports.attachments import ExportAttachments
from frisket.server.exports.plan import (
    AttachmentPath,
    SheetExportPlan,
    build_sheet_export_plan,
    iter_export_row_batches,
)
from frisket.server.exports.rowset import ExportError
from frisket.server.exports.sheet_csv import (
    SheetExportLimits,
    csv_display,
    iter_export_csv_bytes,
    iter_export_csv_rows,
)
from frisket.server.downloads import download_filename
from frisket.server.workspace import Workspace


@dataclass(frozen=True)
class SheetDatasetArtifact:
    filename: str
    content: bytes
    media_type: str


@dataclass(frozen=True)
class SheetDatasetFile:
    filename: str
    path: Path
    media_type: str
    close: Callable[[], None]


@dataclass(frozen=True)
class SheetDatasetStream:
    filename: str
    chunks: Iterator[bytes]
    media_type: str
    close: Callable[[], None]


class SheetDatasetExportError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


class SheetDatasetExportService:
    def __init__(
        self, workspace: Workspace, *, limits: SheetExportLimits | None = None
    ):
        self._workspace = workspace
        self._limits = limits or SheetExportLimits()

    def export(
        self,
        project_id: str,
        sheet_ids: list[int],
        *,
        format_: str,
        filter_: str | None,
        sort: str | None,
        formula_policy: str,
        scope_row_ids: str | None = None,
        include_attachments: bool = False,
    ) -> SheetDatasetArtifact | SheetDatasetFile:
        project = self._workspace.get(project_id)
        self._validate_formula_policy(formula_policy)
        selected = _unique_sheet_ids(sheet_ids)
        if format_ not in ("csv", "xlsx"):
            raise SheetDatasetExportError(
                400,
                "format must be 'csv' or 'xlsx'",
                code="invalid_export_format",
            )
        scope = _parse_scope_row_ids(scope_row_ids)
        if (filter_ is not None or sort is not None or scope is not None) and len(
            selected
        ) != 1:
            raise SheetDatasetExportError(
                400,
                "current-view export requires exactly one selected sheet",
                code="invalid_current_view_selection",
            )
        project_name = self._project_name(project, project_id)
        with project.read_snapshot() as snapshot:
            plans = [
                self._build_plan(
                    snapshot,
                    sheet_id,
                    filter_=filter_,
                    sort=sort,
                    scope_row_ids=scope,
                    streaming_rowset=True,
                    include_attachment_paths=include_attachments,
                )
                for sheet_id in selected
            ]
            if include_attachments or (format_ == "csv" and len(plans) > 1):
                return self._render_archive(
                    snapshot,
                    project_name,
                    plans,
                    formula_policy,
                    format_=format_,
                    include_attachments=include_attachments,
                )
            if format_ == "xlsx":
                out = io.BytesIO()
                _write_xlsx(snapshot, plans, out, formula_policy=formula_policy)
                return SheetDatasetArtifact(
                    filename=download_filename(project_name, ".xlsx"),
                    content=out.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            plan = plans[0]
            return SheetDatasetArtifact(
                filename=download_filename(plan.sheet_name, ".csv"),
                content=b"\xef\xbb\xbf"
                + _render_plan_csv(snapshot, plan, formula_policy=formula_policy),
                media_type="text/csv; charset=utf-8-sig",
            )

    def stream_csv(
        self,
        project_id: str,
        sheet_ids: list[int],
        *,
        filter_: str | None,
        sort: str | None,
        formula_policy: str,
        scope_row_ids: str | None = None,
    ) -> SheetDatasetStream:
        """Plan a single CSV eagerly, but defer every row read to iteration."""
        project = self._workspace.get(project_id)
        self._validate_formula_policy(formula_policy)
        selected = _unique_sheet_ids(sheet_ids)
        scope = _parse_scope_row_ids(scope_row_ids)
        if len(selected) != 1:
            raise SheetDatasetExportError(
                400,
                "streaming CSV needs one selected sheet",
                code="invalid_sheet_selection",
            )
        snapshot = project.read_snapshot()
        try:
            plan = self._build_plan(
                snapshot,
                selected[0],
                filter_=filter_,
                sort=sort,
                scope_row_ids=scope,
                streaming_rowset=True,
            )
            filename = download_filename(plan.sheet_name, ".csv")
            chunks = _iter_snapshot_csv_bytes(
                snapshot, plan, formula_policy=formula_policy
            )
        except BaseException:
            snapshot.close()
            raise
        return SheetDatasetStream(
            filename=filename,
            chunks=chunks,
            media_type="text/csv; charset=utf-8-sig",
            close=snapshot.close,
        )

    def _render_archive(
        self,
        project: Any,
        project_name: str,
        plans: list[SheetExportPlan],
        formula_policy: str,
        *,
        format_: str,
        include_attachments: bool,
    ) -> SheetDatasetFile:
        directory = tempfile.TemporaryDirectory(prefix="frisket-sheet-export-")
        target = Path(directory.name) / "download.zip"
        attachments = ExportAttachments()
        attachment_path = attachments.path if include_attachments else None
        try:
            with zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                if format_ == "csv":
                    used_names: set[str] = set()
                    for plan in plans:
                        filename = _unique_filename(
                            download_filename(plan.sheet_name, ".csv"), used_names
                        )
                        with archive.open(filename, "w", force_zip64=True) as sink:
                            for chunk in iter_export_csv_bytes(
                                project,
                                plan,
                                formula_policy=formula_policy,
                                attachment_path=attachment_path,
                            ):
                                sink.write(chunk)
                else:
                    workbook_path = Path(directory.name) / "tables.xlsx"
                    _write_xlsx(
                        project,
                        plans,
                        workbook_path,
                        formula_policy=formula_policy,
                        attachment_path=attachment_path,
                    )
                    archive.write(
                        workbook_path,
                        download_filename(project_name, ".xlsx"),
                        compress_type=zipfile.ZIP_STORED,
                    )
                    workbook_path.unlink()
                attachments.write(project, archive)
        except ExportError as exc:
            directory.cleanup()
            raise SheetDatasetExportError(409, exc.message, code=exc.code) from exc
        except BaseException:
            directory.cleanup()
            raise
        return SheetDatasetFile(
            filename=download_filename(project_name, f"-{format_}.zip"),
            path=target,
            media_type="application/zip",
            close=directory.cleanup,
        )

    def _build_plan(
        self,
        project: Any,
        sheet_id: int,
        *,
        filter_: str | None,
        sort: str | None,
        scope_row_ids: list[int] | None = None,
        streaming_rowset: bool = False,
        include_attachment_paths: bool = False,
    ) -> SheetExportPlan:
        query: dict[str, Any] | None = None
        if filter_ is not None or sort is not None:
            query = {
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": sheet_id},
            }
            try:
                if filter_ is not None:
                    query["filter"] = json.loads(filter_)
                if sort is not None:
                    query["sort"] = json.loads(sort)
            except json.JSONDecodeError as exc:
                raise SheetDatasetExportError(
                    400,
                    f"invalid filter/sort JSON: {exc}",
                    code="invalid_query_json",
                ) from exc
        try:
            return build_sheet_export_plan(
                project,
                sheet_id,
                query=query,
                media_policy="references",
                value_policy="display_scalars",
                max_rows=self._limits.max_rows,
                query_field="filter",
                streaming_rowset=streaming_rowset,
                scope_row_ids=scope_row_ids,
                include_attachment_paths=include_attachment_paths,
            )
        except ExportError as exc:
            if exc.code == "invalid_sheet_ref":
                raise SheetDatasetExportError(
                    404,
                    f"no sheet '{sheet_id}'",
                    code="invalid_sheet_ref",
                ) from exc
            if exc.code == "export_rowset_too_large":
                max_rows = exc.details.get("max_rows", self._limits.max_rows)
                raise SheetDatasetExportError(
                    400,
                    f"sheet export exceeds the hosted row limit of {max_rows}",
                    code="export_rowset_too_large",
                ) from exc
            raise SheetDatasetExportError(400, exc.message, code=exc.code) from exc

    def _validate_formula_policy(self, formula_policy: str) -> None:
        if formula_policy not in ("escape", "raw"):
            raise SheetDatasetExportError(
                400,
                "formula_policy must be 'escape' or 'raw'",
                code="invalid_formula_policy",
            )

    def _project_name(self, project: Any, project_id: str) -> str:
        metadata = project.project_metadata()
        return str(metadata.get("name") or project_id)


def _write_xlsx(
    project: Any,
    plans: list[SheetExportPlan],
    destination: Any,
    *,
    formula_policy: str,
    attachment_path: AttachmentPath | None = None,
) -> None:
    workbook = Workbook(write_only=True)
    used_titles: set[str] = set()
    for plan in plans:
        worksheet = workbook.create_sheet(
            _worksheet_title(plan.sheet_name, used_titles)
        )
        worksheet.append(
            [
                _xlsx_cell(name, formula_policy=formula_policy)
                for name in plan.column_names
            ]
        )
        for batch in iter_export_row_batches(
            project, plan, attachment_path=attachment_path
        ):
            for row in batch.rows:
                worksheet.append(
                    [_xlsx_cell(value, formula_policy=formula_policy) for value in row]
                )
    workbook.save(destination)


def _unique_sheet_ids(sheet_ids: list[int]) -> list[int]:
    selected = list(dict.fromkeys(int(sheet_id) for sheet_id in sheet_ids))
    if not selected:
        raise SheetDatasetExportError(
            400,
            "select at least one sheet",
            code="missing_sheet_selection",
        )
    return selected


def _parse_scope_row_ids(raw: str | None) -> list[int] | None:
    if raw is None:
        return None
    values: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            row_id = int(token)
        except ValueError as exc:
            raise SheetDatasetExportError(
                400,
                f"invalid scope_row_ids value: {token!r}",
                code="invalid_scope_row_ids",
            ) from exc
        if row_id <= 0:
            raise SheetDatasetExportError(
                400,
                "scope_row_ids must contain positive integers",
                code="invalid_scope_row_ids",
            )
        if row_id not in seen:
            seen.add(row_id)
            values.append(row_id)
        if len(values) > 1000:
            raise SheetDatasetExportError(
                400,
                "scope_row_ids may contain at most 1000 rows",
                code="invalid_scope_row_ids",
            )
    return values


def _render_plan_csv(
    project: Any,
    plan: SheetExportPlan,
    *,
    formula_policy: str,
) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerows(iter_export_csv_rows(project, plan, formula_policy=formula_policy))
    return out.getvalue().encode("utf-8")


def _iter_snapshot_csv_bytes(
    snapshot: Any,
    plan: SheetExportPlan,
    *,
    formula_policy: str,
) -> Iterator[bytes]:
    try:
        yield from iter_export_csv_bytes(snapshot, plan, formula_policy=formula_policy)
    finally:
        snapshot.close()


def _unique_filename(filename: str, used_names: set[str]) -> str:
    stem, dot, suffix = filename.rpartition(".")
    candidate = filename
    index = 2
    while candidate.casefold() in used_names:
        candidate = f"{stem}-{index}{dot}{suffix}"
        index += 1
    used_names.add(candidate.casefold())
    return candidate


_INVALID_WORKSHEET_TITLE = re.compile(r"[\\/*?:\[\]]")


def _worksheet_title(name: str, used_titles: set[str]) -> str:
    base = _INVALID_WORKSHEET_TITLE.sub("-", name).strip("'") or "Sheet"
    base = base[:31]
    candidate = base
    index = 2
    while candidate.casefold() in used_titles:
        suffix = f"-{index}"
        candidate = f"{base[: 31 - len(suffix)]}{suffix}"
        index += 1
    used_titles.add(candidate.casefold())
    return candidate


def _xlsx_cell(value: Any, *, formula_policy: str) -> Any:
    if isinstance(value, str):
        return csv_display(value, formula_policy=formula_policy)
    if value is None or isinstance(value, bool | int | float):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

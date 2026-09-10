"""Sheet export services."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from openpyxl import Workbook

from frisket.server.exports.plan import (
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
    ) -> SheetDatasetArtifact:
        project = self._workspace.get(project_id)
        self._validate_formula_policy(formula_policy)
        selected = _unique_sheet_ids(sheet_ids)
        if format_ not in ("csv", "xlsx"):
            raise SheetDatasetExportError(
                400,
                "format must be 'csv' or 'xlsx'",
                code="invalid_export_format",
            )
        if (filter_ is not None or sort is not None) and len(selected) != 1:
            raise SheetDatasetExportError(
                400,
                "current-view export requires exactly one selected sheet",
                code="invalid_current_view_selection",
            )
        plans = [
            self._build_plan(
                project,
                sheet_id,
                filter_=filter_ if len(selected) == 1 else None,
                sort=sort if len(selected) == 1 else None,
            )
            for sheet_id in selected
        ]
        if format_ == "xlsx":
            return self._render_xlsx(project, project_id, plans, formula_policy)
        if len(plans) == 1:
            plan = plans[0]
            return SheetDatasetArtifact(
                filename=download_filename(plan.sheet_name, ".csv"),
                content=b"\xef\xbb\xbf"
                + _render_plan_csv(project, plan, formula_policy=formula_policy),
                media_type="text/csv; charset=utf-8-sig",
            )
        return self._render_csv_zip(project, project_id, plans, formula_policy)

    def stream_csv(
        self,
        project_id: str,
        sheet_ids: list[int],
        *,
        filter_: str | None,
        sort: str | None,
        formula_policy: str,
    ) -> SheetDatasetStream:
        """Plan a single CSV eagerly, but defer every row read to iteration."""
        project = self._workspace.get(project_id)
        self._validate_formula_policy(formula_policy)
        selected = _unique_sheet_ids(sheet_ids)
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

    def _render_csv_zip(
        self,
        project: Any,
        project_id: str,
        plans: list[SheetExportPlan],
        formula_policy: str,
    ) -> SheetDatasetArtifact:
        out = io.BytesIO()
        used_names: set[str] = set()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for plan in plans:
                filename = _unique_filename(
                    download_filename(plan.sheet_name, ".csv"), used_names
                )
                archive.writestr(
                    filename,
                    b"\xef\xbb\xbf"
                    + _render_plan_csv(project, plan, formula_policy=formula_policy),
                )
        return SheetDatasetArtifact(
            filename=download_filename(
                self._project_name(project, project_id), "-csv.zip"
            ),
            content=out.getvalue(),
            media_type="application/zip",
        )

    def _render_xlsx(
        self,
        project: Any,
        project_id: str,
        plans: list[SheetExportPlan],
        formula_policy: str,
    ) -> SheetDatasetArtifact:
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
            for batch in iter_export_row_batches(project, plan):
                for row in batch.rows:
                    worksheet.append(
                        [
                            _xlsx_cell(value, formula_policy=formula_policy)
                            for value in row
                        ]
                    )
        out = io.BytesIO()
        workbook.save(out)
        return SheetDatasetArtifact(
            filename=download_filename(
                self._project_name(project, project_id), ".xlsx"
            ),
            content=out.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def _build_plan(
        self,
        project: Any,
        sheet_id: int,
        *,
        filter_: str | None,
        sort: str | None,
        streaming_rowset: bool = False,
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


def _unique_sheet_ids(sheet_ids: list[int]) -> list[int]:
    selected = list(dict.fromkeys(int(sheet_id) for sheet_id in sheet_ids))
    if not selected:
        raise SheetDatasetExportError(
            400,
            "select at least one sheet",
            code="missing_sheet_selection",
        )
    return selected


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

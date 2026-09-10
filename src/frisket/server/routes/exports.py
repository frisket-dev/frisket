"""Export route registration."""

from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from frisket.server.downloads import unlink_best_effort
from frisket.server.services.project_exports import (
    ProjectExportError,
    ProjectExportService,
)
from frisket.server.services.sheet_export import (
    SheetDatasetExportError,
    SheetDatasetExportService,
)
from frisket.server.services.work_log_exports import WorkLogExportService


class _ClosingStreamingResponse(StreamingResponse):
    """Close a response-owned resource on completion, error, or disconnect."""

    def __init__(
        self,
        content: Any,
        *,
        close: Callable[[], None],
        media_type: str,
        headers: dict[str, str],
    ) -> None:
        self._close_response_resource = close
        try:
            super().__init__(content, media_type=media_type, headers=headers)
        except BaseException:
            close()
            raise

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._close_response_resource()


def register_sheet_dataset_export_route(
    app: FastAPI,
    *,
    service: SheetDatasetExportService,
) -> None:
    @app.get("/api/projects/{pid}/exports/sheets")
    def export_sheet_dataset(
        pid: str,
        sheet_ids: list[int] = Query(alias="sheet_id"),
        format_: Literal["csv", "xlsx"] = Query(alias="format"),
        filter_: str | None = Query(default=None, alias="filter"),
        sort: str | None = None,
        formula_policy: str = Query(default="escape"),
    ) -> Response:
        """Export selected sheets through the shared dataset export plan.

        Uses the same value precedence the grid uses (edits > current run result
        > original source cell), the same media reference flattening, and the
        same formula policy as the ``export.sheet_csv`` v1 action. CSV produces
        one file for one sheet or a ZIP for several; Excel produces a workbook
        with one tab per selected sheet.
        """
        try:
            if format_ == "csv" and len(set(sheet_ids)) == 1:
                export = service.stream_csv(
                    pid,
                    sheet_ids,
                    filter_=filter_,
                    sort=sort,
                    formula_policy=formula_policy,
                )
                return _ClosingStreamingResponse(
                    export.chunks,
                    close=export.close,
                    media_type=export.media_type,
                    headers={
                        "Content-Disposition": f'attachment; filename="{export.filename}"'
                    },
                )
            export = service.export(
                pid,
                sheet_ids,
                format_=format_,
                filter_=filter_,
                sort=sort,
                formula_policy=formula_policy,
            )
        except SheetDatasetExportError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        return Response(
            export.content,
            media_type=export.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename}"'
            },
        )


def register_work_log_export_routes(
    app: FastAPI,
    *,
    service: WorkLogExportService,
) -> None:
    @app.get("/api/projects/{pid}/export/work-log.md")
    def export_work_log(pid: str) -> Response:
        """Export a human-readable project work log as Markdown."""
        export = service.markdown(pid)
        return Response(
            export.body,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename}"'
            },
        )

    @app.get("/api/projects/{pid}/export/work-log.html")
    def export_work_log_html(pid: str) -> Response:
        """Export a polished HTML work log with tables and example changes."""
        export = service.html(pid)
        return Response(
            export.body,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename}"'
            },
        )

    @app.get("/api/projects/{pid}/export/work-log.pdf")
    def export_work_log_pdf(pid: str) -> Response:
        """Export the work log as a shareable text PDF."""
        export = service.pdf(pid)
        return Response(
            export.body,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename}"'
            },
        )


def register_project_export_routes(
    app: FastAPI,
    *,
    service: ProjectExportService,
) -> None:
    @app.get("/api/projects/{pid}/actions/export")
    def action_export(pid: str) -> dict:
        """Programmatic export-link discovery (``export_project``).

        This remains part of the stable JSON-first programmatic contract:
        ``export_project`` is
        advertised by ``GET /api/actions/schema`` via
        frisket.actions.ACTION_NAMES, returns frisket.actions.export_links,
        and is pinned by tests/test_programmatic_contract.py;
        session_or_pat/viewer in the endpoint catalog. Removal
        condition: remove only via a versioned frisket.actions schema cut
        that drops ``export_project`` from ACTION_NAMES and retires the
        ``action_export`` endpoint-catalog entry and its contract tests in
        the same change.
        """
        return service.action_export(pid)

    @app.get("/api/projects/{pid}/export")
    def export_project(
        pid: str,
        include_media: bool = True,
        include_traces: bool = False,
        mode: Literal["bundle", "db"] = "bundle",
    ) -> FileResponse:
        try:
            if mode == "db":
                export = service.export_database(pid)
                return FileResponse(
                    export.path,
                    media_type="application/vnd.sqlite3",
                    filename=export.filename,
                    background=BackgroundTask(unlink_best_effort, export.path),
                )
            export = service.export_bundle(
                pid,
                include_media=include_media,
                include_traces=include_traces,
            )
        except ProjectExportError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        return FileResponse(
            export.path,
            media_type="application/zip",
            filename=export.filename,
            background=BackgroundTask(unlink_best_effort, export.path),
        )


__all__ = [
    "register_project_export_routes",
    "register_sheet_dataset_export_route",
    "register_work_log_export_routes",
]

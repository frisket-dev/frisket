"""Work-log export services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frisket.server.exports.work_log import (
    build_work_log_payload,
    simple_text_pdf,
    work_log_html,
    work_log_markdown,
    work_log_pdf_lines,
)
from frisket.server.downloads import download_filename
from frisket.server.workspace import Workspace


@dataclass(frozen=True)
class WorkLogExport:
    filename: str
    body: str | bytes


class WorkLogExportService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def markdown(self, project_id: str) -> WorkLogExport:
        payload = self._payload(project_id)
        return WorkLogExport(
            filename=download_filename(payload["project_name"], "-work-log.md"),
            body=work_log_markdown(payload),
        )

    def html(self, project_id: str) -> WorkLogExport:
        payload = self._payload(project_id)
        return WorkLogExport(
            filename=download_filename(payload["project_name"], "-work-log.html"),
            body=work_log_html(payload),
        )

    def pdf(self, project_id: str) -> WorkLogExport:
        payload = self._payload(project_id)
        return WorkLogExport(
            filename=download_filename(payload["project_name"], "-work-log.pdf"),
            body=simple_text_pdf(work_log_pdf_lines(payload)),
        )

    def _payload(self, project_id: str) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        return build_work_log_payload(project, project_id)

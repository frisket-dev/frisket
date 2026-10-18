"""Best-effort priority for an explicitly opened, already-admitted PDF row."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa

from frisket.contracts.http.document_priority import DocumentPriorityResponse
from frisket.engine.jobs.queue import PROJECT_RUN_KIND, jobs_table
from frisket.server.route_errors import RouteError


PDF_IMPORT_OCR_PRIORITY = "pdf_import_ocr"


class DocumentPriorityService:
    """Find the authorized automatic OCR run that already owns a target row."""

    def __init__(self, workspace: Any) -> None:
        self.workspace = workspace

    def promote(
        self,
        project_id: str,
        *,
        sheet_id: int,
        row_id: int,
        column_id: int,
        may_run_actions: bool,
    ) -> DocumentPriorityResponse:
        if not may_run_actions:
            raise RouteError(403, "running actions is not permitted")

        # Opening the project through the workspace applies its normal project
        # and storage authority before shared queue metadata is inspected.
        project = self.workspace.get(project_id)
        storage_org_id = self.workspace.queue_storage_org_id
        c = jobs_table.c
        statement = (
            sa.select(c.id, c.run_id, c.payload)
            .where(
                c.kind == PROJECT_RUN_KIND,
                c.project_id == project_id,
                c.storage_org_id.is_(None)
                if storage_org_id is None
                else c.storage_org_id == storage_org_id,
                c.sheet_id == sheet_id,
                c.action_kind == "media.ocr",
                c.status.in_(("queued", "running")),
            )
            .order_by(c.id.desc())
        )
        with self.workspace.queue.engine.connect() as cx:
            candidates = tuple(cx.execute(statement))

        for candidate in candidates:
            payload = self._payload(candidate.payload)
            if payload is None or not self._matches_column(payload, column_id):
                continue
            run_id = int(candidate.run_id or 0)
            if run_id <= 0 or not self._run_admits_row(project, run_id, row_id):
                continue
            promoted = self.workspace.queue.promote_project_run(
                int(candidate.id),
                project_id=project_id,
                storage_org_id=storage_org_id,
                preferred_row_id=row_id,
            )
            if promoted:
                return DocumentPriorityResponse(promoted=True)

        return DocumentPriorityResponse(promoted=False)

    @staticmethod
    def _payload(raw: str) -> Mapping[str, Any] | None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None
        if payload.get("background_priority") != PDF_IMPORT_OCR_PRIORITY:
            return None
        return payload

    @staticmethod
    def _matches_column(payload: Mapping[str, Any], column_id: int) -> bool:
        columns = payload.get("v1_input_column_ids")
        if not isinstance(columns, Mapping):
            return False
        return any(
            isinstance(value, int)
            and not isinstance(value, bool)
            and value == column_id
            for value in columns.values()
        )

    @staticmethod
    def _run_admits_row(project: Any, run_id: int, row_id: int) -> bool:
        return (
            project.db.execute(
                "SELECT 1 FROM run_rows WHERE run_id=? AND row_id=? LIMIT 1",
                (run_id, row_id),
            ).fetchone()
            is not None
        )


__all__ = ["DocumentPriorityService", "PDF_IMPORT_OCR_PRIORITY"]

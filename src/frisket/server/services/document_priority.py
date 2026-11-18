"""Best-effort priority for an explicitly opened, already-imported PDF row."""

from __future__ import annotations

from typing import Any

from frisket.contracts.http.document_priority import DocumentPriorityResponse
from frisket.engine.store.import_intake import import_intake_dir, read_import_header
from frisket.server.route_errors import RouteError
from frisket.server.services.pdf_split_preparation import PdfSplitPreparationStore


class DocumentPriorityService:
    """Move an opened document's unread import OCR pages to the front."""

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
        # and storage authority before any import inventory is read.
        project = self.workspace.get(project_id)
        refs = [
            str(row[0])
            for row in project.db.execute(
                "SELECT session.id FROM import_session_rows AS member "
                "JOIN import_sessions AS session ON session.id=member.session_id "
                "WHERE member.row_id=? AND session.sheet_id=? "
                "AND session.state IN ('completed','kept') "
                "ORDER BY session.created_at DESC",
                (row_id, sheet_id),
            )
        ]
        for ref in refs:
            directory = import_intake_dir(project.path, ref)
            try:
                header = read_import_header(directory)
            except (OSError, ValueError):
                continue
            generation = (header.provider_state or {}).get("preparation_generation")
            if not generation or not (directory / "inventory.db").is_file():
                continue
            with PdfSplitPreparationStore(directory / "inventory.db") as store:
                if store.prefer_row(
                    generation=str(generation),
                    row_id=row_id,
                    source_column_id=column_id,
                ):
                    return DocumentPriorityResponse(promoted=True, import_ref=ref)
        return DocumentPriorityResponse(promoted=False, import_ref=None)


__all__ = ["DocumentPriorityService"]

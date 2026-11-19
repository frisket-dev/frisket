"""Publish frozen PDF drafts through the packet import."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from frisket.server.services.pdf_imports.organized import start_publication
from frisket.server.services.pdf_imports.service import (
    FrozenPdfImport,
    PdfImportService,
)
from frisket.server.services.pdf_packet_commit import PdfPacketCommitter


class PdfImportTextPreparation(Protocol):
    def reserve_pdf_import(
        self, project_id: str, *, receipt_id: str, import_ref: str
    ) -> Any: ...


@dataclass(frozen=True)
class PdfImportCommitResult:
    commit_id: str
    import_ref: str
    receipt_id: str
    sheet_id: int
    state: str
    committed_rows: int
    selected_total: int
    job_id: int | None = None


class PdfImportCommitService:
    """Turn a reviewed draft into one durable packet import."""

    def __init__(
        self,
        workspace: Any,
        *,
        intake: PdfImportService,
        text_preparation: PdfImportTextPreparation | None = None,
    ) -> None:
        self._workspace = workspace
        self._intake = intake
        self._text_preparation = text_preparation

    def commit_frozen(
        self, project_id: str, frozen: FrozenPdfImport, *, request_context: Any = None
    ) -> PdfImportCommitResult:
        """Commit a snapshot already frozen by the request-authority route."""

        if frozen.project_id != project_id:
            raise ValueError("frozen PDF import belongs to another project")
        project = self._workspace.get(project_id)
        if frozen.storage_identity != project.storage_identity:
            raise ValueError("frozen PDF import storage identity changed")
        try:
            result = start_publication(
                self._workspace,
                self._intake,
                project_id,
                frozen,
                request_context=request_context,
            )
        except Exception:

            def restore_draft() -> None:
                self._intake.reopen_commit(
                    project_id,
                    frozen.draft_id,
                    idempotency_key=frozen.idempotency_key,
                )

            PdfPacketCommitter(self._workspace).rollback_before_plan(
                project_id,
                idempotency_key=f"pdf-import:{frozen.commit_id}",
                restore_draft=restore_draft,
            )
            raise
        if self._text_preparation is not None and (
            frozen.pdf_preparation or frozen.semantic_search
        ):
            # External workers cannot call the tenant's in-process hook, so the
            # recovery intent for page text is persisted with the import.
            self._text_preparation.reserve_pdf_import(
                project_id,
                receipt_id=result["receipt_id"],
                import_ref=result["import_ref"],
            )
        return PdfImportCommitResult(
            commit_id=frozen.commit_id,
            import_ref=result["import_ref"],
            receipt_id=result["receipt_id"],
            sheet_id=result["sheet_id"],
            state=result["status"],
            committed_rows=result.get("document_count", 0),
            selected_total=frozen.committed_total,
            job_id=result.get("job_id"),
        )


__all__ = ["PdfImportCommitResult", "PdfImportCommitService"]

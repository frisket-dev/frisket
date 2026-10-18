"""Table import draft service."""

from typing import Any

from frisket.contracts.http.table_imports import TableImportDraft

from .service import FrozenTableImport, TableImportService
from .storage import TableImportDraftError


class _SingleProjectWorkspace:
    def __init__(self, project: Any, project_id: str) -> None:
        self._project = project
        self._project_id = project_id

    def get(self, project_id: str) -> Any:
        if project_id != self._project_id:
            raise KeyError(project_id)
        return self._project


def run_table_analysis(
    project: Any, project_id: str, draft_id: str, settings_revision: int
) -> TableImportDraft:
    """Run one claimed draft analysis against its already-open project."""

    return TableImportService(
        _SingleProjectWorkspace(project, project_id)
    ).run_analysis(project_id, draft_id, settings_revision)


__all__ = [
    "FrozenTableImport",
    "TableImportDraftError",
    "TableImportService",
    "run_table_analysis",
]

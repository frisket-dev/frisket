"""Project provenance services."""

from __future__ import annotations

from typing import Any

from frisket.server.provenance_payloads import provenance_manifest_payload
from frisket.server.workspace import Workspace


class ProjectProvenanceService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def manifest(
        self,
        project_id: str,
        *,
        runs_offset: int,
        runs_limit: int,
        receipts_offset: int,
        receipts_limit: int,
    ) -> dict[str, Any]:
        """Preserve Workspace.get's canonical missing-project behavior."""
        return provenance_manifest_payload(
            self._workspace.get(project_id),
            project_id,
            runs_offset=runs_offset,
            runs_limit=runs_limit,
            receipts_offset=receipts_offset,
            receipts_limit=receipts_limit,
        )

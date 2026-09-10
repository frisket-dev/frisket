"""Project operation history service for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.server.run_payloads import history_page_payload
from frisket.server.workspace import Workspace


class ProjectHistoryService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def history(
        self,
        project_id: str,
        *,
        offset: int | None,
        limit: int,
    ) -> dict[str, Any]:
        return history_page_payload(
            self._workspace.get(project_id),
            offset=offset,
            limit=limit,
        )

"""Project backfill activity services."""

from __future__ import annotations

from typing import Any

from frisket.server.backfill_activity_payloads import backfill_activity_payload
from frisket.server.workspace import Workspace


class ProjectBackfillActivityService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def page(
        self,
        project_id: str,
        *,
        column_id: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        """Preserve Workspace.get's canonical missing-project behavior."""
        return backfill_activity_payload(
            self._workspace.get(project_id),
            column_id=column_id,
            offset=offset,
            limit=limit,
        )

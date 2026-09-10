"""Project timing service for local server routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from frisket.server.run_status import project_timing_payload
from frisket.server.workspace import Workspace


class ProjectTimingService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def project_timing(self, project_id: str) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        jobs = self._workspace.queue.list_project_jobs(
            project_id,
            storage_org_id=self._workspace.queue_storage_org_id,
            limit=5000,
        )
        return project_timing_payload(project, jobs, now=datetime.now(UTC))

"""Project action utility services for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.authoring import actions as action_contract
from frisket.server.workspace import Workspace


class ProjectActionUtilityService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def describe_project(self, project_id: str) -> dict[str, Any]:
        return action_contract.describe_project(
            project_id,
            self._workspace.get(project_id),
        )

    def list_sheets(self, project_id: str) -> dict[str, Any]:
        return action_contract.list_sheets(project_id, self._workspace.get(project_id))

    def read_range(
        self,
        project_id: str,
        *,
        sheet_id: int,
        offset: int,
        limit: int,
        columns: list[str] | None,
    ) -> dict[str, Any]:
        return action_contract.read_range(
            project_id,
            self._workspace.get(project_id),
            sheet_id=sheet_id,
            offset=offset,
            limit=limit,
            columns=columns,
        )

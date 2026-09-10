"""Project entity and review read services."""

from __future__ import annotations

from typing import Any

from frisket.engine.runner.entities import list_entities
from frisket.engine.runner.review import queue_count, review_bundle_page, review_queue
from frisket.server.review_payloads import public_review_action_payload
from frisket.server.workspace import Workspace
from frisket.engine.store import Project


class ProjectEntityReviewError(Exception):
    def __init__(self, detail: dict[str, Any]):
        super().__init__(str(detail.get("code", "project_entity_review_error")))
        self.detail = detail


class ProjectEntityReviewService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def entities(self, project_id: str) -> list[dict[str, Any]]:
        return list_entities(self._project(project_id))

    def review_queue(
        self,
        project_id: str,
        *,
        sheet_id: int | None,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        project = self._project(project_id)
        return [
            public_review_action_payload(item)
            for item in review_queue(project, sheet_id=sheet_id, run_id=run_id)
        ]

    def review_bundles(
        self,
        project_id: str,
        *,
        sheet_id: int | None,
        offset: int,
        limit: int,
        run_id: int | None,
        include_reviewed: bool,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        page = review_bundle_page(
            project,
            sheet_id=sheet_id,
            offset=offset,
            limit=limit,
            run_id=run_id,
            include_reviewed=include_reviewed,
        )
        return {
            **page,
            "bundles": [
                public_review_action_payload(bundle) for bundle in page["bundles"]
            ],
        }

    def review_count(
        self, project_id: str, *, run_id: int | None = None
    ) -> dict[str, int]:
        return {"count": queue_count(self._project(project_id), run_id=run_id)}

    def _project(self, project_id: str) -> Project:
        return self._workspace.get(project_id)

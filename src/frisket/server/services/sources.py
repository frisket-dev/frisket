"""Source listing/detail and source-health services."""

from __future__ import annotations

import json
from typing import Any

from frisket.server.paging import offset_page_meta
from frisket.server.workspace import Workspace
from frisket.server.sources.health import SourceHealthNotFound, build_source_health
from frisket.engine.store.sources import SourceStore


class SourceNotFound(ValueError):
    pass


class SourceService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def list_sources(self, project_id: str) -> list[dict[str, Any]]:
        project = self._workspace.get(project_id)
        return [_source_dict(row) for row in SourceStore(project).sources()]

    def get_source(
        self,
        project_id: str,
        source_id: int,
        *,
        runs_offset: int,
        runs_limit: int,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        source_store = SourceStore(project)
        row = source_store.get_source(source_id)
        if row is None:
            raise SourceNotFound("source not found")

        out = _source_dict(row)
        runs = [
            dict(run)
            for run in source_store.source_runs_page(
                source_id,
                offset=runs_offset,
                limit=runs_limit,
            )
        ]
        total = source_store.source_runs_total(source_id)
        latest_run = runs[0] if runs_offset == 0 and runs else None
        if latest_run is None and total > 0:
            latest = source_store.source_runs_page(source_id, offset=0, limit=1)
            latest_run = dict(latest[0]) if latest else None
        out["runs"] = runs
        out["runs_page"] = {
            **offset_page_meta(
                schema_version="frisket.source_runs_page.v1",
                order="desc",
                offset=runs_offset,
                limit=runs_limit,
                total=total,
                item_count=len(runs),
            ),
            "latest_run": latest_run,
            "latest_run_loaded": bool(
                latest_run is not None
                and any(run["id"] == latest_run["id"] for run in runs)
            ),
        }
        return out

    def get_source_health(
        self,
        project_id: str,
        source_id: int,
        *,
        runs_offset: int,
        runs_limit: int,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        jobs = self._workspace.queue.list_project_jobs(
            project_id,
            storage_org_id=self._workspace.queue_storage_org_id,
            source_id=source_id,
            limit=100,
        )
        try:
            return build_source_health(
                project,
                source_id,
                runs_offset=runs_offset,
                runs_limit=runs_limit,
                jobs=jobs,
            )
        except SourceHealthNotFound as exc:
            raise SourceNotFound("source not found") from exc


def _source_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    try:
        data["config"] = json.loads(data.get("config") or "{}")
    except (TypeError, ValueError):
        data["config"] = {}
    data["enabled"] = bool(data.get("enabled"))
    return data

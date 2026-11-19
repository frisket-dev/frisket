"""Feed listing/detail and feed-health services."""

from __future__ import annotations

import json
from typing import Any

from frisket.contracts.http.project_sources import (
    ProjectSourceDetail,
    ProjectSourceHealth,
    ProjectSourceList,
)
from frisket.contracts.http.paging import offset_page_meta
from frisket.server.workspace import Workspace
from frisket.feeds.health import FeedHealthNotFound, build_feed_health
from frisket.engine.store.feeds import FeedStore


class FeedNotFound(ValueError):
    pass


class FeedService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def list_feeds(self, project_id: str) -> ProjectSourceList:
        project = self._workspace.get(project_id)
        return ProjectSourceList.model_validate(
            [_feed_dict(row) for row in FeedStore(project).feeds()]
        )

    def get_feed(
        self,
        project_id: str,
        feed_id: int,
        *,
        runs_offset: int,
        runs_limit: int,
    ) -> ProjectSourceDetail:
        project = self._workspace.get(project_id)
        feed_store = FeedStore(project)
        row = feed_store.get_feed(feed_id)
        if row is None:
            raise FeedNotFound("feed not found")

        out = _feed_dict(row)
        runs = [
            dict(run)
            for run in feed_store.feed_checks_page(
                feed_id,
                offset=runs_offset,
                limit=runs_limit,
            )
        ]
        total = feed_store.feed_checks_total(feed_id)
        latest_run = runs[0] if runs_offset == 0 and runs else None
        if latest_run is None and total > 0:
            latest = feed_store.feed_checks_page(feed_id, offset=0, limit=1)
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
        return ProjectSourceDetail.model_validate(out)

    def get_feed_health(
        self,
        project_id: str,
        feed_id: int,
        *,
        runs_offset: int,
        runs_limit: int,
    ) -> ProjectSourceHealth:
        project = self._workspace.get(project_id)
        jobs = self._workspace.queue.list_project_jobs(
            project_id,
            storage_org_id=self._workspace.queue_storage_organization_id,
            source_id=feed_id,
            limit=100,
        )
        try:
            health = build_feed_health(
                project,
                feed_id,
                runs_offset=runs_offset,
                runs_limit=runs_limit,
                jobs=jobs,
            )
        except FeedHealthNotFound as exc:
            raise FeedNotFound("feed not found") from exc
        return ProjectSourceHealth.model_validate(health)


def _feed_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    try:
        data["config"] = json.loads(data.get("config") or "{}")
    except (TypeError, ValueError):
        data["config"] = {}
    data["enabled"] = bool(data.get("enabled"))
    return data

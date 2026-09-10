from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.routes.instance import register_health_routes
from frisket.server.routes.sources import register_source_routes
from frisket.server.services.sources import SourceNotFound


def test_source_routes_bind_queries_defaults_and_map_service_errors() -> None:
    service = _FakeSourceService()
    app = FastAPI()
    register_source_routes(app, service=service)
    register_health_routes(app)
    client = TestClient(app)

    listed = client.get("/api/projects/p/sources")
    assert listed.status_code == 200
    assert listed.json() == [_source_payload(1)]
    assert service.calls[-1] == ("list_sources", "p")

    detail = client.get(
        "/api/projects/p/sources/9",
        params={"runs_offset": 2, "runs_limit": 3},
    )
    assert detail.status_code == 200
    assert service.calls[-1] == ("get_source", "p", 9, 2, 3)

    detail_default = client.get("/api/projects/p/sources/9")
    assert detail_default.status_code == 200
    assert service.calls[-1] == ("get_source", "p", 9, 0, 50)

    health = client.get(
        "/api/projects/p/sources/9/health",
        params={"runs_offset": 4, "runs_limit": 5},
    )
    assert health.status_code == 200
    assert service.calls[-1] == ("get_source_health", "p", 9, 4, 5)

    health_default = client.get("/api/projects/p/sources/9/health")
    assert health_default.status_code == 200
    assert service.calls[-1] == ("get_source_health", "p", 9, 0, 20)

    missing_detail = client.get("/api/projects/p/sources/404")
    assert missing_detail.status_code == 404
    assert missing_detail.json()["detail"] == "source not found"

    missing_health = client.get("/api/projects/p/sources/404/health")
    assert missing_health.status_code == 404
    assert missing_health.json()["detail"] == "source not found"

    invalid = client.get(
        "/api/projects/p/sources/9",
        params={"runs_offset": -1, "runs_limit": 0},
    )
    assert invalid.status_code == 422

    assert client.get("/api/health").json() == {"ok": True}


class _FakeSourceService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def list_sources(self, project_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_sources", project_id))
        return [_source_payload(1)]

    def get_source(
        self,
        project_id: str,
        source_id: int,
        *,
        runs_offset: int,
        runs_limit: int,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_source", project_id, source_id, runs_offset, runs_limit)
        )
        if source_id == 404:
            raise SourceNotFound("source not found")
        return {
            **_source_payload(source_id),
            "runs": [],
            "runs_page": {
                "schema_version": "frisket.source_runs_page.v1",
                "order": "desc",
                "offset": runs_offset,
                "limit": runs_limit,
                "total": 0,
                "has_more": False,
                "next_offset": None,
                "latest_run": None,
                "latest_run_loaded": False,
            },
        }

    def get_source_health(
        self,
        project_id: str,
        source_id: int,
        *,
        runs_offset: int,
        runs_limit: int,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_source_health", project_id, source_id, runs_offset, runs_limit)
        )
        if source_id == 404:
            raise SourceNotFound("source not found")
        return {
            "schema_version": "frisket.source_health.v1",
            "source": {
                "id": source_id,
                "name": f"source-{source_id}",
                "kind": "rss",
                "url": None,
                "enabled": True,
                "schedule": None,
                "sheet_id": None,
                "created_at": "2026-08-10 00:00:00",
                "redactions": ["config", "cursor"],
            },
            "summary": {
                "status": "never_run",
                "last_success_at": None,
                "last_failure_at": None,
                "consecutive_failures": 0,
                "new_rows_total": 0,
                "new_rows_recent": 0,
                "changed_rows_recent": 0,
                "skipped_rows_recent": 0,
                "revisions_recent": 0,
                "recent_run_count": 0,
                "last_cursor_summary": "not_recorded",
            },
            "runs_page": {
                "schema_version": "frisket.source_runs_page.v1",
                "order": "desc",
                "offset": runs_offset,
                "limit": runs_limit,
                "total": 0,
                "has_more": False,
                "next_offset": None,
            },
            "runs": [],
            "downstream_jobs": [],
            "costs": {
                "recent_actual_micro": 0,
                "recent_estimated_micro": 0,
                "basis": (
                    "source_runs.cost_micro for loaded runs; "
                    "downstream costs unknown-safe"
                ),
            },
            "alerts": [],
            "warnings": [],
        }


def _source_payload(source_id: int) -> dict[str, Any]:
    return {
        "id": source_id,
        "name": f"source-{source_id}",
        "kind": "rss",
        "url": None,
        "config": {},
        "sheet_id": None,
        "schedule": None,
        "enabled": True,
        "cursor": None,
        "last_checked_at": None,
        "last_status": None,
        "new_rows_total": 0,
        "created_at": "2026-08-10 00:00:00",
    }

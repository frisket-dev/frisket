from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.routes.watches import register_watch_routes
from frisket.server.services.watches import (
    WatchNotFound,
    WatchRequestError,
    WatchRunNotFound,
)


WATCH_ROUTE_ENTRIES = [
    (["GET"], "/api/projects/{pid}/watches", "list_watches"),
    (["POST"], "/api/projects/{pid}/watches", "create_watch"),
    (["PATCH"], "/api/projects/{pid}/watches/{watch_id}", "patch_watch"),
    (["DELETE"], "/api/projects/{pid}/watches/{watch_id}", "delete_watch"),
    (["POST"], "/api/projects/{pid}/watches/{watch_id}/run", "run_watch"),
    (["GET"], "/api/projects/{pid}/watches/{watch_id}/runs", "list_watch_runs"),
    (
        ["GET"],
        "/api/projects/{pid}/watches/{watch_id}/runs/{run_id}/events",
        "list_watch_run_events",
    ),
]


def _api_routes(app: FastAPI) -> list[tuple[list[str], str, str]]:
    return [
        (sorted(route.methods or []), route.path, route.endpoint.__name__)
        for route in app.router.routes
        if isinstance(route, APIRoute)
    ]


def test_watch_route_order_after_lenses_before_notifications(tmp_path: Path) -> None:
    app = create_app(tmp_path / "ws")
    routes = _api_routes(app)

    expected = [
        (["GET"], "/api/projects/{pid}/lenses/{lens_id}/resolve", "resolve_lens"),
        *WATCH_ROUTE_ENTRIES,
        (["GET"], "/api/projects/{pid}/notifications", "list_notifications"),
    ]
    index = routes.index(expected[0])

    assert routes[index : index + len(expected)] == expected


def test_watch_routes_bind_schema_queries_and_map_service_errors() -> None:
    service = _FakeWatchService()
    app = FastAPI()
    register_watch_routes(app, service=service)
    client = TestClient(app)

    listed = client.get("/api/projects/p/watches")
    assert listed.status_code == 200
    assert listed.json() == [_watch_payload(1)]

    created = client.post(
        "/api/projects/p/watches",
        json={
            "name": "Budget",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "budget"},
            "enabled": False,
        },
    )
    assert created.status_code == 200
    assert service.calls[-1] == (
        "create_watch",
        "p",
        "Budget",
        {"kind": "project"},
        {"kind": "fts", "q": "budget"},
        None,
        False,
    )

    bad_create = client.post(
        "/api/projects/p/watches",
        json={
            "name": "bad",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "budget"},
        },
    )
    assert bad_create.status_code == 400
    assert bad_create.json()["detail"] == "bad watch"

    incomplete_create = client.post("/api/projects/p/watches", json={"name": "bad"})
    assert incomplete_create.status_code == 422

    retired_source = client.post(
        "/api/projects/p/watches",
        json={
            "name": "Retired source",
            "query": {"kind": "fts", "q": "budget"},
            "source_view_id": 7,
        },
    )
    assert retired_source.status_code == 422

    retired_inline = client.post(
        "/api/projects/p/watches",
        json={
            "name": "Inline source",
            "query": {"kind": "fts", "q": "budget"},
            "source_view": {"name": "Source", "sheet_id": 7, "filter": {}},
        },
    )
    assert retired_inline.status_code == 422

    renamed = client.patch("/api/projects/p/watches/9", json={"name": " Renamed "})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Renamed"
    assert service.calls[-1] == ("patch_watch", "p", 9, {"name": "Renamed"})

    paused = client.patch("/api/projects/p/watches/9", json={"enabled": False})
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False
    assert service.calls[-1] == ("patch_watch", "p", 9, {"enabled": False})

    empty_patch = client.patch("/api/projects/p/watches/9", json={})
    assert empty_patch.status_code == 422

    deleted = client.delete("/api/projects/p/watches/9")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "deleted": 9}
    assert service.calls[-1] == ("delete_watch", "p", 9)

    missing = client.post("/api/projects/p/watches/404/run")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "watch not found"

    runs = client.get(
        "/api/projects/p/watches/9/runs",
        params={"offset": 2, "limit": 3, "hits_limit": 4},
    )
    assert runs.status_code == 200
    assert service.calls[-1] == ("list_watch_runs", "p", 9, 2, 3, 4)

    events = client.get(
        "/api/projects/p/watches/9/runs/7/events",
        params={"offset": 1, "limit": 2, "event_kind": "row_entered"},
    )
    assert events.status_code == 200
    assert service.calls[-1] == (
        "list_watch_run_events",
        "p",
        9,
        7,
        1,
        2,
        "row_entered",
    )

    bad_event = client.get(
        "/api/projects/p/watches/9/runs/7/events",
        params={"event_kind": "bad"},
    )
    assert bad_event.status_code == 400
    assert bad_event.json()["detail"] == "unsupported watch run event kind"

    wrong_run = client.get("/api/projects/p/watches/9/runs/404/events")
    assert wrong_run.status_code == 404
    assert wrong_run.json()["detail"] == "watch run not found"


def _run_payload(run_id: int, *, watch_id: int) -> dict[str, Any]:
    """The frozen watch_run wire row."""

    return {
        "id": run_id,
        "watch_id": watch_id,
        "status": "ok",
        "op_cursor_before": 0,
        "op_cursor_after": 0,
        "matched_rows": 0,
        "new_rows": 0,
        "error": None,
        "error_code": None,
        "resolved_query_hash": None,
        "resolved_query": {},
        "started_at": "2026-08-10 00:00:00",
        "finished_at": None,
    }


def _watch_payload(watch_id: int, *, name: str = "Budget") -> dict[str, Any]:
    """The frozen watch wire row."""

    return {
        "id": watch_id,
        "name": name,
        "scope": "project",
        "sheet_id": None,
        "query": {},
        "query_version": None,
        "query_hash": None,
        "detection_policy": {"kind": "new_matches"},
        "enabled": True,
        "last_evaluated_op": 0,
        "last_run_id": None,
        "last_status": None,
        "created_at": "2026-08-10 00:00:00",
        "updated_at": "2026-08-10 00:00:00",
        "latest_run": None,
    }


class _FakeWatchService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def list_watches(self, project_id: str) -> list[dict]:
        self.calls.append(("list_watches", project_id))
        return [_watch_payload(1)]

    def create_watch(
        self,
        project_id: str,
        *,
        name: str,
        scope: dict[str, Any] | None,
        query: dict[str, Any],
        detection_policy: dict[str, Any] | None,
        enabled: bool,
    ) -> dict:
        self.calls.append(
            (
                "create_watch",
                project_id,
                name,
                scope,
                query,
                detection_policy,
                enabled,
            )
        )
        if name == "bad":
            raise WatchRequestError("bad watch")
        return _watch_payload(2, name=name)

    def run_watch(self, project_id: str, watch_id: int) -> dict:
        self.calls.append(("run_watch", project_id, watch_id))
        if watch_id == 404:
            raise WatchNotFound("watch not found")
        return {
            "schema_version": "frisket.watch_run.v1",
            "watch": _watch_payload(watch_id),
            "run": _run_payload(7, watch_id=watch_id),
            "hits": [],
        }

    def patch_watch(
        self,
        project_id: str,
        watch_id: int,
        *,
        fields: dict[str, Any],
    ) -> dict:
        self.calls.append(("patch_watch", project_id, watch_id, fields))
        if not fields:
            raise WatchRequestError("at least one watch field is required")
        if "name" in fields:
            name = str(fields["name"]).strip()
            if not name:
                raise WatchRequestError("watch name is required")
            return _watch_payload(watch_id, name=name)
        return {**_watch_payload(watch_id), "enabled": bool(fields["enabled"])}

    def delete_watch(self, project_id: str, watch_id: int) -> dict:
        self.calls.append(("delete_watch", project_id, watch_id))
        if watch_id == 404:
            raise WatchNotFound("watch not found")
        return {"ok": True, "deleted": watch_id}

    def list_watch_runs(
        self,
        project_id: str,
        watch_id: int,
        *,
        offset: int,
        limit: int,
        hits_limit: int,
    ) -> dict:
        self.calls.append(
            ("list_watch_runs", project_id, watch_id, offset, limit, hits_limit)
        )
        return {
            "schema_version": "frisket.watch_runs_page.v1",
            "order": "desc",
            "offset": offset,
            "limit": limit,
            "total": 0,
            "has_more": False,
            "next_offset": None,
            "hits_limit": hits_limit,
            "runs": [],
        }

    def list_watch_run_events(
        self,
        project_id: str,
        watch_id: int,
        run_id: int,
        *,
        offset: int,
        limit: int,
        event_kind: str | None,
    ) -> dict:
        self.calls.append(
            (
                "list_watch_run_events",
                project_id,
                watch_id,
                run_id,
                offset,
                limit,
                event_kind,
            )
        )
        if run_id == 404:
            raise WatchRunNotFound("watch run not found")
        if event_kind == "bad":
            raise WatchRequestError("unsupported watch run event kind")
        return {
            "schema_version": "frisket.watch_run_events_page.v1",
            "order": "asc",
            "offset": offset,
            "limit": limit,
            "total": 0,
            "has_more": False,
            "next_offset": None,
            "events": [],
        }

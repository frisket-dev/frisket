from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.routes.views import register_view_lens_routes
from frisket.server.services.views import (
    LensNotFound,
    LensRequestError,
    ViewNotFound,
)


VIEW_LENS_ROUTE_ENTRIES = [
    (["GET"], "/api/projects/{pid}/views", "list_views"),
    (["POST"], "/api/projects/{pid}/views", "create_view"),
    (["GET"], "/api/projects/{pid}/views/{view_id}", "get_view_ep"),
    (["PATCH"], "/api/projects/{pid}/views/{view_id}", "patch_view"),
    (
        ["PUT"],
        "/api/projects/{pid}/views/{view_id}/definition",
        "replace_view_definition",
    ),
    (["DELETE"], "/api/projects/{pid}/views/{view_id}", "delete_view_ep"),
    (["GET"], "/api/projects/{pid}/lenses", "list_lenses"),
    (["POST"], "/api/projects/{pid}/lenses", "create_lens"),
    (["GET"], "/api/projects/{pid}/lenses/{lens_id}", "get_lens_ep"),
    (["PATCH"], "/api/projects/{pid}/lenses/{lens_id}", "patch_lens"),
    (["DELETE"], "/api/projects/{pid}/lenses/{lens_id}", "delete_lens_ep"),
    (["GET"], "/api/projects/{pid}/lenses/{lens_id}/resolve", "resolve_lens"),
]


def _api_routes(app: FastAPI) -> list[tuple[list[str], str, str]]:
    return [
        (sorted(route.methods or []), route.path, route.endpoint.__name__)
        for route in app.router.routes
        if isinstance(route, APIRoute)
    ]


def test_view_lens_route_order_after_spend_before_watchlists(tmp_path: Path) -> None:
    app = create_app(tmp_path / "ws")
    routes = _api_routes(app)

    expected = [
        (["GET"], "/api/spend", "spend"),
        *VIEW_LENS_ROUTE_ENTRIES,
        (["GET"], "/api/projects/{pid}/watches", "list_watches"),
    ]
    index = routes.index(expected[0])

    assert routes[index : index + len(expected)] == expected


def test_lens_patch_presentation_alone_does_not_rewrite_spec(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Lens patch"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("rows")

    created = client.post(
        f"/api/projects/{pid}/lenses",
        json={
            "name": "Open rows",
            "query": {
                "kind": "filter",
                "sheet_id": sheet_id,
                "filter": {},
            },
            "presentation": {"columns": ["headline"]},
        },
    )
    assert created.status_code == 200, created.text
    original_spec = created.json()["spec"]

    patched = client.patch(
        f"/api/projects/{pid}/lenses/{created.json()['id']}",
        json={"presentation": {"columns": ["changed"]}},
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["spec"] == original_spec


def test_view_lens_routes_bind_schema_query_and_map_service_errors() -> None:
    service = _FakeViewLensService()
    app = FastAPI()
    register_view_lens_routes(app, service=service)
    client = TestClient(app)

    listed = client.get("/api/projects/p/views", params={"sheet_id": 7})
    assert listed.status_code == 200
    assert listed.json() == [_row_payload(1, sheet_id=7)]

    renamed = client.patch("/api/projects/p/views/3", json={"name": "Renamed"})
    assert renamed.status_code == 200
    assert service.calls[-1] == ("rename_view", "p", 3, "Renamed")

    replaced = client.put(
        "/api/projects/p/views/3/definition",
        json={
            "filter": {},
            "sort": None,
            "columns": ["name"],
            "column_groups": None,
        },
    )
    assert replaced.status_code == 200
    assert service.calls[-1] == (
        "replace_view_definition",
        "p",
        3,
        {},
        None,
        ["name"],
        None,
    )

    missing_view = client.get("/api/projects/p/views/404")
    assert missing_view.status_code == 404
    assert missing_view.json()["detail"] == "view not found"

    resolved = client.get("/api/projects/p/lenses/9/resolve?limit=3&offset=1")
    assert resolved.status_code == 400
    assert resolved.json()["detail"] == {
        "code": "bad_lens",
        "message": "cannot resolve",
        "field": "query",
    }
    assert service.calls[-1] == ("resolve_lens", "p", 9, 3, 1)


def _row_payload(row_id: int, *, sheet_id: int | None = 3) -> dict[str, Any]:
    """The frozen seven-field saved-view/saved-lens wire row."""

    return {
        "id": row_id,
        "name": f"row-{row_id}",
        "sheet_id": sheet_id,
        "spec": {},
        "op_id": None,
        "created_at": "2026-08-10 00:00:00",
        "updated_at": "2026-08-10 00:00:00",
    }


class _FakeViewLensService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def list_views(self, project_id: str, *, sheet_id: int | None = None) -> list[dict]:
        self.calls.append(("list_views", project_id, sheet_id))
        return [_row_payload(1, sheet_id=sheet_id)]

    def create_view(
        self,
        project_id: str,
        *,
        name: str,
        sheet_id: int,
        filter_: dict[str, Any],
        sort: list[Any] | None,
        columns: list[Any] | None,
        column_groups: list[Any] | None,
    ) -> dict:
        self.calls.append(
            (
                "create_view",
                project_id,
                name,
                sheet_id,
                filter_,
                sort,
                columns,
                column_groups,
            )
        )
        return {**_row_payload(2, sheet_id=sheet_id), "name": name}

    def get_view(self, project_id: str, view_id: int) -> dict:
        self.calls.append(("get_view", project_id, view_id))
        if view_id == 404:
            raise ViewNotFound("view not found")
        return _row_payload(view_id)

    def rename_view(
        self,
        project_id: str,
        view_id: int,
        *,
        name: str,
    ) -> dict:
        self.calls.append(("rename_view", project_id, view_id, name))
        return _row_payload(view_id)

    def replace_view_definition(
        self,
        project_id: str,
        view_id: int,
        *,
        filter_: dict[str, Any],
        sort: list[Any] | None,
        columns: list[Any] | None,
        column_groups: list[Any] | None,
    ) -> dict:
        self.calls.append(
            (
                "replace_view_definition",
                project_id,
                view_id,
                filter_,
                sort,
                columns,
                column_groups,
            )
        )
        return _row_payload(view_id)

    def delete_view(self, project_id: str, view_id: int) -> dict:
        self.calls.append(("delete_view", project_id, view_id))
        return {"ok": True, "deleted": view_id}

    def list_lenses(
        self,
        project_id: str,
        *,
        sheet_id: int | None = None,
    ) -> list[dict]:
        self.calls.append(("list_lenses", project_id, sheet_id))
        return [_row_payload(4, sheet_id=sheet_id)]

    def create_lens(
        self,
        project_id: str,
        *,
        name: str,
        query: dict[str, Any],
        presentation: dict[str, Any] | None,
    ) -> dict:
        self.calls.append(("create_lens", project_id, name, query, presentation))
        return {**_row_payload(5), "name": name}

    def get_lens(self, project_id: str, lens_id: int) -> dict:
        self.calls.append(("get_lens", project_id, lens_id))
        if lens_id == 404:
            raise LensNotFound("lens not found")
        return _row_payload(lens_id)

    def patch_lens(
        self,
        project_id: str,
        lens_id: int,
        *,
        fields: dict[str, Any],
        provided_fields: set[str],
    ) -> dict:
        self.calls.append(("patch_lens", project_id, lens_id, fields, provided_fields))
        return _row_payload(lens_id)

    def delete_lens(self, project_id: str, lens_id: int) -> dict:
        self.calls.append(("delete_lens", project_id, lens_id))
        return {"ok": True, "deleted": lens_id}

    def resolve_lens(
        self,
        project_id: str,
        lens_id: int,
        *,
        limit: int,
        offset: int,
    ) -> dict:
        self.calls.append(("resolve_lens", project_id, lens_id, limit, offset))
        raise LensRequestError(
            code="bad_lens",
            message="cannot resolve",
            field="query",
        )

"""HTTP contracts for the global and project column-type catalog reads."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.contracts.http.column_types import ColumnTypeList
from frisket.contracts.http.models import HttpError
from frisket.server.routes.sheet_features import register_column_type_routes


class _CatalogService:
    def list_column_types(self, project_id: str | None = None) -> list[dict[str, Any]]:
        del project_id
        return [
            {
                "name": "plugin_stars",
                "core": False,
                "plugin": "stars",
                "presentation": {
                    "renderer": "stars",
                    "align": "center",
                    "userSelectable": True,
                },
                "has_validator": True,
                "has_parser": False,
                "description": "A plugin-provided rating.",
            }
        ]


ROUTES = {
    "list_column_types": ("/api/column-types", {401, 500}),
    "list_project_column_types": (
        "/api/projects/{pid}/column-types",
        {401, 403, 404, 500},
    ),
}


def _app() -> FastAPI:
    app = FastAPI()
    register_column_type_routes(app, service=_CatalogService())  # type: ignore[arg-type]
    return app


def test_column_type_routes_publish_strict_bare_array_contracts() -> None:
    app = _app()
    routes = {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name in ROUTES
    }
    assert set(routes) == set(ROUTES)
    for name, (path, errors) in ROUTES.items():
        route = routes[name]
        assert route.path == path
        assert route.methods == {"GET"}
        assert route.response_model is ColumnTypeList
        assert route.response_model_exclude_unset is True
        assert set(route.responses) == errors
        assert all(value == {"model": HttpError} for value in route.responses.values())

    schema = app.openapi()
    entry = schema["components"]["schemas"]["ColumnTypeEntry"]
    assert entry["additionalProperties"] is False
    assert entry["required"] == [
        "name",
        "core",
        "plugin",
        "presentation",
        "has_validator",
        "has_parser",
        "description",
    ]
    assert "additionalProperties" in entry["properties"]["presentation"]
    for path in ("/api/column-types", "/api/projects/{pid}/column-types"):
        assert schema["paths"][path]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/ColumnTypeList"}
    # FastAPI emits an implicit 422 for every path parameter in its raw schema;
    # the shared exporter removes that impossible framework response. The route
    # declarations above are the truthful status source consumed by it.


def test_column_type_catalog_round_trips_open_presentation_bytes() -> None:
    client = TestClient(_app())
    expected = _CatalogService().list_column_types()
    for path in ("/api/column-types", "/api/projects/project-1/column-types"):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json() == expected
        assert list(response.json()[0]) == [
            "name",
            "core",
            "plugin",
            "presentation",
            "has_validator",
            "has_parser",
            "description",
        ]
    assert (
        ColumnTypeList.model_validate(expected).model_dump(exclude_unset=True)
        == expected
    )

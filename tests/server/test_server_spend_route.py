from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.routes.spend import register_spend_routes


def test_spend_route_inventory_order_after_project_exports_before_views(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "ws")
    routes = [
        (sorted(route.methods or []), route.path, route.endpoint.__name__)
        for route in app.router.routes
        if isinstance(route, APIRoute)
    ]

    expected = [
        (["GET"], "/api/projects/{pid}/actions/export", "action_export"),
        (["GET"], "/api/projects/{pid}/export", "export_project"),
        (["GET"], "/api/spend", "spend"),
        (["GET"], "/api/projects/{pid}/views", "list_views"),
    ]
    index = routes.index(expected[0])

    assert routes[index : index + len(expected)] == expected


def test_register_spend_routes_returns_fake_service_payload_verbatim() -> None:
    class FakeSpendService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def spend_dashboard(self) -> dict:
            self.calls.append("spend_dashboard")
            return {
                "rows": [
                    {
                        "project": "p",
                        "model": None,
                        "month": None,
                        "runs": 2,
                        "rows": 3,
                        "cost": 0.123456789,
                    }
                ],
                "total_cost": 0.123456789,
                "has_unknown_costs": True,
                "unknown_cost_models": ["unpriced"],
            }

    app = FastAPI()
    service = FakeSpendService()
    register_spend_routes(app, service=service)

    response = TestClient(app).get("/api/spend")

    assert response.status_code == 200
    assert response.json() == {
        "rows": [
            {
                "project": "p",
                "model": None,
                "month": None,
                "runs": 2,
                "rows": 3,
                "cost": 0.123456789,
            }
        ],
        "total_cost": 0.123456789,
        "has_unknown_costs": True,
        "unknown_cost_models": ["unpriced"],
    }
    assert list(response.json()) == [
        "rows",
        "total_cost",
        "has_unknown_costs",
        "unknown_cost_models",
    ]
    assert list(response.json()["rows"][0]) == [
        "project",
        "model",
        "month",
        "runs",
        "rows",
        "cost",
    ]
    assert service.calls == ["spend_dashboard"]


def test_spend_route_declares_a_closed_response_and_auth_error_surface() -> None:
    class EmptySpendService:
        def spend_dashboard(self) -> dict:
            return {
                "rows": [],
                "total_cost": 0.0,
                "has_unknown_costs": False,
                "unknown_cost_models": [],
            }

    app = FastAPI()
    register_spend_routes(app, service=EmptySpendService())
    [route] = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/api/spend"
    ]

    assert route.response_model is not None
    assert route.response_model.__name__ == "SpendReport"
    assert route.responses.keys() == {401, 403, 500}
    schema = app.openapi()["paths"]["/api/spend"]["get"]
    assert schema.get("parameters", []) == []
    assert "requestBody" not in schema
    assert schema["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SpendReport"
    }

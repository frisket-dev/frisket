from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_project_copilot_route_order_before_search(tmp_path: Path) -> None:
    app = create_app(tmp_path / "ws")
    routes = [
        (sorted(route.methods or []), route.path)
        for route in app.router.routes
        if isinstance(route, APIRoute)
    ]

    previous_route = (["GET"], "/api/projects/{pid}/review/count")
    expected = [
        (["POST"], "/api/projects/{pid}/copilot"),
        (["GET"], "/api/projects/{pid}/search"),
    ]
    index = routes.index(previous_route)

    assert routes[index + 1 : index + 1 + len(expected)] == expected


def test_project_copilot_llm_failure_maps_to_502(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Copilot"}).json()["id"]

    response = client.post(
        f"/api/projects/{project_id}/copilot",
        json={"messages": [{"role": "user", "content": "help"}]},
    )

    assert response.status_code == 502
    assert "anthropic" in response.json()["detail"]

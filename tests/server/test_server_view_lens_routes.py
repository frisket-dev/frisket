"""Unified View route boundary replaces independent saved-lens authority."""

from fastapi.routing import APIRoute
from frisket.server.app import create_app


def test_view_routes_have_one_authority(tmp_path):
    app = create_app(tmp_path / "workspace")
    routes = {
        (route.path, method): route.name
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert not any("/lenses" in path for path, _ in routes)
    expected = {
        ("/api/projects/{pid}/views", "GET"): "list_views",
        ("/api/projects/{pid}/views", "POST"): "create_view",
        ("/api/projects/{pid}/views/{view_id}", "GET"): "get_view_ep",
        ("/api/projects/{pid}/views/{view_id}", "PATCH"): "patch_view",
        ("/api/projects/{pid}/views/{view_id}", "DELETE"): "delete_view_ep",
        (
            "/api/projects/{pid}/views/{view_id}/definition",
            "PUT",
        ): "replace_view_definition",
        ("/api/projects/{pid}/views/{view_id}/resolve", "GET"): "resolve_view",
    }
    assert {key: routes[key] for key in expected} == expected

from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.server.app import create_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _api_route_inventory(app) -> list[tuple[list[str], str, str]]:
    inventory: list[tuple[list[str], str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api"):
            continue
        methods = sorted(
            method for method in getattr(route, "methods", set()) if method != "HEAD"
        )
        inventory.append((methods, path, route.name))
    return inventory


def _route(app, path: str) -> APIRoute:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path:
            return route
    raise AssertionError(f"missing route {path}")


def _query_defaults(route: APIRoute) -> dict[str, tuple[str, object]]:
    out: dict[str, tuple[str, object]] = {}
    for param in route.dependant.query_params:
        default: object = (
            "<required>"
            if repr(param.default) == "PydanticUndefined"
            else param.default
        )
        out[param.name] = (param.alias, default)
    return out


def _path_segments(path: str) -> list[str]:
    return path.strip("/").split("/")


def _is_param_segment(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _paths_could_overlap(path_a: str, path_b: str) -> bool:
    """True if some concrete URL could match both `path_a` and `path_b`.

    Two paths overlap only if they have the same segment count and, at
    every differing segment, at least one side is a `{param}` -- a literal
    segment mismatch (e.g. "sheets" vs "views") means no URL can satisfy
    both regardless of param positions elsewhere.
    """
    segments_a, segments_b = _path_segments(path_a), _path_segments(path_b)
    if len(segments_a) != len(segments_b):
        return False
    for segment_a, segment_b in zip(segments_a, segments_b):
        if segment_a == segment_b:
            continue
        if _is_param_segment(segment_a) or _is_param_segment(segment_b):
            continue
        return False
    return True


def _overlapping_same_method_route_pairs(
    routes: list[tuple[list[str], str, str]],
) -> list[tuple[str, str, str, str]]:
    conflicts = []
    for i, (methods_a, path_a, name_a) in enumerate(routes):
        for methods_b, path_b, name_b in routes[i + 1 :]:
            if not set(methods_a) & set(methods_b):
                continue
            if _paths_could_overlap(path_a, path_b):
                conflicts.append((path_a, name_a, path_b, name_b))
    return conflicts


def test_route_overlap_inventory_detects_duplicate_and_template_routes():
    assert _overlapping_same_method_route_pairs(
        [
            (["GET"], "/api/widgets/{widget_id}/read", "widget_by_id"),
            (["GET"], "/api/widgets/{slug}/read", "widget_by_slug"),
            (["GET"], "/api/health", "first_health"),
            (["GET"], "/api/health", "second_health"),
        ]
    ) == [
        (
            "/api/widgets/{widget_id}/read",
            "widget_by_id",
            "/api/widgets/{slug}/read",
            "widget_by_slug",
        ),
        (
            "/api/health",
            "first_health",
            "/api/health",
            "second_health",
        ),
    ]


def test_route_overlap_inventory_allows_same_path_with_disjoint_methods():
    assert (
        _overlapping_same_method_route_pairs(
            [
                (["GET"], "/api/widgets/search", "get_search"),
                (["POST"], "/api/widgets/search", "post_search"),
            ]
        )
        == []
    )


def test_no_order_sensitive_route_overlaps(tmp_path):
    """No two /api routes should ever ambiguously match the same URL+method.

    FastAPI/Starlette resolves routes in REGISTRATION order, so if two
    routes could both match one concrete request (e.g. a literal
    "/api/projects/search" registered AFTER a param route
    "/api/projects/{pid}"), the later one is silently unreachable and
    correctness would start depending on list position. If this ever
    fails, the reported pair needs EITHER a path redesign (no overlap) OR
    an explicit relative-order pin scoped to just that pair -- not a
    full-list route-order transcript (deleted 2026-07-19, rule 19).
    """
    app = create_app(tmp_path / "workspace")
    conflicts = _overlapping_same_method_route_pairs(_api_route_inventory(app))

    assert conflicts == [], f"order-sensitive route overlaps found: {conflicts}"


def test_route_inventory_captures_query_aliases_and_defaults(tmp_path):
    app = create_app(tmp_path / "workspace")

    assert _query_defaults(
        _route(app, "/api/projects/{pid}/sheets/{sheet_id}/data")
    ) == {
        "offset": ("offset", 0),
        "limit": ("limit", 200),
        "parent_row_id": ("parent_row_id", None),
        "filter_": ("filter", None),
        "sort": ("sort", None),
        "row_ids": ("row_ids", None),
    }
    assert _query_defaults(_route(app, "/api/projects/{pid}/exports/sheets")) == {
        "sheet_ids": ("sheet_id", "<required>"),
        "format_": ("format", "<required>"),
        "filter_": ("filter", None),
        "sort": ("sort", None),
        "formula_policy": ("formula_policy", "escape"),
    }
    assert _query_defaults(_route(app, "/api/projects/{pid}/actions/v1/run")) == {}
    assert _query_defaults(
        _route(app, "/api/projects/{pid}/workbench/marketplace")
    ) == {
        "contribution_id": ("contribution_id", "<required>"),
        "query": ("query", ""),
    }


def test_inventory_captures_download_blob_and_import_headers(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Inventory"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("rows.csv", "name\nAda\nGrace\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    csv_response = client.get(
        f"/api/projects/{pid}/exports/sheets?sheet_id={sheet_id}&format=csv"
    )
    assert csv_response.status_code == 200, csv_response.text
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert 'filename="rows.csv"' in csv_response.headers["content-disposition"]
    assert [row["name"] for row in csv.DictReader(io.StringIO(csv_response.text))] == [
        "Ada",
        "Grace",
    ]

    project = client.app.state.workspace.get(pid)
    digest = project.add_blob(
        b"hello",
        filename="hello.txt",
        mime="text/plain",
    )
    blob_response = client.get(f"/api/projects/{pid}/blobs/{digest}")
    assert blob_response.status_code == 200, blob_response.text
    assert blob_response.headers["content-type"].startswith("text/plain")
    assert blob_response.content == b"hello"

    bundle_response = client.get(f"/api/projects/{pid}/export")
    assert bundle_response.status_code == 200, bundle_response.text
    assert bundle_response.headers["content-type"].startswith("application/zip")
    assert "inventory.frisket.zip" in bundle_response.headers["content-disposition"]

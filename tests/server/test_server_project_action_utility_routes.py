from __future__ import annotations

import ast
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.server.app import create_app


ROOT = Path(__file__).resolve().parents[2]
SERVER_APP = ROOT / "src" / "frisket" / "server" / "app.py"

PROJECT_ACTION_ROUTE_PATHS = {
    "/api/projects/{pid}/actions/describe",
    "/api/projects/{pid}/actions/sheets",
    "/api/projects/{pid}/actions/read-range",
}


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _route_decorator_paths(tree: ast.Module) -> set[str]:
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            if call is None or not call.args:
                continue
            target = call.func
            if (
                isinstance(target, ast.Attribute)
                and target.attr in {"get", "post", "patch", "delete", "api_route"}
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                paths.add(call.args[0].value)
    return paths


def test_project_action_routes_keep_split_route_order(tmp_path) -> None:
    app = create_app(tmp_path / "workspace")
    routes = [
        (sorted(route.methods - {"HEAD", "OPTIONS"}), route.path, route.name)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
    ]

    # Index-independent adjacency pin (reconciled 2026-07-07,
    # manifest-check-reconciliation-v1): the absolute ordinals (routes[27],
    # routes[42:44]) were pre-redesign implementation details that shifted with
    # every route landing ahead of action_describe_project (settings revamp,
    # provider-keys, seed-sample, inc-7 /lineage insertion, ...). The
    # load-bearing invariant is that the split action-utility routes register
    # AFTER action_describe_project and that sheets + read-range stay adjacent
    # in that order. Full-inventory order stays pinned by the green
    # test_server_route_inventory.py.
    describe = (
        ["GET"],
        "/api/projects/{pid}/actions/describe",
        "action_describe_project",
    )
    sheets = (["GET"], "/api/projects/{pid}/actions/sheets", "action_list_sheets")
    read_range = (
        ["POST"],
        "/api/projects/{pid}/actions/read-range",
        "action_read_range",
    )
    assert describe in routes
    assert sheets in routes
    sheets_at = routes.index(sheets)
    assert routes[sheets_at : sheets_at + 2] == [sheets, read_range]
    assert routes.index(describe) < sheets_at


def test_project_action_data_routes_preserve_http_contract(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Action Utilities"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("people")
    columns = {"name": project.add_column(sheet_id, "name", type="text")}
    project.add_rows(sheet_id, [{"name": "Ada"}], columns)

    sheets = client.get(f"/api/projects/{pid}/actions/sheets")
    assert sheets.status_code == 200, sheets.text
    sheets_body = sheets.json()
    assert sheets_body["action"] == "list_sheets"
    assert sheets_body["sheets"][0]["id"] == sheet_id

    page = client.post(
        f"/api/projects/{pid}/actions/read-range",
        json={"sheet_id": sheet_id, "columns": ["name"], "limit": 1},
    )
    assert page.status_code == 200, page.text
    assert page.json()["rows"] == [
        {"row_id": 1, "row_index": 1, "cells": {"name": "Ada"}}
    ]

    missing = client.post(
        f"/api/projects/{pid}/actions/read-range",
        json={"sheet_id": 9999, "limit": 1},
    )
    assert missing.status_code == 404

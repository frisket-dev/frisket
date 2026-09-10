"""Browser HTTP contracts for the existing lineage and sheet-graph reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.projects import register_project_lifecycle_routes
from frisket.server.routes.sheet_features import register_sheet_graph_routes
from frisket.server.services.projects import ProjectNotFound
from frisket.server.services.sheet_graph import SheetGraphRouteError
from scripts.ci.export_web_openapi import export_real_compositions


_LINEAGE_PATH = "/api/projects/{pid}/lineage"
_SHEET_GRAPH_PATH = "/api/projects/{pid}/sheets/{sheet_id}/graph"
_OPERATION_IDS = {
    "lineage": "tenant.project_lineage.get",
    "sheet_graph": "tenant.sheet_graph.get",
}


class _ProjectService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def lineage(self, pid: str) -> dict[str, Any]:
        self.calls.append(pid)
        if pid == "missing":
            raise ProjectNotFound("no project 'missing'")
        return {
            "project_id": pid,
            "op_cursor": 17,
            "nodes": [
                {
                    "id": "sheet:1",
                    "kind": "sheet",
                    "name": "Cases",
                    "nested": [None, {"future": True}],
                }
            ],
            "edges": [],
            "extensions": {"arbitrary": [1, False, None]},
        }


class _SheetGraphService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, dict[str, Any]]] = []

    def sheet_graph(self, pid: str, sheet_id: int, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((pid, sheet_id, kwargs))
        if sheet_id == 404:
            raise SheetGraphRouteError(
                404,
                {"code": "sheet_not_found", "message": "no sheet 404", "future": None},
            )
        return {
            "schema_version": "frisket.sheet_graph.v1",
            "sheet_id": sheet_id,
            "materialized_kind": "edge",
            "direction": "directed",
            "nodes": [],
            "edges": [],
            "truncated": False,
            "limits": {
                "nodes": kwargs["limit_nodes"],
                "edges": kwargs["limit_edges"],
                "returned_nodes": 0,
                "returned_edges": 0,
            },
            "config": kwargs,
            "diagnostics": [
                {
                    "code": "future",
                    "message": "future diagnostic",
                    "nested": {"null": None},
                }
            ],
        }


def _client() -> tuple[TestClient, _ProjectService, _SheetGraphService]:
    app = FastAPI()
    register_route_error_handler(app)
    projects = _ProjectService()
    sheet_graph = _SheetGraphService()
    register_project_lifecycle_routes(app, service=projects)  # type: ignore[arg-type]
    register_sheet_graph_routes(app, service=sheet_graph)  # type: ignore[arg-type]
    return TestClient(app), projects, sheet_graph


def test_lineage_is_a_lossless_bodyless_get_with_ignored_unknown_query() -> None:
    client, projects, _sheet_graph = _client()

    response = client.get("/api/projects/project-1/lineage?future=ignored")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "project_id": "project-1",
        "op_cursor": 17,
        "nodes": [
            {
                "id": "sheet:1",
                "kind": "sheet",
                "name": "Cases",
                "nested": [None, {"future": True}],
            }
        ],
        "edges": [],
        "extensions": {"arbitrary": [1, False, None]},
    }
    assert projects.calls == ["project-1"]

    missing = client.get("/api/projects/missing/lineage")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "no project 'missing'"}


def test_sheet_graph_preserves_existing_coercion_defaults_errors_and_extensions() -> (
    None
):
    client, _projects, sheet_graph = _client()

    response = client.get(
        "/api/projects/project-1/sheets/7/graph",
        params={
            "direction": "future-direction",
            "node_label_column_id": "11",
            "node_color_column_id": "12",
            "node_size_column_id": "13",
            "edge_label_column_id": "14",
            "limit_nodes": "2000",
            "limit_edges": "5000",
            "ignored": "yes",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["diagnostics"] == [
        {
            "code": "future",
            "message": "future diagnostic",
            "nested": {"null": None},
        }
    ]
    assert response.json()["config"] == {
        "direction": "future-direction",
        "node_label_column_id": 11,
        "node_color_column_id": 12,
        "node_size_column_id": 13,
        "edge_label_column_id": 14,
        "limit_nodes": 2000,
        "limit_edges": 5000,
    }
    assert sheet_graph.calls == [
        (
            "project-1",
            7,
            {
                "direction": "future-direction",
                "node_label_column_id": 11,
                "node_color_column_id": 12,
                "node_size_column_id": 13,
                "edge_label_column_id": 14,
                "limit_nodes": 2000,
                "limit_edges": 5000,
            },
        )
    ]

    defaults = client.get("/api/projects/project-1/sheets/8/graph")
    assert defaults.status_code == 200
    assert sheet_graph.calls[-1][2] == {
        "direction": None,
        "node_label_column_id": None,
        "node_color_column_id": None,
        "node_size_column_id": None,
        "edge_label_column_id": None,
        "limit_nodes": 200,
        "limit_edges": 500,
    }

    invalid = client.get("/api/projects/project-1/sheets/not-an-int/graph")
    assert invalid.status_code == 422
    too_many = client.get("/api/projects/project-1/sheets/8/graph?limit_nodes=2001")
    assert too_many.status_code == 422
    assert len(sheet_graph.calls) == 2

    missing = client.get("/api/projects/project-1/sheets/404/graph")
    assert missing.status_code == 404
    assert missing.json() == {
        "detail": {"code": "sheet_not_found", "message": "no sheet 404", "future": None}
    }


def test_graph_lineage_openapi_uses_two_distinct_open_object_responses() -> None:
    client, _projects, _sheet_graph = _client()
    document = client.app.openapi()

    lineage = document["paths"][_LINEAGE_PATH]["get"]
    sheet_graph = document["paths"][_SHEET_GRAPH_PATH]["get"]
    assert "requestBody" not in lineage
    assert set(lineage["responses"]) >= {"200", "401", "403", "404", "422", "500"}
    assert set(sheet_graph["responses"]) >= {"200", "401", "403", "404", "422", "500"}
    lineage_schema = lineage["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    graph_schema = sheet_graph["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert lineage_schema == {"$ref": "#/components/schemas/ProjectLineageResponse"}
    assert graph_schema == {"$ref": "#/components/schemas/SheetGraphResponse"}
    lineage_component = document["components"]["schemas"]["ProjectLineageResponse"]
    graph_component = document["components"]["schemas"]["SheetGraphResponse"]
    assert set(lineage_component["required"]) == {
        "project_id",
        "op_cursor",
        "nodes",
        "edges",
    }
    assert set(graph_component["required"]) == {
        "schema_version",
        "sheet_id",
        "materialized_kind",
        "direction",
        "nodes",
        "edges",
        "truncated",
        "limits",
        "diagnostics",
    }


def test_graph_lineage_catalog_and_real_export_add_only_the_two_browser_reads(
    tmp_path: Path,
) -> None:
    policies = {entry.id: entry for entry in BASE_ENDPOINT_CATALOG}
    for operation_id in _OPERATION_IDS.values():
        policy = policies[operation_id]
        assert (
            policy.route_owner,
            policy.method,
            policy.auth,
            policy.browser_client,
            policy.forwards_to_tenant,
            policy.project_role,
            policy.resolvers,
            policy.reserves_funding,
        ) == ("tenant", "GET", "session_or_pat", True, False, "viewer", (), False)

    document = export_real_compositions(tmp_path / "state")
    assert (
        document["paths"][_LINEAGE_PATH]["get"]["operationId"]
        == _OPERATION_IDS["lineage"]
    )
    assert (
        document["paths"][_SHEET_GRAPH_PATH]["get"]["operationId"]
        == _OPERATION_IDS["sheet_graph"]
    )
    assert "/api/projects/{pid}/graph/neighborhood" not in document["paths"]


@pytest.mark.parametrize(
    "model_name",
    ("ProjectLineageResponse", "SheetGraphResponse"),
)
def test_graph_lineage_responses_refuse_non_object_values(model_name: str) -> None:
    from frisket.contracts.http import graph_lineage

    model = getattr(graph_lineage, model_name)
    with pytest.raises(ValidationError):
        model.model_validate(["not", "an", "object"])


@pytest.mark.parametrize(
    ("model_name", "payload"),
    (
        (
            "ProjectLineageResponse",
            {"project_id": "p1", "op_cursor": 1, "nodes": [], "edges": None},
        ),
        (
            "ProjectLineageResponse",
            {
                "project_id": "p1",
                "op_cursor": 1,
                "nodes": [{"id": "sheet:1", "kind": "future", "name": "Cases"}],
                "edges": [],
            },
        ),
        (
            "SheetGraphResponse",
            {
                "schema_version": "frisket.sheet_graph.v1",
                "sheet_id": 7,
                "materialized_kind": "link",
                "direction": "directed",
                "nodes": [],
                "edges": [],
                "truncated": False,
                "limits": {
                    "nodes": 200,
                    "edges": 500,
                    "returned_nodes": 0,
                    "returned_edges": 0,
                },
                "diagnostics": [],
            },
        ),
        (
            "SheetGraphResponse",
            {
                "schema_version": "frisket.sheet_graph.v1",
                "sheet_id": 7,
                "materialized_kind": "edge",
                "direction": "directed",
                "nodes": [
                    {
                        "id": "1:2",
                        "label": "Case",
                        "sheet_id": 1,
                        "row_id": 2,
                        "row_ref": {"sheet_id": 1},
                        "degree": 1,
                    }
                ],
                "edges": [],
                "truncated": False,
                "limits": {
                    "nodes": 200,
                    "edges": 500,
                    "returned_nodes": 1,
                    "returned_edges": 0,
                },
                "diagnostics": [],
            },
        ),
    ),
)
def test_graph_lineage_responses_refuse_malformed_product_cores(
    model_name: str,
    payload: object,
) -> None:
    from frisket.contracts.http import graph_lineage

    model = getattr(graph_lineage, model_name)
    with pytest.raises(ValidationError):
        model.model_validate(payload)

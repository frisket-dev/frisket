"""The generated browser contract for project-wide search."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.testclient import TestClient

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.models import HttpError
from frisket.contracts.http.project_search import ProjectSearchHit, ProjectSearchHits
from frisket.server.routes.project_research import register_project_search_routes
from scripts.ci import export_web_openapi as exporter


@dataclass
class _SearchService:
    hits: list[dict[str, object]]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def search(self, pid: str, **query: object) -> list[dict[str, object]]:
        self.calls.append({"pid": pid, **query})
        return self.hits


def _route(service: _SearchService) -> APIRoute:
    app = _app(service)
    return next(route for route in app.routes if isinstance(route, APIRoute))


def _app(service: _SearchService) -> FastAPI:
    app = FastAPI()
    register_project_search_routes(app, service=service)  # type: ignore[arg-type]
    return app


def test_project_search_route_declares_its_exact_generated_wire_truth() -> None:
    route = _route(_SearchService([]))

    assert route.name == "search_ep"
    assert route.methods == {"GET"}
    assert route.path == "/api/projects/{pid}/search"
    assert route.response_model is ProjectSearchHits
    assert route.response_model_exclude_unset is True
    assert set(route.responses) == {401, 403, 404, 422, 500}
    assert all(value == {"model": HttpError} for value in route.responses.values())
    assert [parameter.name for parameter in route.dependant.query_params] == [
        "q",
        "limit",
        "mode",
        "rerank",
    ]
    assert route.dependant.query_params[0].field_info.is_required()
    assert [parameter.default for parameter in route.dependant.query_params[1:]] == [
        50,
        "keyword",
        "auto",
    ]


def test_project_search_hit_is_open_but_strict_and_sparse_optional_fields_are_non_null() -> (
    None
):
    hit = ProjectSearchHit.model_validate(
        {
            "sheet_id": 1,
            "row_id": 2,
            "column_id": 3,
            "column_name": "body",
            "ai_generated": False,
            "snip": "<b>budget</b>",
            "producer_extension": {"preserved": True},
        }
    )
    assert hit.model_dump(exclude_unset=True)["producer_extension"] == {
        "preserved": True
    }
    assert ProjectSearchHits.model_validate(
        [hit.model_dump(exclude_unset=True)]
    ).root == [hit]

    for field in ("score", "semantic", "rerank_score"):
        with pytest.raises(ValidationError):
            ProjectSearchHit.model_validate({**hit.model_dump(), field: None})
    with pytest.raises(ValidationError):
        ProjectSearchHit.model_validate({**hit.model_dump(), "sheet_id": "1"})
    with pytest.raises(ValidationError):
        ProjectSearchHits.model_validate({"hits": []})


def test_project_search_route_keeps_producer_order_and_omits_unset_fields() -> None:
    hits = [
        {
            "sheet_id": 5,
            "row_id": 7,
            "column_id": 11,
            "column_name": "body",
            "ai_generated": True,
            "snip": "first",
            "score": 0.9,
            "producer_extension": "open",
        },
        {
            "sheet_id": 5,
            "row_id": 8,
            "column_id": 11,
            "column_name": "body",
            "ai_generated": False,
            "snip": "second",
            "semantic": False,
        },
    ]
    service = _SearchService(hits)
    response = TestClient(_app(service)).get(
        "/api/projects/project/search",
        params={"q": "budget", "limit": 2, "mode": "semantic", "rerank": "on"},
    )

    assert response.status_code == 200
    assert response.json() == hits
    assert service.calls == [
        {
            "pid": "project",
            "q": "budget",
            "limit": 2,
            "mode": "semantic",
            "rerank": "on",
        }
    ]


def test_project_search_is_the_single_new_browser_projection(tmp_path) -> None:
    search_entries = [
        entry for entry in BASE_ENDPOINT_CATALOG if entry.id == "tenant.search_ep.get"
    ]
    assert len(search_entries) == 1
    assert search_entries[0].browser_client is True
    assert search_entries[0].resolvers == ()

    document = exporter.export_real_compositions(tmp_path / "state")
    assert {
        operation["operationId"]
        for operation in document["paths"]["/api/projects/{pid}/search"].values()
    } == {"tenant.search_ep.get"}

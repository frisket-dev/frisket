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
from frisket.contracts.http.project_search import ProjectSearchHit, ProjectSearchPage
from frisket.server.routes.project_research import register_project_search_routes


@dataclass
class _SearchService:
    hits: list[dict[str, object]]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def search(self, pid: str, **query: object) -> dict[str, object]:
        self.calls.append({"pid": pid, **query})
        return {"hits": self.hits, "indexing": False, "coverage": _coverage()}


def _coverage() -> dict[str, object]:
    return {
        "requested_mode": "semantic",
        "effective_modes": ["lexical"],
        "state": "partial",
        "searched_channels": ["text"],
        "is_top_k": True,
        "truncated": False,
        "channels": [
            {
                "channel": "semantic",
                "state": "unavailable",
                "recipe_id": None,
                "eligible_count": None,
                "prepared_count": None,
                "failed_count": None,
                "reason": "local model unavailable",
            }
        ],
    }


def _hit(**overrides: object) -> dict[str, object]:
    hit: dict[str, object] = {
        "sheet_id": 1,
        "row_id": 2,
        "column_id": 3,
        "column_name": "body",
        "ai_generated": False,
        "snip": "<b>budget</b>",
        "score": 0.8,
        "semantic": False,
        "source": {
            "source_id": "source:cell:2:3",
            "source_kind": "text",
            "version_kind": "retrieval_revision",
            "version_value": 4,
            "content_hash": None,
        },
        "part": {
            "part_id": "part:cell:2:3",
            "part_kind": "text",
            "producer_recipe_id": "recipe:text-v1",
        },
        "location": {
            "kind": "text",
            "precision": "source_unit",
            "text": None,
            "pages": None,
            "time": None,
            "source_span_ids": [],
        },
        "contributions": [
            {
                "method": "lexical",
                "channel": "text",
                "rank": 1,
                "representation_id": None,
                "space_id": None,
                "score": 1.0,
            }
        ],
        "additional_binding_count": 0,
    }
    hit.update(overrides)
    return hit


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
    assert route.response_model is ProjectSearchPage
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


def test_project_search_hit_is_open_but_strict_and_located() -> None:
    hit = ProjectSearchHit.model_validate(_hit(producer_extension={"preserved": True}))
    assert hit.model_dump(exclude_unset=True)["producer_extension"] == {
        "preserved": True
    }
    assert ProjectSearchPage.model_validate(
        {
            "hits": [hit.model_dump(exclude_unset=True)],
            "indexing": True,
            "coverage": _coverage(),
        }
    ).hits == [hit]

    for field in ("score", "semantic", "rerank_score"):
        with pytest.raises(ValidationError):
            ProjectSearchHit.model_validate({**hit.model_dump(), field: None})
    with pytest.raises(ValidationError):
        ProjectSearchHit.model_validate({**hit.model_dump(), "sheet_id": "1"})
    with pytest.raises(ValidationError):
        ProjectSearchPage.model_validate({"hits": [], "indexing": False})


def test_project_search_route_keeps_producer_order_and_omits_unset_fields() -> None:
    hits = [
        _hit(row_id=7, snip="first", score=0.9, producer_extension="open"),
        _hit(row_id=8, snip="second"),
    ]
    service = _SearchService(hits)
    response = TestClient(_app(service)).get(
        "/api/projects/project/search",
        params={"q": "budget", "limit": 2, "mode": "semantic", "rerank": "on"},
    )

    assert response.status_code == 200
    assert response.json() == {"hits": hits, "indexing": False, "coverage": _coverage()}
    assert service.calls == [
        {
            "pid": "project",
            "q": "budget",
            "limit": 2,
            "mode": "semantic",
            "rerank": "on",
        }
    ]


def test_project_search_is_the_single_new_browser_projection(
    real_openapi_document,
) -> None:
    search_entries = [
        entry for entry in BASE_ENDPOINT_CATALOG if entry.id == "tenant.search_ep.get"
    ]
    assert len(search_entries) == 1
    assert search_entries[0].browser_client is True
    assert search_entries[0].resolvers == ()

    assert {
        operation["operationId"]
        for operation in real_openapi_document["paths"][
            "/api/projects/{pid}/search"
        ].values()
    } == {"tenant.search_ep.get"}

"""The generated browser contract for grouped located retrieval."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.testclient import TestClient

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.models import HttpError
from frisket.contracts.http.project_search import (
    ProjectSearchRequest,
    ProjectSearchResponse,
)
from frisket.server.routes.project_research import register_project_search_routes


@dataclass
class _SearchService:
    response: dict[str, object]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def search(self, pid: str, *, request: ProjectSearchRequest) -> dict[str, object]:
        self.calls.append({"pid": pid, "request": request})
        return self.response


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
                "setup": {
                    "setup_ref": "engine-setup:embeddinggemma-2.local@1",
                    "scope": "workspace",
                },
            }
        ],
    }


def _location() -> dict[str, object]:
    return {
        "kind": "text",
        "precision": "source_unit",
        "text": None,
        "pages": None,
        "time": None,
        "source_span_ids": [],
    }


def _hit() -> dict[str, object]:
    return {
        "column_name": "body",
        "ai_generated": False,
        "snippet": "budget",
        "score": 0.8,
        "source": {
            "source_id": "cell:2:3",
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
        "binding": {
            "binding_id": "cell:2:3",
            "sheet_id": 1,
            "row_id": 2,
            "column_id": 3,
            "dependencies": [{"row_id": 2, "column_id": 3, "retrieval_revision": 4}],
        },
        "location": _location(),
        "navigation": {
            "kind": "cell",
            "sheet_id": 1,
            "row_id": 2,
            "column_id": 3,
            "text_range": None,
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


def _response() -> dict[str, object]:
    return {
        "groups": [
            {
                "group_key": "cell:2:3",
                "primary": _hit(),
                "supporting": [],
                "additional_count": 0,
            }
        ],
        "coverage": _coverage(),
    }


def _app(service: _SearchService) -> FastAPI:
    app = FastAPI()
    register_project_search_routes(app, service=service)  # type: ignore[arg-type]
    return app


def _route(service: _SearchService) -> APIRoute:
    return next(route for route in _app(service).routes if isinstance(route, APIRoute))


def _text_request() -> dict[str, object]:
    return {
        "scope": {"kind": "project"},
        "query": {"kind": "text", "text": "budget", "lexical_syntax": "fts"},
        "mode": "semantic",
        "group_by": "source",
        "limit": 12,
        "ranking": {"rerank": "on"},
    }


def test_project_search_route_is_one_typed_post_contract() -> None:
    route = _route(_SearchService(_response()))

    assert route.name == "search_ep"
    assert route.methods == {"POST"}
    assert route.path == "/api/projects/{pid}/search"
    assert route.response_model is ProjectSearchResponse
    assert route.response_model_exclude_unset is True
    assert set(route.responses) == {401, 403, 404, 422, 500}
    assert all(value == {"model": HttpError} for value in route.responses.values())
    assert [parameter.name for parameter in route.dependant.body_params] == ["request"]


def test_project_search_request_has_scoped_text_and_similarity_unions() -> None:
    text = ProjectSearchRequest.model_validate(_text_request())
    assert text.query.kind == "text"
    assert "space_id" not in ProjectSearchRequest.model_json_schema()["properties"]

    similar = ProjectSearchRequest.model_validate(
        {
            "scope": {
                "kind": "sheets",
                "sheets": [
                    {"sheet_id": 7, "row_ids": [8], "column_ids": [9]},
                    {"sheet_id": 7, "row_ids": [10], "column_ids": None},
                ],
            },
            "query": {
                "kind": "similar",
                "anchor": {
                    "kind": "cell",
                    "sheet_id": 7,
                    "row_id": 8,
                    "column_id": 9,
                },
            },
            "mode": "semantic",
            "named_index_id": "index-1",
        }
    )
    assert similar.scope.kind == "sheets"
    assert len(similar.scope.sheets) == 2
    assert similar.query.kind == "similar"

    with pytest.raises(ValidationError, match="similar searches require semantic"):
        ProjectSearchRequest.model_validate(
            {
                **_text_request(),
                "query": similar.query.model_dump(),
                "mode": "hybrid",
            }
        )


def test_project_search_response_keeps_groups_coverage_and_navigation_strict() -> None:
    response = ProjectSearchResponse.model_validate(_response())
    assert response.groups[0].primary.navigation.kind == "cell"
    assert response.coverage.channels[0].setup is not None

    bad_hit = {**_hit(), "producer_extension": "not accepted"}
    with pytest.raises(ValidationError):
        ProjectSearchResponse.model_validate(
            {
                **_response(),
                "groups": [
                    {
                        "group_key": "cell:2:3",
                        "primary": bad_hit,
                        "supporting": [],
                        "additional_count": 0,
                    }
                ],
            }
        )


def test_project_search_route_preserves_typed_body_and_group_order() -> None:
    service = _SearchService(_response())
    response = TestClient(_app(service)).post(
        "/api/projects/project/search", json=_text_request()
    )

    assert response.status_code == 200
    assert response.json() == _response()
    assert len(service.calls) == 1
    assert service.calls[0]["pid"] == "project"
    request = service.calls[0]["request"]
    assert isinstance(request, ProjectSearchRequest)
    assert request.limit == 12


def test_project_search_endpoint_catalog_exposes_only_the_post_operation(
    real_openapi_document,
) -> None:
    entries = [
        entry for entry in BASE_ENDPOINT_CATALOG if entry.route_name == "search_ep"
    ]
    assert len(entries) == 1
    assert entries[0].id == "tenant.search_ep.post"
    assert entries[0].browser_client is True
    assert {
        operation["operationId"]
        for operation in real_openapi_document["paths"][
            "/api/projects/{pid}/search"
        ].values()
    } == {"tenant.search_ep.post"}

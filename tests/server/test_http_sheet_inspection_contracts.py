"""Public HTTP contracts for sheet statistics and row location reads."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.models import ColumnStats, HttpError, SheetRowLocation
from frisket.server.routes.sheet_grid import register_sheet_grid_routes


ERROR_STATUSES = {400, 401, 403, 404, 422, 500}


class _SheetInspectionService:
    stats_payload: dict[str, Any]
    location_payload: dict[str, Any]

    def __init__(self) -> None:
        self.stats_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.location_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.stats_payload = {
            "schema_version": "frisket.column_stats.v1",
            "sheet_id": 7,
            "column": {"id": 9, "name": "score", "type": "number", "format": None},
            "row_count": 3,
            "threshold": 100_000,
            "computed": True,
            "requires_manual_analyze": False,
            "missing": 0,
            "present": 3,
            "distinct": 3,
            "top_values": [{"value": "1", "count": 1}],
            "numeric": {
                "count": 3,
                "mean": 2.5,
                "median": 2.5,
                "min": 1,
                "max": 4.5,
                "histogram": [
                    {"min": 0, "max": 2.5, "count": 1},
                    {"min": 2.5, "max": 5, "count": 2},
                ],
            },
            "text": {
                "count": 1,
                "shortest": "five",
                "shortest_length": 4,
                "longest": "five",
                "longest_length": 4,
                "mean_length": 4,
                "median_length": 4,
                "length_histogram": [{"min": 4, "max": 4, "count": 1}],
            },
            "date": None,
            "json_types": [{"type": "float", "count": 3}],
        }
        self.location_payload = {
            "schema_version": "frisket.sheet_row_location.v1",
            "sheet_id": 7,
            "row_id": 11,
            "found": False,
            "index": None,
            "page_offset": None,
            "page_size": 500,
        }

    def column_stats(self, *args: object, **kwargs: object) -> dict[str, Any]:
        self.stats_calls.append((args, kwargs))
        return self.stats_payload

    def locate_sheet_row(self, *args: object, **kwargs: object) -> dict[str, Any]:
        self.location_calls.append((args, kwargs))
        return self.location_payload


def _app(service: _SheetInspectionService | None = None) -> FastAPI:
    app = FastAPI()
    register_sheet_grid_routes(app, service=service or _SheetInspectionService())  # type: ignore[arg-type]
    return app


def test_sheet_inspection_routes_publish_strict_truthful_contracts() -> None:
    app = _app()
    routes = {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.name in {"column_stats", "locate_sheet_row"}
    }
    assert set(routes) == {"column_stats", "locate_sheet_row"}
    assert routes["column_stats"].response_model is ColumnStats
    assert routes["locate_sheet_row"].response_model is SheetRowLocation
    for route in routes.values():
        assert route.methods == {"GET"}
        assert route.response_model_exclude_unset is True
        assert set(route.responses) == ERROR_STATUSES
        assert all(value == {"model": HttpError} for value in route.responses.values())

    document = app.openapi()
    paths = document["paths"]
    stats = paths["/api/projects/{pid}/sheets/{sheet_id}/columns/{column_id}/stats"][
        "get"
    ]
    locate = paths["/api/projects/{pid}/sheets/{sheet_id}/rows/{row_id}/locate"]["get"]
    assert stats["operationId"].startswith("column_stats_")
    assert locate["operationId"].startswith("locate_sheet_row_")
    assert stats["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ColumnStats"
    }
    assert locate["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SheetRowLocation"
    }
    assert set(stats["responses"]) == {"200", *(str(code) for code in ERROR_STATUSES)}
    assert set(locate["responses"]) == {"200", *(str(code) for code in ERROR_STATUSES)}
    schemas = document["components"]["schemas"]
    for name in (
        "ColumnStats",
        "ColumnStatsColumn",
        "ColumnStatsHistogramBin",
        "ColumnStatsNumeric",
        "ColumnStatsText",
        "SheetRowLocation",
    ):
        assert schemas[name]["additionalProperties"] is False
    assert schemas["ColumnStats"]["properties"]["schema_version"]["const"] == (
        "frisket.column_stats.v1"
    )
    assert schemas["SheetRowLocation"]["properties"]["schema_version"]["const"] == (
        "frisket.sheet_row_location.v1"
    )


def test_stats_gating_omits_unset_fields_and_preserves_explicit_nulls() -> None:
    service = _SheetInspectionService()
    service.stats_payload = {
        "schema_version": "frisket.column_stats.v1",
        "sheet_id": 7,
        "column": {"id": 9, "name": "score", "type": "number", "format": None},
        "row_count": 100_001,
        "threshold": 100_000,
        "computed": False,
        "requires_manual_analyze": True,
    }
    response = TestClient(_app(service)).get(
        "/api/projects/project-1/sheets/7/columns/9/stats"
    )
    assert response.status_code == 200, response.text
    assert response.json() == service.stats_payload

    service.stats_payload = {
        **service.stats_payload,
        "computed": True,
        "requires_manual_analyze": False,
        "numeric": None,
        "text": None,
        "date": None,
        "file": None,
    }
    response = TestClient(_app(service)).get(
        "/api/projects/project-1/sheets/7/columns/9/stats"
    )
    assert response.json()["numeric"] is None
    assert response.json()["text"] is None
    assert response.json()["date"] is None
    assert response.json()["file"] is None
    assert "missing" not in response.json()

    service.stats_payload = {
        "schema_version": "frisket.column_stats.v1",
        "sheet_id": 7,
        "column": {"id": 9, "name": "doc", "type": "file", "format": None},
        "row_count": 2,
        "threshold": 100_000,
        "computed": True,
        "requires_manual_analyze": False,
        "missing": 1,
        "present": 1,
        "top_values": [],
        "numeric": None,
        "text": None,
        "date": None,
        "json_types": [],
        "file": {"count": 1, "min_size": 5, "max_size": 5},
    }
    response = TestClient(_app(service)).get(
        "/api/projects/project-1/sheets/7/columns/9/stats"
    )
    assert response.json() == service.stats_payload
    assert "distinct" not in response.json()


def test_stats_and_location_round_trip_numeric_false_and_null_bytes() -> None:
    client = TestClient(_app())
    stats = client.get("/api/projects/project-1/sheets/7/columns/9/stats")
    assert stats.status_code == 200, stats.text
    bins = stats.json()["numeric"]["histogram"]
    assert bins == [
        {"min": 0, "max": 2.5, "count": 1},
        {"min": 2.5, "max": 5, "count": 2},
    ]
    assert b'"mean":2.5,"median":2.5,"min":1,"max":4.5' in stats.content
    assert b'"mean_length":4,"median_length":4' in stats.content
    assert b'"length_histogram":[{"min":4,"max":4,"count":1}]' in stats.content

    locate = client.get("/api/projects/project-1/sheets/7/rows/11/locate")
    assert locate.status_code == 200, locate.text
    assert locate.json() == {
        "schema_version": "frisket.sheet_row_location.v1",
        "sheet_id": 7,
        "row_id": 11,
        "found": False,
        "index": None,
        "page_offset": None,
        "page_size": 500,
    }


def test_sheet_inspection_query_scope_forwards_zero_json_and_default_page_size() -> (
    None
):
    service = _SheetInspectionService()
    client = TestClient(_app(service))
    filter_json = '{"name":{"contains":"Ada"}}'
    sort_json = '[{"columnId":"9","direction":"desc"}]'

    stats = client.get(
        "/api/projects/project-1/sheets/7/columns/9/stats",
        params={
            "parent_row_id": "0",
            "filter": filter_json,
            "sort": sort_json,
        },
    )
    locate = client.get(
        "/api/projects/project-1/sheets/7/rows/11/locate",
        params={
            "parent_row_id": "0",
            "filter": filter_json,
            "sort": sort_json,
        },
    )
    assert stats.status_code == locate.status_code == 200
    assert service.stats_calls == [
        (
            ("project-1", 7, 9),
            {
                "force": False,
                "parent_row_id": 0,
                "filter_": filter_json,
                "sort": sort_json,
            },
        )
    ]
    assert service.location_calls == [
        (
            ("project-1", 7, 11),
            {
                "page_size": 500,
                "parent_row_id": 0,
                "filter_": filter_json,
                "sort": sort_json,
            },
        )
    ]

    ignored = client.get(
        "/api/projects/project-1/sheets/7/rows/11/locate",
        params={"row_ids": "11"},
    )
    assert ignored.status_code == 200
    assert service.location_calls[-1] == (
        ("project-1", 7, 11),
        {
            "page_size": 500,
            "parent_row_id": None,
            "filter_": None,
            "sort": None,
        },
    )


@pytest.mark.parametrize("model", [ColumnStats, SheetRowLocation])
def test_sheet_inspection_models_reject_unknown_fields(model: type[Any]) -> None:
    service = _SheetInspectionService()
    payload = (
        dict(service.stats_payload)
        if model is ColumnStats
        else dict(service.location_payload)
    )
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(payload)

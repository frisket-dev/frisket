"""Runtime and OpenAPI pins for the read-only resolve preview boundaries."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.routes.previews import register_preview_routes
from frisket.server.services.previews import PreviewRequestError


class _ResolvePreviewService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    def column_values_preview(self, project_id: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(("column", project_id, payload))
        if payload["input_column"] == "missing":
            raise PreviewRequestError(
                code="invalid_input_ref", message="no such column", field="input_column"
            )
        return self.responses.get("column", _VALID_RESPONSES["column"])

    def replace_rules_preview(self, project_id: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(("replace", project_id, payload))
        if payload["rules"] == "bad":
            raise PreviewRequestError(
                code="invalid_regex", message="rule does not compile", field="rules"
            )
        return self.responses.get("replace", _VALID_RESPONSES["replace"])

    def cluster_preview(self, project_id: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(("cluster", project_id, payload))
        return self.responses.get("cluster", _VALID_RESPONSES["cluster"])


def _client(
    *, raise_server_exceptions: bool = True
) -> tuple[TestClient, _ResolvePreviewService]:
    app = FastAPI()
    service = _ResolvePreviewService()
    register_preview_routes(app, service=service)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=raise_server_exceptions), service


_VALID_RESPONSES = {
    "column": {
        "schema_version": "frisket.column_values_preview.v1",
        "sheet_id": 7,
        "column_id": 11,
        "input_column": "city",
        "total_rows": 4,
        "distinct": 2,
        "missing": 1,
        "values": [{"value": "NYC", "count": 2, "producer_note": "preserved"}],
        "offset": 0,
        "limit": 500,
        "truncated": False,
        "value_hash": "sha256:column",
        "search": None,
        "distribution": None,
        "list_facet": None,
        "producer_extension": {"version": 2},
    },
    "replace": {
        "schema_version": "frisket.replace_rules_preview.v1",
        "sheet_id": 7,
        "column_id": 11,
        "total_rows": 4,
        "rule_counts": [
            {
                "index": 0,
                "matched_rows": 2,
                "matched_values": 1,
                "producer_note": "preserved",
            }
        ],
        "unmatched_rows": 1,
        "test_result": {
            "matched_rule_index": 0,
            "output": "New York",
            "producer_note": "preserved",
        },
        "value_hash": "sha256:replace",
        "producer_extension": {"version": 2},
    },
    "cluster": {
        "schema_version": "frisket.cluster_preview.v1",
        "sheet_id": 7,
        "sheet_name": "Places",
        "column_id": 11,
        "column": "city",
        "column_type": "text",
        "method": "fingerprint",
        "min_size": 2,
        "row_count": 4,
        "value_hash": "sha256:cluster",
        "count": 1,
        "clusters": [
            {
                "key": "new-york",
                "canonical": "New York",
                "size": 2,
                "values": [
                    {"value": "New York", "count": 2, "producer_note": "preserved"}
                ],
                "row_ids": [1, 2],
                "producer_note": "preserved",
            }
        ],
        "semantic": False,
        "producer_extension": {"version": 2},
    },
}


@pytest.mark.parametrize(
    ("key", "path", "body"),
    [
        (
            "column",
            "/api/projects/project-1/column-values/v1/preview",
            {"sheet_id": 7, "input_column": "city"},
        ),
        (
            "replace",
            "/api/projects/project-1/replace-rules/v1/preview",
            {"sheet_id": 7, "input_column": "city", "rules": []},
        ),
        (
            "cluster",
            "/api/projects/project-1/clusters/v1/preview",
            {"sheet_id": 7, "input_column": "city"},
        ),
    ],
)
def test_resolve_preview_response_contracts_reject_missing_and_wrong_stable_cores(
    key: str, path: str, body: dict[str, Any]
) -> None:
    client, service = _client(raise_server_exceptions=False)
    service.responses[key] = {
        "schema_version": _VALID_RESPONSES[key]["schema_version"],
        "sheet_id": "7",
    }

    response = client.post(path, json=body)

    assert response.status_code == 500


@pytest.mark.parametrize(
    ("key", "path", "body"),
    [
        (
            "column",
            "/api/projects/project-1/column-values/v1/preview",
            {"sheet_id": 7, "input_column": "city"},
        ),
        (
            "replace",
            "/api/projects/project-1/replace-rules/v1/preview",
            {"sheet_id": 7, "input_column": "city", "rules": []},
        ),
        (
            "cluster",
            "/api/projects/project-1/clusters/v1/preview",
            {"sheet_id": 7, "input_column": "city"},
        ),
    ],
)
def test_resolve_preview_response_contracts_preserve_valid_nested_and_root_extensions(
    key: str, path: str, body: dict[str, Any]
) -> None:
    client, service = _client()
    service.responses[key] = _VALID_RESPONSES[key]

    response = client.post(path, json=body)

    assert response.status_code == 200, response.text
    assert response.json() == _VALID_RESPONSES[key]


def test_resolve_preview_contracts_preserve_defaults_extras_and_bare_errors() -> None:
    client, service = _client()

    column = client.post(
        "/api/projects/project-1/column-values/v1/preview",
        json={"sheet_id": "7", "input_column": "city", "future": True},
    )
    assert column.status_code == 200, column.text
    assert service.calls[0] == (
        "column",
        "project-1",
        {
            "sheet_id": "7",
            "input_column": "city",
            "search": None,
            "limit": None,
            "offset": None,
        },
    )

    replace = client.post(
        "/api/projects/project-1/replace-rules/v1/preview",
        json={"sheet_id": 7, "input_column": "city", "rules": [], "future": True},
    )
    assert replace.status_code == 200, replace.text
    assert service.calls[1] == (
        "replace",
        "project-1",
        {
            "sheet_id": 7,
            "input_column": "city",
            "rules": [],
            "unmatched": "keep",
            "test_value": None,
        },
    )

    cluster = client.post(
        "/api/projects/project-1/clusters/v1/preview",
        json={"sheet_id": 7, "input_column": "city", "future": True},
    )
    assert cluster.status_code == 200, cluster.text
    assert service.calls[2] == (
        "cluster",
        "project-1",
        {
            "sheet_id": 7,
            "input_column": "city",
            "method": "fingerprint",
            "min_size": 2,
            "threshold": None,
            "ngram_size": None,
            "key_template": None,
        },
    )

    refused = client.post(
        "/api/projects/project-1/column-values/v1/preview",
        json={"sheet_id": 7, "input_column": "missing"},
    )
    assert refused.status_code == 400
    assert refused.json()["schema_version"] == "frisket.action_error.v1"
    assert refused.json()["code"] == "invalid_input_ref"
    assert "detail" not in refused.json()


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/project-1/column-values/v1/preview",
        "/api/projects/project-1/replace-rules/v1/preview",
        "/api/projects/project-1/clusters/v1/preview",
    ],
)
def test_resolve_preview_contracts_keep_non_objects_at_the_fastapi_422_boundary(
    path: str,
) -> None:
    client, service = _client()

    response = client.post(path, json=[])

    assert response.status_code == 422
    assert service.calls == []


def test_resolve_preview_openapi_exposes_the_three_named_operations() -> None:
    client, _service = _client()
    document = client.app.openapi()

    for path in (
        "/api/projects/{pid}/column-values/v1/preview",
        "/api/projects/{pid}/replace-rules/v1/preview",
        "/api/projects/{pid}/clusters/v1/preview",
    ):
        operation = document["paths"][path]["post"]
        assert operation["requestBody"]["required"] is True
        assert set(operation["responses"]) >= {
            "200",
            "400",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
        }


def test_resolve_preview_openapi_requires_stable_cores_and_allows_json_extensions() -> (
    None
):
    client, _service = _client()
    schemas = client.app.openapi()["components"]["schemas"]

    expected_required = {
        "ClusterPreviewResponse": {
            "schema_version",
            "sheet_id",
            "sheet_name",
            "column_id",
            "column",
            "column_type",
            "method",
            "min_size",
            "row_count",
            "value_hash",
            "count",
            "clusters",
            "semantic",
        },
        "ClusterPreviewGroup": {"key", "canonical", "size", "values", "row_ids"},
        "ClusterPreviewValue": {"value", "count"},
        "ColumnValuesPreviewResponse": {
            "schema_version",
            "sheet_id",
            "column_id",
            "input_column",
            "total_rows",
            "distinct",
            "missing",
            "values",
            "offset",
            "limit",
            "truncated",
            "value_hash",
            "search",
            "distribution",
        },
        "ColumnValueCount": {"value", "count"},
        "ReplaceRulesPreviewResponse": {
            "schema_version",
            "sheet_id",
            "column_id",
            "total_rows",
            "rule_counts",
            "unmatched_rows",
            "test_result",
            "value_hash",
        },
        "ReplaceRuleMatchCount": {"index", "matched_rows", "matched_values"},
        "ReplaceRulesTestResult": {"matched_rule_index", "output"},
    }

    for name, required in expected_required.items():
        assert set(schemas[name]["required"]) == required
        assert schemas[name]["additionalProperties"] == {
            "$ref": "#/components/schemas/JsonValue"
        }

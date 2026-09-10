"""Runtime and OpenAPI pins for the three read-only entity-mention routes."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.server.routes.previews import register_preview_routes
from frisket.server.services.previews import PreviewRequestError
from scripts.ci.export_web_openapi import export_real_compositions


_ROUTES = {
    "preview": "/api/projects/project-1/entity-mentions/v1/preview",
    "documents": "/api/projects/project-1/entity-mentions/v1/documents",
    "occurrences": "/api/projects/project-1/entity-mentions/v1/occurrences",
}

_OPENAPI_ROUTES = {
    "preview": (
        "/api/projects/{pid}/entity-mentions/v1/preview",
        "EntityMentionsPreviewRequest",
        "EntityMentionsPreviewResponse",
    ),
    "documents": (
        "/api/projects/{pid}/entity-mentions/v1/documents",
        "EntityMentionDocumentsRequest",
        "EntityMentionDocumentsResponse",
    ),
    "occurrences": (
        "/api/projects/{pid}/entity-mentions/v1/occurrences",
        "EntityMentionOccurrencesRequest",
        "EntityMentionOccurrencesResponse",
    ),
}

_OPERATION_IDS = {
    "preview": "tenant.entity_mentions_preview.post",
    "documents": "tenant.entity_mention_documents.post",
    "occurrences": "tenant.entity_mention_occurrences.post",
}


_VALID_RESPONSES = {
    "preview": {
        "schema_version": "entity-mentions-preview.v2",
        "sheet_id": 7,
        "column": {
            "id": 11,
            "name": "entities",
            "semantic_type": "entity_mentions",
            "producer_note": "preserved",
        },
        "coverage": {
            "target_rows": 10,
            "completed_rows": 9,
            "failed_rows": 1,
            "sheet_rows": 12,
            "scope_kind": "all_rows",
            "producer_note": "preserved",
        },
        "search": None,
        "type": None,
        "total_groups": 1,
        "type_totals": [
            {"type": "person", "total_groups": 1, "producer_note": "preserved"}
        ],
        "limit": 100,
        "offset": 0,
        "items": [
            {
                "type": "person",
                "selector": {
                    "kind": "fingerprint",
                    "fingerprint": "ada",
                    "producer_note": "preserved",
                },
                "label": "Ada",
                "row_count": 3,
                "mention_count": 4,
                "surface_count": 1,
                "surfaces": [
                    {
                        "text": "Ada",
                        "row_count": 3,
                        "mention_count": 4,
                        "producer_note": "preserved",
                    }
                ],
                "producer_note": "preserved",
            }
        ],
        "producer_extension": {"version": 2},
    },
    "documents": {
        "schema_version": "entity-mention-detail.v1",
        "sheet_id": 7,
        "column": {
            "id": 11,
            "name": "entities",
            "semantic_type": "entity_mentions",
            "producer_note": "preserved",
        },
        "type": "person",
        "selector": {
            "kind": "fingerprint",
            "fingerprint": "ada",
            "producer_note": "preserved",
        },
        "totals": {"documents": 1, "mentions": 4, "producer_note": "preserved"},
        "limit": 100,
        "offset": 0,
        "documents": [
            {
                "row_id": 20,
                "title": "Hearing",
                "occurrence_count": 4,
                "producer_note": "preserved",
            }
        ],
        "next_offset": None,
        "producer_extension": {"version": 2},
    },
    "occurrences": {
        "schema_version": "entity-mention-occurrences.v1",
        "sheet_id": 7,
        "row_id": 20,
        "column": {
            "id": 11,
            "name": "entities",
            "semantic_type": "entity_mentions",
            "producer_note": "preserved",
        },
        "type": "person",
        "selector": {"kind": "text", "text": "Ada", "producer_note": "preserved"},
        "snippet_radius": 32,
        "limit": 100,
        "offset": 0,
        "text_column": {"id": 4, "name": "body", "producer_note": "preserved"},
        "totals": {"occurrences": 1, "producer_note": "preserved"},
        "occurrences": [
            {
                "occurrence_id": "occ-1",
                "start": 3,
                "end": 6,
                "quote": "Ada",
                "snippet": {
                    "text": "Ada signed",
                    "mark_start": 0,
                    "mark_end": 3,
                    "truncated_start": False,
                    "truncated_end": True,
                    "producer_note": "preserved",
                },
                "producer_note": "preserved",
            }
        ],
        "next_offset": None,
        "unpositioned": {
            "reason": "content_hash_mismatch",
            "total": 1,
            "producer_note": "preserved",
        },
        "producer_extension": {"version": 2},
    },
}


class _EntityMentionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    def _result(
        self,
        route: str,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((route, project_id, payload))
        if payload["sheet_id"] == "refuse":
            raise PreviewRequestError(
                code="invalid_input_ref",
                message="no such sheet",
                field="sheet_id",
                details={"requested": "refuse"},
            )
        return self.responses.get(route, _VALID_RESPONSES[route])

    def entity_mentions_preview(
        self, project_id: str, **payload: Any
    ) -> dict[str, Any]:
        return self._result("preview", project_id, payload)

    def entity_mention_documents(
        self, project_id: str, **payload: Any
    ) -> dict[str, Any]:
        return self._result("documents", project_id, payload)

    def entity_mention_occurrences(
        self, project_id: str, **payload: Any
    ) -> dict[str, Any]:
        return self._result("occurrences", project_id, payload)


def _client(
    *, raise_server_exceptions: bool = True
) -> tuple[TestClient, _EntityMentionService]:
    app = FastAPI()
    service = _EntityMentionService()
    register_preview_routes(app, service=service)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=raise_server_exceptions), service


@pytest.mark.parametrize(
    ("route", "body", "expected"),
    [
        (
            "preview",
            {
                "sheet_id": "7",
                "column_id": True,
                "future_extension": {"kept_out_of_service": True},
            },
            {
                "sheet_id": "7",
                "column_id": True,
                "search": None,
                "type": None,
                "limit": None,
                "offset": None,
            },
        ),
        (
            "documents",
            {
                "sheet_id": "7",
                "column_id": True,
                "type": ["person"],
                "fingerprint": "ada",
                "future_extension": 1,
            },
            {
                "sheet_id": "7",
                "column_id": True,
                "type": ["person"],
                "fingerprint": "ada",
                "text": None,
                "limit": None,
                "offset": None,
            },
        ),
        (
            "occurrences",
            {
                "sheet_id": "7",
                "row_id": False,
                "column_id": [9],
                "type": {"raw": "person"},
                "text": "Ada",
                "snippet_radius": "wide",
                "future_extension": [],
            },
            {
                "sheet_id": "7",
                "row_id": False,
                "column_id": [9],
                "type": {"raw": "person"},
                "fingerprint": None,
                "text": "Ada",
                "limit": None,
                "offset": None,
                "snippet_radius": "wide",
            },
        ),
    ],
)
def test_entity_mention_requests_preserve_raw_defaults_extras_and_json(
    route: str,
    body: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    client, service = _client()

    response = client.post(_ROUTES[route], json=body)

    assert response.status_code == 200, response.text
    assert service.calls == [(route, "project-1", expected)]
    assert response.json() == _VALID_RESPONSES[route]


@pytest.mark.parametrize(
    ("route", "body", "mutate"),
    [
        (
            "preview",
            {"sheet_id": 7, "column_id": 11},
            lambda response: response.update({"coverage": {}}),
        ),
        (
            "documents",
            {"sheet_id": 7, "column_id": 11, "type": "person", "text": "Ada"},
            lambda response: response.update({"column": "not-an-object"}),
        ),
        (
            "occurrences",
            {
                "sheet_id": 7,
                "row_id": 20,
                "column_id": 11,
                "type": "person",
                "text": "Ada",
            },
            lambda response: response["unpositioned"].update(
                {"reason": "future_reason"}
            ),
        ),
    ],
)
def test_entity_mention_response_contracts_reject_malformed_stable_cores(
    route: str,
    body: dict[str, Any],
    mutate: Any,
) -> None:
    client, service = _client(raise_server_exceptions=False)
    response = deepcopy(_VALID_RESPONSES[route])
    mutate(response)
    service.responses[route] = response

    result = client.post(_ROUTES[route], json=body)

    assert result.status_code == 500


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("preview", {"sheet_id": 7, "column_id": 11}),
        (
            "documents",
            {"sheet_id": 7, "column_id": 11, "type": "person", "text": "Ada"},
        ),
        (
            "occurrences",
            {
                "sheet_id": 7,
                "row_id": 20,
                "column_id": 11,
                "type": "person",
                "text": "Ada",
            },
        ),
    ],
)
def test_entity_mention_response_contracts_preserve_nested_and_root_extensions(
    route: str, body: dict[str, Any]
) -> None:
    client, service = _client()
    service.responses[route] = _VALID_RESPONSES[route]

    response = client.post(_ROUTES[route], json=body)

    assert response.status_code == 200, response.text
    assert response.json() == _VALID_RESPONSES[route]


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_entity_mention_errors_remain_bare_service_owned_400s(route: str) -> None:
    client, _service = _client()

    response = client.post(_ROUTES[route], json={"sheet_id": "refuse"})

    assert response.status_code == 400
    assert response.json() == {
        "schema_version": "frisket.action_error.v1",
        "code": "invalid_input_ref",
        "message": "no such sheet",
        "field": "sheet_id",
        "details": {"requested": "refuse"},
        "action_kind": None,
        "needs_confirmation": False,
    }
    assert "detail" not in response.json()


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize("body", [[], "body", 3, None])
def test_entity_mention_non_objects_stay_at_fastapi_422(route: str, body: Any) -> None:
    client, service = _client()

    response = client.post(_ROUTES[route], json=body)

    assert response.status_code == 422
    assert service.calls == []


def test_entity_mention_openapi_binds_requests_responses_and_errors() -> None:
    client, _service = _client()
    document = client.app.openapi()

    for path, request_name, response_name in _OPENAPI_ROUTES.values():
        operation = document["paths"][path]["post"]
        assert operation["requestBody"]["required"] is True
        request_schema = operation["requestBody"]["content"]["application/json"][
            "schema"
        ]
        response_schema = operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert request_schema == {"$ref": f"#/components/schemas/{request_name}"}
        assert response_schema == {"$ref": f"#/components/schemas/{response_name}"}
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


def test_entity_mention_openapi_requires_stable_cores_and_allows_json_extensions() -> (
    None
):
    client, _service = _client()
    schemas = client.app.openapi()["components"]["schemas"]

    expected_required = {
        "EntityMentionsPreviewResponse": {
            "schema_version",
            "sheet_id",
            "column",
            "coverage",
            "search",
            "type",
            "total_groups",
            "type_totals",
            "limit",
            "offset",
            "items",
        },
        "EntityMentionColumn": {"id", "name", "semantic_type"},
        "EntityMentionsCoverage": {
            "target_rows",
            "completed_rows",
            "failed_rows",
            "sheet_rows",
            "scope_kind",
        },
        "EntityMentionTypeTotal": {"type", "total_groups"},
        "EntityMentionGroup": {
            "type",
            "selector",
            "label",
            "row_count",
            "mention_count",
            "surface_count",
            "surfaces",
        },
        "EntityMentionSurface": {"text", "row_count", "mention_count"},
        "EntityMentionDocumentsResponse": {
            "schema_version",
            "sheet_id",
            "column",
            "type",
            "selector",
            "totals",
            "limit",
            "offset",
            "documents",
            "next_offset",
        },
        "EntityMentionTotals": {"documents", "mentions"},
        "EntityMentionDocument": {"row_id", "title", "occurrence_count"},
        "EntityMentionOccurrencesResponse": {
            "schema_version",
            "sheet_id",
            "row_id",
            "column",
            "type",
            "selector",
            "snippet_radius",
            "limit",
            "offset",
            "text_column",
            "totals",
            "occurrences",
            "next_offset",
            "unpositioned",
        },
        "EntityMentionTextColumn": {"id", "name"},
        "EntityMentionOccurrencesTotals": {"occurrences"},
        "EntityMentionOccurrence": {
            "occurrence_id",
            "start",
            "end",
            "quote",
            "snippet",
        },
        "EntityMentionSnippet": {
            "text",
            "mark_start",
            "mark_end",
            "truncated_start",
            "truncated_end",
        },
        "EntityMentionUnpositioned": {"reason", "total"},
        "EntityMentionFingerprintSelector": {"kind", "fingerprint"},
        "EntityMentionTextSelector": {"kind", "text"},
    }

    for name, required in expected_required.items():
        assert set(schemas[name]["required"]) == required
        assert schemas[name]["additionalProperties"] == {
            "$ref": "#/components/schemas/JsonValue"
        }


def test_entity_mention_catalog_keeps_editor_auth_and_adds_browser_membership() -> None:
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
        ) == (
            "tenant",
            "POST",
            "session_or_pat",
            True,
            False,
            "editor",
            (),
            False,
        )


def test_entity_mention_real_export_adds_exactly_the_three_operations(
    tmp_path: Path,
) -> None:
    document = export_real_compositions(tmp_path / "state")

    for route, (path, _request_name, _response_name) in _OPENAPI_ROUTES.items():
        assert document["paths"][path]["post"]["operationId"] == _OPERATION_IDS[route]

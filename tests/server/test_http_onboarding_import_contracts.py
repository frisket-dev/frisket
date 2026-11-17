"""Runtime and OpenAPI coverage for the onboarding sample-seed route."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.onboarding_imports import SampleProjectSeedResponse
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.project_inspection import register_sample_project_routes
from frisket.server.services.sample_project import SampleProjectSeedError


_OPERATIONS = {
    "tenant.seed_sample_project.post": ("POST", "/api/projects/{pid}/seed-sample"),
}


class _SeedService:
    def __init__(self) -> None:
        self.project_ids: list[str] = []

    def seed(self, project_id: str) -> dict[str, Any]:
        self.project_ids.append(project_id)
        if project_id == "broken":
            raise SampleProjectSeedError("sample seed import did not produce a sheet")
        return {
            "ok": True,
            "project_id": project_id,
            "sheet_id": 7,
            "sheet_name": "Articles",
            "blank_column": "summary",
            "rows": 600,
        }


def _client() -> tuple[TestClient, _SeedService]:
    app = FastAPI()
    register_route_error_handler(app)
    seed = _SeedService()
    register_sample_project_routes(app, service=seed)  # type: ignore[arg-type]
    return TestClient(app), seed


def test_sample_seed_route_preserves_runtime_shapes() -> None:
    client, seed = _client()

    seeded = client.post("/api/projects/project-1/seed-sample")
    assert seeded.status_code == 200, seeded.text
    assert seeded.json() == {
        "ok": True,
        "project_id": "project-1",
        "sheet_id": 7,
        "sheet_name": "Articles",
        "blank_column": "summary",
        "rows": 600,
    }
    assert seed.project_ids == ["project-1"]
    failed_seed = client.post("/api/projects/broken/seed-sample")
    assert failed_seed.status_code == 500
    assert failed_seed.json() == {
        "detail": "sample seed import did not produce a sheet"
    }


def test_sample_seed_openapi_declares_exact_json_boundaries() -> None:
    client, _seed = _client()
    document = client.app.openapi()

    seed = document["paths"]["/api/projects/{pid}/seed-sample"]["post"]
    assert "requestBody" not in seed
    assert seed["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SampleProjectSeedResponse"
    }


def test_sample_seed_contract_model_round_trips() -> None:
    assert SampleProjectSeedResponse.model_validate(
        {
            "ok": True,
            "project_id": "project-1",
            "sheet_id": 1,
            "sheet_name": "Articles",
            "blank_column": "summary",
            "rows": 600,
        }
    ).model_dump() == {
        "ok": True,
        "project_id": "project-1",
        "sheet_id": 1,
        "sheet_name": "Articles",
        "blank_column": "summary",
        "rows": 600,
    }


def test_sample_seed_catalog_and_real_export_include_the_operation(
    real_openapi_document,
) -> None:
    policies = {entry.id: entry for entry in BASE_ENDPOINT_CATALOG}
    for operation_id in _OPERATIONS:
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
        ) == ("tenant", "POST", "session_or_pat", True, False, "editor", (), False)

    operations = {
        str(operation["operationId"]): (method.upper(), path)
        for path, path_item in real_openapi_document["paths"].items()
        for method, operation in path_item.items()
    }
    assert {
        operation_id: operations[operation_id] for operation_id in _OPERATIONS
    } == _OPERATIONS

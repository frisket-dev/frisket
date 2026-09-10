"""Runtime and OpenAPI coverage for onboarding and JSON import routes."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.onboarding_imports import (
    ImportPasteDraftBody,
    ImportPasteDraftResponse,
    ImportPasteConfirmBody,
    ImportPasteConfirmResponse,
    ImportUrlsBody,
    ImportUrlsResponse,
    SampleProjectSeedResponse,
)
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.imports import (
    register_import_draft_routes,
    register_import_urls_routes,
)
from frisket.server.routes.project_inspection import register_sample_project_routes
from frisket.server.services.import_drafts import (
    ImportDraftConfirmResponse,
    ImportDraftRouteError,
)
from frisket.server.services.import_urls import (
    ImportUrlsResponse as ServiceUrlsResponse,
)
from frisket.server.services.sample_project import SampleProjectSeedError
from scripts.ci.export_web_openapi import export_real_compositions


_OPERATIONS = {
    "tenant.seed_sample_project.post": ("POST", "/api/projects/{pid}/seed-sample"),
    "tenant.import_paste_draft.post": (
        "POST",
        "/api/projects/{pid}/import/drafts/paste",
    ),
    "tenant.import_paste_confirm.post": (
        "POST",
        "/api/projects/{pid}/import/drafts/paste/confirm",
    ),
    "tenant.import_urls.post": ("POST", "/api/projects/{pid}/import/urls"),
    "tenant.import_update_preview.post": (
        "POST",
        "/api/projects/{pid}/import/rows/update/preview",
    ),
    "tenant.import_csv_update_preview.post": (
        "POST",
        "/api/projects/{pid}/import/csv/update/preview",
    ),
    "tenant.update_csv.post": ("POST", "/api/projects/{pid}/import/csv/update"),
    "tenant.import_xlsx_update_preview.post": (
        "POST",
        "/api/projects/{pid}/import/xlsx/update/preview",
    ),
    "tenant.update_xlsx.post": ("POST", "/api/projects/{pid}/import/xlsx/update"),
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


class _PasteService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def paste_draft(self, project_id: str, raw: str) -> dict[str, Any]:
        self.calls.append((project_id, raw))
        if raw == "refuse":
            raise ImportDraftRouteError(400, "paste at least one data row")
        return {
            "schema_version": "frisket.import_draft.v2",
            "draft_id": "paste@sha256:test",
            "source_kind": "paste",
            "sheet_name": "Pasted rows",
            "row_count": 1,
            "columns": [
                {
                    "key": "name",
                    "name": "name",
                    "type": "text",
                    "include": True,
                    "sample_values": ["Ada"],
                    "producer_extension": {"kept": True},
                }
            ],
            "preview_rows": [{"name": "Ada", "future": [True, None]}],
            "warnings": [],
            "source": {
                "kind": "inline",
                "label": "pasted table",
                "fingerprint": "sha256:test",
                "line_count": 2,
                "extension": {"kept": True},
            },
            "producer_extension": {"kept": True},
        }

    def executor_deps_for_request(self, _project_id: str, _request: Any) -> Any:
        return None

    def confirm(
        self, project_id: str, body: ImportPasteConfirmBody, *, deps: Any = None
    ) -> ImportDraftConfirmResponse:
        assert deps is None
        return ImportDraftConfirmResponse(
            200,
            {
                "sheet_id": 7,
                "rows": 1,
                "columns": [column.name for column in body.columns if column.name],
            },
        )


class _UrlsService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], str | None, str]] = []

    async def import_urls(
        self,
        project_id: str,
        *,
        request: Any,
        urls: list[str],
        sheet_name: str | None,
        column: str,
    ) -> ServiceUrlsResponse:
        self.calls.append((project_id, urls, sheet_name, column))
        if project_id == "needs-confirmation":
            return ServiceUrlsResponse(
                status_code=402,
                force_json_response=True,
                payload={
                    "schema_version": "frisket.action_result.v1",
                    "action": {"kind": "import.urls", "action_id": "url-1"},
                    "status": "needs_confirmation",
                    "project_id": project_id,
                    "errors": [],
                },
            )
        return ServiceUrlsResponse(
            status_code=200,
            payload={"sheet_id": 9, "rows": 2, "downloaded": 1, "failed": 1},
        )


def _client() -> tuple[TestClient, _SeedService, _PasteService, _UrlsService]:
    app = FastAPI()
    register_route_error_handler(app)
    seed = _SeedService()
    paste = _PasteService()
    urls = _UrlsService()
    register_sample_project_routes(app, service=seed)  # type: ignore[arg-type]
    register_import_draft_routes(app, service=paste)  # type: ignore[arg-type]
    register_import_urls_routes(app, service=urls)  # type: ignore[arg-type]
    return TestClient(app), seed, paste, urls


def test_onboarding_import_routes_preserve_runtime_shapes_and_json_defaults() -> None:
    client, seed, paste, urls = _client()

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

    draft = client.post(
        "/api/projects/project-1/import/drafts/paste",
        json={"raw": "name\nAda\n"},
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["preview_rows"] == [{"name": "Ada", "future": [True, None]}]
    assert draft.json()["producer_extension"] == {"kept": True}
    assert draft.json()["columns"][0]["producer_extension"] == {"kept": True}
    assert "format" not in draft.json()["columns"][0]
    assert draft.json()["source"]["extension"] == {"kept": True}
    assert paste.calls == [("project-1", "name\nAda\n")]
    refused = client.post(
        "/api/projects/project-1/import/drafts/paste", json={"raw": "refuse"}
    )
    assert refused.status_code == 400
    assert refused.json() == {"detail": "paste at least one data row"}

    confirmed = client.post(
        "/api/projects/project-1/import/drafts/paste/confirm",
        json={
            "raw": "name\nAda\n",
            "draft_id": "paste@sha256:test",
            "columns": [{"source_name": "name", "name": "name", "type": "text"}],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"sheet_id": 7, "rows": 1, "columns": ["name"]}

    imported = client.post(
        "/api/projects/project-1/import/urls",
        json={"urls": ["https://example.test/a.mp3", ""]},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"sheet_id": 9, "rows": 2, "downloaded": 1, "failed": 1}
    assert urls.calls == [
        ("project-1", ["https://example.test/a.mp3", ""], None, "media")
    ]

    confirmation = client.post(
        "/api/projects/needs-confirmation/import/urls",
        json={"urls": ["https://example.test/a.mp3"]},
    )
    assert confirmation.status_code == 402
    assert confirmation.json()["schema_version"] == "frisket.action_result.v1"
    assert "detail" not in confirmation.json()


def test_onboarding_import_request_boundaries_preserve_validation_and_defaults() -> (
    None
):
    client, _seed, paste, urls = _client()

    blank = client.post("/api/projects/project-1/import/drafts/paste", json={"raw": ""})
    assert blank.status_code == 422
    assert paste.calls == []

    at_limit = client.post(
        "/api/projects/project-1/import/drafts/paste",
        json={"raw": "x" * 2_000_000},
    )
    assert at_limit.status_code == 200, at_limit.text
    assert paste.calls == [("project-1", "x" * 2_000_000)]

    over_limit = client.post(
        "/api/projects/project-1/import/drafts/paste",
        json={"raw": "x" * 2_000_001},
    )
    assert over_limit.status_code == 422
    assert paste.calls == [("project-1", "x" * 2_000_000)]

    missing_urls = client.post("/api/projects/project-1/import/urls", json={})
    assert missing_urls.status_code == 422
    assert urls.calls == []


def test_onboarding_import_openapi_declares_exact_json_boundaries() -> None:
    client, _seed, _paste, _urls = _client()
    document = client.app.openapi()

    seed = document["paths"]["/api/projects/{pid}/seed-sample"]["post"]
    assert "requestBody" not in seed
    assert seed["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SampleProjectSeedResponse"
    }

    paste = document["paths"]["/api/projects/{pid}/import/drafts/paste"]["post"]
    assert paste["requestBody"]["required"] is True
    assert paste["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportPasteDraftResponse"
    }
    assert paste["responses"]["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HttpError"
    }
    assert paste["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportPasteDraftBody"
    }
    assert document["components"]["schemas"]["ImportPasteDraftResponse"][
        "required"
    ] == [
        "schema_version",
        "draft_id",
        "source_kind",
        "sheet_name",
        "row_count",
        "columns",
        "preview_rows",
        "warnings",
        "source",
    ]
    assert (
        document["components"]["schemas"]["ImportPasteDraftResponse"][
            "additionalProperties"
        ]
        is True
    )

    confirm = document["paths"]["/api/projects/{pid}/import/drafts/paste/confirm"][
        "post"
    ]
    assert confirm["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportPasteConfirmBody"
    }
    assert confirm["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportPasteConfirmResponse"
    }

    urls = document["paths"]["/api/projects/{pid}/import/urls"]["post"]
    assert urls["requestBody"]["required"] is True
    assert urls["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportUrlsBody"
    }
    assert urls["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportUrlsResponse"
    }
    for status in ("400", "500"):
        assert (
            "anyOf"
            in urls["responses"][status]["content"]["application/json"]["schema"]
        )
    for status in ("402", "409"):
        assert urls["responses"][status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ActionResult"
        }
    assert urls["x-frisket-v1-transport"]["action_kind"] == "import.urls"


def test_onboarding_import_contract_models_preserve_producer_owned_leaves_and_defaults() -> (
    None
):
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
    assert ImportPasteDraftResponse.model_validate(
        {
            "schema_version": "frisket.import_draft.v2",
            "draft_id": "paste@sha256:test",
            "source_kind": "paste",
            "sheet_name": "Pasted rows",
            "row_count": 1,
            "columns": [
                {
                    "key": "name",
                    "name": "name",
                    "type": "text",
                    "include": True,
                    "sample_values": ["Ada"],
                    "extension": {"recursive": [1, None, True]},
                }
            ],
            "preview_rows": [{"name": "Ada", "future": [True, None]}],
            "warnings": [],
            "source": {
                "kind": "inline",
                "label": "pasted table",
                "fingerprint": "sha256:test",
                "line_count": 2,
                "extension": {"recursive": [1, None, True]},
            },
            "extension": {"recursive": [1, None, True]},
        }
    ).model_dump() == {
        "schema_version": "frisket.import_draft.v2",
        "draft_id": "paste@sha256:test",
        "source_kind": "paste",
        "sheet_name": "Pasted rows",
        "row_count": 1,
        "columns": [
            {
                "key": "name",
                "name": "name",
                "type": "text",
                "include": True,
                "sample_values": ["Ada"],
                "format": None,
                "extension": {"recursive": [1, None, True]},
            }
        ],
        "preview_rows": [{"name": "Ada", "future": [True, None]}],
        "warnings": [],
        "source": {
            "kind": "inline",
            "label": "pasted table",
            "fingerprint": "sha256:test",
            "line_count": 2,
            "extension": {"recursive": [1, None, True]},
        },
        "extension": {"recursive": [1, None, True]},
    }
    assert ImportPasteDraftBody(raw="x" * 2_000_000).raw == "x" * 2_000_000
    assert (
        ImportPasteConfirmBody(
            raw="name\nAda\n",
            draft_id="paste@sha256:test",
            columns=[{"source_name": "name", "name": "name", "type": "text"}],
        ).sheet_name
        is None
    )
    assert ImportPasteConfirmResponse(
        sheet_id=1, rows=1, columns=["name"]
    ).model_dump() == {
        "sheet_id": 1,
        "rows": 1,
        "columns": ["name"],
    }
    assert ImportUrlsBody(urls=["https://example.test/a"]).model_dump() == {
        "urls": ["https://example.test/a"],
        "sheet_name": None,
        "column": "media",
    }
    assert ImportUrlsResponse.model_validate(
        {"sheet_id": 1, "rows": 2, "downloaded": 1, "failed": 0}
    ).model_dump() == {"sheet_id": 1, "rows": 2, "downloaded": 1, "failed": 0}


def test_onboarding_import_catalog_and_real_export_include_import_operations(
    tmp_path,
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

    document = export_real_compositions(tmp_path / "state")
    operations = {
        str(operation["operationId"]): (method.upper(), path)
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
    }
    assert {
        operation_id: operations[operation_id] for operation_id in _OPERATIONS
    } == _OPERATIONS

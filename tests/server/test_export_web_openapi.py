"""HTTP-02's exporter matrix plus HTTP-05's truthful implicit-422 class."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import Cookie, Depends, FastAPI, Header, Path as ApiPath, Query
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from pydantic import AfterValidator, BaseModel

from frisket.contracts.http.endpoint_catalog import (
    BASE_ENDPOINT_CATALOG,
    EndpointPolicy,
)
from scripts.ci import export_web_openapi as exporter


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "export_web_openapi.py"


class Payload(BaseModel):
    value: str


class LocalShape(BaseModel):
    local_value: str


class TeamShape(BaseModel):
    team_value: int


def _reject_reserved_path(value: str) -> str:
    if value == "reserved":
        raise ValueError("reserved path value")
    return value


def _add_json_route(
    app: FastAPI,
    path: str,
    name: str,
    *,
    method: str = "GET",
    response_model: Any = Payload,
) -> None:
    async def handler() -> dict[str, Any]:
        return {"value": "ok"}

    app.add_api_route(
        path,
        handler,
        methods=[method],
        name=name,
        response_model=response_model,
    )


def _policy(
    owner: str,
    name: str,
    method: str,
    *,
    operation_id: str | None = None,
    member: bool = True,
) -> EndpointPolicy:
    return EndpointPolicy(
        id=operation_id or f"{owner}.{name}.{method.lower()}",
        route_owner=owner,  # type: ignore[arg-type]
        route_name=name,
        method=method,
        auth="public",
        browser_client=member,
    )


def _operation_ids(document: Mapping[str, Any]) -> set[str]:
    return {
        str(operation["operationId"])
        for path_item in document["paths"].values()
        for operation in path_item.values()
    }


def test_run_provenance_reads_are_exported_from_typed_response_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = exporter.export_real_compositions(tmp_path / "state")
    row = _operation(document, "tenant.action_run_trace_row.get")
    provenance = _operation(document, "tenant.provenance.get")
    assert row["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RunTraceRowEvidence"
    }
    assert provenance["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProvenanceManifest"
    }
    assert row["x-frisket-editions"] == ["local", "team"]
    assert provenance["x-frisket-editions"] == ["local", "team"]


def test_runtime_projection_browser_operations_have_typed_successes_only(
    tmp_path: Path,
) -> None:
    document = exporter.export_real_compositions(tmp_path / "state")

    # These fixed upload bridges retain native multipart bodies at the browser
    # boundary; all other projected operations retain JSON transport.
    for operation_id, response_name in (
        ("tenant.import_csv.post", "ImportCsvResponse"),
        ("tenant.import_csv_preview.post", "ImportCsvPreviewResponse"),
        ("tenant.import_xlsx.post", "ImportXlsxResponse"),
        ("tenant.import_pdf.post", "ImportPdfResponse"),
        ("tenant.import_files.post", "ImportFilesResponse"),
    ):
        imported = _operation(document, operation_id)
        assert imported["x-frisket-editions"] == ["local", "team"]
        assert imported["requestBody"]["required"] is True
        assert set(imported["requestBody"]["content"]) == {"multipart/form-data"}
        assert imported["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": f"#/components/schemas/{response_name}"}
    for operation_id, response_name, success_status in (
        ("tenant.ocr_compare_scratch.post", "ActionPreviewStartResponse", "202"),
        (
            "tenant.ocr_compare_scratch_estimate.post",
            "OcrCompareScratchEstimateResponse",
            "200",
        ),
        (
            "tenant.transcribe_compare_scratch.post",
            "ActionPreviewStartResponse",
            "202",
        ),
        (
            "tenant.transcribe_compare_scratch_estimate.post",
            "TranscribeCompareScratchEstimateResponse",
            "200",
        ),
        (
            "tenant.topic_segmentation_compare_scratch.post",
            "TopicSegmentationCompareScratchResponse",
            "200",
        ),
    ):
        scratch = _operation(document, operation_id)
        assert scratch["x-frisket-editions"] == ["local", "team"]
        assert scratch["requestBody"]["required"] is True
        assert set(scratch["requestBody"]["content"]) == {"multipart/form-data"}
        assert scratch["responses"][success_status]["content"]["application/json"][
            "schema"
        ] == {"$ref": f"#/components/schemas/{response_name}"}
        error_name = (
            "ActionPreviewErrorResponse" if success_status == "202" else "ActionError"
        )
        assert scratch["responses"]["400"]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{error_name}"
        }
        expected_responses = {
            success_status,
            "400",
            "401",
            "403",
            "404",
            "422",
            "500",
        }
        if success_status == "202":
            expected_responses.add("402")
        assert set(scratch["responses"]) == expected_responses
        for status in ("401", "403", "404", "500"):
            assert scratch["responses"][status]["content"]["application/json"][
                "schema"
            ] == {"$ref": "#/components/schemas/HttpError"}
        validation_name = (
            "HttpError"
            if operation_id.startswith(
                ("tenant.ocr_compare_scratch", "tenant.transcribe_compare_scratch")
            )
            else "HTTPValidationError"
        )
        assert scratch["responses"]["422"]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{validation_name}"
        }
    for operation_id, response_name in (
        ("tenant.runtime_projection_status.post", "RuntimeProjectionStatus"),
        ("tenant.runtime_projection_build.post", "RuntimeProjectionBuildPlan"),
        (
            "tenant.runtime_projection_artifact.post",
            "RuntimeProjectionArtifactResponse",
        ),
    ):
        operation = _operation(document, operation_id)
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": f"#/components/schemas/{response_name}"}
        assert operation["x-frisket-editions"] == ["local", "team"]
    spend = _operation(document, "tenant.spend.get")
    assert spend["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SpendReport"
    }
    assert spend["x-frisket-editions"] == ["local", "team"]


def test_http_completion_batch_one_projects_five_typed_browser_operations(
    tmp_path: Path,
) -> None:
    document = exporter.export_real_compositions(tmp_path / "state")

    expected = {
        "outer.list_org_env_vars.get": (
            ["team"],
            "OrganizationEnvList",
            None,
            {"200", "401", "403", "500"},
        ),
        "outer.set_org_env_var.post": (
            ["team"],
            "OrganizationEnvSave",
            "OrganizationEnvSaveRequest",
            {"200", "400", "401", "403", "422", "500"},
        ),
        "outer.delete_org_env_var.delete": (
            ["team"],
            "OrganizationEnvDelete",
            None,
            {"200", "400", "401", "403", "500"},
        ),
        "outer.org_media_proxy_status.get": (
            ["team"],
            "OrganizationMediaProxyStatus",
            None,
            {"200", "401", "403", "500"},
        ),
        "tenant.project_attempts.get": (
            ["local", "team"],
            "ProjectAttemptsPage",
            None,
            {"200", "401", "403", "404", "422", "500"},
        ),
    }
    for operation_id, (
        editions,
        response_name,
        request_name,
        statuses,
    ) in expected.items():
        operation = _operation(document, operation_id)
        assert operation["x-frisket-editions"] == editions
        assert set(operation["responses"]) == statuses
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": f"#/components/schemas/{response_name}"}
        if request_name is None:
            assert "requestBody" not in operation
        else:
            assert operation["requestBody"] == {
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{request_name}"}
                    }
                },
                "required": True,
            }

    attempts = _operation(document, "tenant.project_attempts.get")
    assert attempts["parameters"] == [
        {
            "in": "path",
            "name": "pid",
            "required": True,
            "schema": {"title": "Pid", "type": "string"},
        },
        {
            "in": "query",
            "name": "run_id",
            "required": False,
            "schema": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "title": "Run Id",
            },
        },
        {
            "in": "query",
            "name": "offset",
            "required": False,
            "schema": {
                "default": 0,
                "minimum": 0,
                "title": "Offset",
                "type": "integer",
            },
        },
        {
            "in": "query",
            "name": "limit",
            "required": False,
            "schema": {
                "default": 25,
                "maximum": 100,
                "minimum": 1,
                "title": "Limit",
                "type": "integer",
            },
        },
    ]


def _operation(document: Mapping[str, Any], operation_id: str) -> Mapping[str, Any]:
    return next(
        operation
        for path_item in document["paths"].values()
        for operation in path_item.values()
        if operation["operationId"] == operation_id
    )


def _case_local_team_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import frisket.team.app as team_app

    def refuse_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("composition construction attempted network access")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    document = exporter.export_real_compositions(tmp_path / "state")
    members = [entry for entry in BASE_ENDPOINT_CATALOG if entry.browser_client]
    team_local_models = {
        entry.id
        for entry in team_app._TEAM_LOCAL_MODEL_DECLARATIONS
        if entry.browser_client
    }
    team_browser_auth = {
        entry.id
        for entry in team_app._TEAM_BROWSER_AUTH_DECLARATIONS
        if entry.browser_client
    }
    mcp_local_only = {
        "tenant.create_mcp_server.post",
        "tenant.delete_mcp_server.delete",
        "tenant.import_mcp_servers.post",
        "tenant.list_mcp_servers.get",
        "tenant.test_mcp_server.post",
        "tenant.update_mcp_server.patch",
    }
    assert {
        "tenant.list_sources.get",
        "tenant.get_source_ep.get",
        "tenant.get_source_health_ep.get",
        "tenant.list_views.get",
        "tenant.create_view.post",
        "tenant.patch_view.patch",
        "tenant.replace_view_definition.put",
        "tenant.delete_view_ep.delete",
        "tenant.list_lenses.get",
        "tenant.create_lens.post",
        "tenant.resolve_lens.get",
        "tenant.list_watches.get",
        "tenant.create_watch.post",
        "tenant.patch_watch.patch",
        "tenant.delete_watch.delete",
        "tenant.run_watch.post",
        "tenant.list_watch_runs.get",
        "tenant.list_watch_run_events.get",
        "tenant.list_notifications.get",
        "tenant.notifications_summary.get",
        "tenant.mark_notifications_seen.post",
        "tenant.mark_notification_read.post",
        "tenant.ack_notification.post",
        "tenant.unack_notification.post",
    } <= _operation_ids(document)
    assert mcp_local_only <= {entry.id for entry in members}
    assert mcp_local_only <= set(exporter.CANONICAL_OPERATION_IDS.values())
    assert mcp_local_only <= _operation_ids(document)
    assert "tenant.import_followthemoney.post" in _operation_ids(document)
    assert "tenant.import_paste_confirm.post" in _operation_ids(document)
    assert len(BASE_ENDPOINT_CATALOG) == 267
    assert len(members) == 201
    assert len(team_local_models) == 5
    assert team_browser_auth == {
        "outer.accept_project_invite.post",
        "outer.complete_magic_link.post",
        "outer.password_login.post",
        "outer.request_link.post",
        "outer.logout.post",
    }
    assert len(exporter.CANONICAL_OPERATION_IDS) == 210
    assert len(_operation_ids(document)) == 211

    discovery = _operation(document, "tenant.discover_local_endpoints.post")
    assert discovery["x-frisket-editions"] == ["local"]
    assert "requestBody" not in discovery
    assert set(discovery["responses"]) == {"200", "400", "500"}
    assert discovery["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/LocalEndpointDiscoveryResponse"
    }

    local_only = {
        "tenant.create_project.post",
        "tenant.delete_project.delete",
        "tenant.list_projects.get",
        "tenant.cancel_model_pull.post",
        "tenant.create_local_endpoint.post",
        "tenant.discover_local_endpoints.post",
        "tenant.delete_local_endpoint.delete",
        "tenant.delete_provider_key.delete",
        "tenant.get_model_pull.get",
        "tenant.list_model_pulls.get",
        "tenant.list_providers.get",
        "tenant.provider_status.get",
        "tenant.pull_artifact.post",
        "tenant.set_provider_key.put",
        "tenant.uninstall_artifact.post",
        "tenant.update_runtime_config.patch",
        "tenant.update_local_endpoint.patch",
        "tenant.validate_provider_key.post",
        *mcp_local_only,
    }
    team_only = {
        "outer.admin_overview.get",
        "outer.admin_browser_audit.get",
        "outer.admin_browser_cancel_job.post",
        "outer.admin_browser_errors.get",
        "outer.admin_browser_health.get",
        "outer.admin_browser_invite_user.post",
        "outer.admin_browser_jobs.get",
        "outer.admin_browser_remove_user.delete",
        "outer.admin_browser_revoke_invite.delete",
        "outer.admin_browser_update_user_role.patch",
        "outer.admin_browser_users.get",
        "outer.client_errors.post",
        "outer.create_token.post",
        "outer.diagnostic_bundle.post",
        "outer.instance_info.get",
        "outer.list_oauth_connections.get",
        "outer.me.get",
        "outer.update_profile.patch",
        "outer.list_org_keys.get",
        "outer.list_org_env_vars.get",
        "outer.list_tokens.get",
        "outer.revoke_token.delete",
        "outer.set_org_key.post",
        "outer.set_org_env_var.post",
        "outer.validate_org_key.post",
        "outer.delete_org_key.delete",
        "outer.delete_org_env_var.delete",
        "outer.org_media_proxy_status.get",
        "outer.list_members.get",
        "outer.set_member.post",
        "outer.remove_member.delete",
        "outer.list_project_invites.get",
        "outer.create_project_invite.post",
        "outer.revoke_project_invite.delete",
        *team_local_models,
        *team_browser_auth,
    }
    for operation_id in _operation_ids(document):
        editions = _operation(document, operation_id)["x-frisket-editions"]
        assert editions == (
            ["local"]
            if operation_id in local_only
            else ["team"]
            if operation_id in team_only
            else ["local", "team"]
        )
    assert {
        operation_id
        for operation_id in _operation_ids(document)
        if _operation(document, operation_id)["x-frisket-editions"] == ["team"]
    } == team_only
    assert "outer.post_org_models_pull.post" in _operation_ids(document)

    # Browser administration is a shared catalog member but only the team
    # composition registers these routes. Pin the public wire subset without
    # coupling this contract to an unrelated total operation count.
    browser_admin = {
        "outer.admin_browser_audit.get": (
            "GET",
            "/api/admin/browser/audit",
            {"200", "401", "403", "422", "500"},
        ),
        "outer.admin_browser_cancel_job.post": (
            "POST",
            "/api/admin/browser/jobs/{job_id}/cancel",
            {"200", "401", "403", "404", "409", "422", "500"},
        ),
        "outer.admin_browser_errors.get": (
            "GET",
            "/api/admin/browser/errors",
            {"200", "401", "403", "422", "500"},
        ),
        "outer.admin_browser_health.get": (
            "GET",
            "/api/admin/browser/health",
            {"200", "401", "403", "500"},
        ),
        "outer.admin_browser_invite_user.post": (
            "POST",
            "/api/admin/browser/users/invite",
            {"200", "400", "401", "403", "404", "409", "422", "500"},
        ),
        "outer.admin_browser_jobs.get": (
            "GET",
            "/api/admin/browser/jobs",
            {"200", "401", "403", "500"},
        ),
        "outer.admin_browser_remove_user.delete": (
            "DELETE",
            "/api/admin/browser/users/{user_ref}",
            {"200", "401", "403", "404", "409", "422", "500"},
        ),
        "outer.admin_browser_revoke_invite.delete": (
            "DELETE",
            "/api/admin/browser/users/invites/{email}",
            {"200", "401", "403", "404", "422", "500"},
        ),
        "outer.admin_browser_update_user_role.patch": (
            "PATCH",
            "/api/admin/browser/users/{user_id}/role",
            {"200", "400", "401", "403", "404", "409", "422", "500"},
        ),
        "outer.admin_browser_users.get": (
            "GET",
            "/api/admin/browser/users",
            {"200", "401", "403", "500"},
        ),
    }
    for operation_id, (method, path, statuses) in browser_admin.items():
        operation = _operation(document, operation_id)
        assert document["paths"][path][method.lower()] == operation
        assert set(operation["responses"]) == statuses
        assert operation["x-frisket-editions"] == ["team"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]
    for operation_id in {
        "outer.admin_browser_invite_user.post",
        "outer.admin_browser_update_user_role.patch",
    }:
        assert _operation(document, operation_id)["requestBody"]["content"][
            "application/json"
        ]["schema"]

    # The real exporter must consume team/app's edition-local declarations,
    # not a second hard-coded roster. Toggling one source declaration changes
    # its projection without touching BASE_ENDPOINT_CATALOG.
    omitted = next(
        entry
        for entry in team_app._TEAM_LOCAL_MODEL_DECLARATIONS
        if entry.browser_client
    )
    monkeypatch.setattr(
        team_app,
        "_TEAM_LOCAL_MODEL_DECLARATIONS",
        tuple(
            replace(entry, browser_client=False) if entry == omitted else entry
            for entry in team_app._TEAM_LOCAL_MODEL_DECLARATIONS
        ),
    )
    source_without_one = team_local_models - {omitted.id}
    document_without_one = exporter.export_real_compositions(tmp_path / "source-only")
    canonical_base_members = {
        exporter.CANONICAL_OPERATION_IDS.get(
            (entry.route_owner, entry.route_name, entry.method.upper()), entry.id
        )
        for entry in members
    }
    assert (
        _operation_ids(document_without_one)
        == canonical_base_members | source_without_one | team_browser_auth
    )
    assert "outer.create_project.post" not in _operation_ids(document)
    assert "outer.list_projects.get" not in _operation_ids(document)
    assert {
        "tenant.provider_catalog.get",
        "outer.list_org_keys.get",
        "outer.set_org_key.post",
        "outer.validate_org_key.post",
        "outer.delete_org_key.delete",
    } <= _operation_ids(document)
    assert (
        "422"
        not in _operation(document, "tenant.project_v1_action_catalog.get")["responses"]
    )
    assert "422" in _operation(document, "tenant.action_job_detail.get")["responses"]
    explicit = _operation(document, "tenant.create_project.post")["responses"]["422"]
    assert explicit["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HttpError"
    }


def _case_implicit_422_truth(_tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()

    async def plain_string_path(pid: str) -> dict[str, str]:
        return {"value": pid}

    async def integer_path(item_id: int) -> dict[str, str]:
        return {"value": str(item_id)}

    async def constrained_string_path(
        pid: Annotated[str, ApiPath(min_length=2)],
    ) -> dict[str, str]:
        return {"value": pid}

    async def validated_string_path(
        pid: Annotated[str, AfterValidator(_reject_reserved_path)],
    ) -> dict[str, str]:
        return {"value": pid}

    async def body_input(payload: Payload) -> dict[str, str]:
        return {"value": payload.value}

    async def dependency_value() -> str:
        return "dependency"

    async def dependency_input(
        pid: str,
        _dependency: str = Depends(dependency_value),
    ) -> dict[str, str]:
        return {"value": pid}

    async def query_input(
        pid: str,
        limit: Annotated[int, Query(ge=1)],
    ) -> dict[str, str]:
        return {"value": f"{pid}:{limit}"}

    async def header_input(
        pid: str,
        token: Annotated[str, Header()],
    ) -> dict[str, str]:
        return {"value": f"{pid}:{token}"}

    async def cookie_input(
        pid: str,
        session: Annotated[str, Cookie()],
    ) -> dict[str, str]:
        return {"value": f"{pid}:{session}"}

    routes: tuple[tuple[str, str, Any, dict[int, dict[str, Any]] | None], ...] = (
        ("/plain/{pid}", "plain_string_path", plain_string_path, None),
        ("/integer/{item_id}", "integer_path", integer_path, None),
        (
            "/constrained/{pid}",
            "constrained_string_path",
            constrained_string_path,
            None,
        ),
        (
            "/validated/{pid}",
            "validated_string_path",
            validated_string_path,
            None,
        ),
        ("/body", "body_input", body_input, None),
        ("/dependency/{pid}", "dependency_input", dependency_input, None),
        ("/query/{pid}", "query_input", query_input, None),
        ("/header/{pid}", "header_input", header_input, None),
        ("/cookie/{pid}", "cookie_input", cookie_input, None),
        (
            "/explicit/{pid}",
            "explicit_422",
            plain_string_path,
            {422: {"model": Payload}},
        ),
    )
    for path, name, endpoint, responses in routes:
        app.add_api_route(
            path,
            endpoint,
            methods=["POST" if name == "body_input" else "GET"],
            name=name,
            response_model=Payload,
            responses=responses,
        )
    document = exporter.export_compositions(
        [exporter.Composition("local", app, "tenant")],
        policies=tuple(
            _policy("tenant", name, "POST" if name == "body_input" else "GET")
            for path, name, _endpoint, _responses in routes
        ),
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )

    assert (
        "422" not in _operation(document, "tenant.plain_string_path.get")["responses"]
    )
    assert TestClient(app).get("/validated/reserved").status_code == 422
    for name in (
        "integer_path",
        "constrained_string_path",
        "validated_string_path",
        "body_input",
        "dependency_input",
        "query_input",
        "header_input",
        "cookie_input",
        "explicit_422",
    ):
        method = "post" if name == "body_input" else "get"
        assert "422" in _operation(document, f"tenant.{name}.{method}")["responses"]
    explicit = _operation(document, "tenant.explicit_422.get")["responses"]["422"]
    assert explicit["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Payload"
    }


def _case_mount_owner_labeling(
    _tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = FastAPI()
    tenant = FastAPI()
    _add_json_route(outer, "/outer", "outer_only")
    _add_json_route(tenant, "/mounted", "mounted_core")
    outer.mount("/tenant-prefix", tenant)
    policies = (
        _policy("outer", "outer_only", "GET"),
        _policy("tenant", "mounted_core", "GET"),
    )
    document = exporter.export_compositions(
        [exporter.Composition("team", outer, "outer")],
        policies=policies,
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    assert set(document["paths"]) == {"/outer", "/tenant-prefix/mounted"}
    outer_identity = _operation(document, "outer.outer_only.get")[
        "x-frisket-effective-identities"
    ]
    tenant_identity = _operation(document, "tenant.mounted_core.get")[
        "x-frisket-effective-identities"
    ]
    assert outer_identity[0]["owner"] == "outer"
    assert tenant_identity[0]["owner"] == "tenant"


def _case_cross_edition_shared(
    _tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    local = FastAPI()
    team_core = FastAPI()
    _add_json_route(local, "/shared", "shared")
    _add_json_route(team_core, "/shared", "shared")
    document = exporter.export_compositions(
        [
            exporter.Composition("local", local, "tenant"),
            exporter.Composition("team", team_core, "tenant"),
        ],
        policies=(
            _policy(
                "tenant",
                "shared",
                "GET",
                operation_id="stable.shared.get",
            ),
        ),
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    assert exporter.operation_count(document) == 1
    assert _operation(document, "stable.shared.get")["x-frisket-editions"] == [
        "local",
        "team",
    ]


def _case_shadowing_resolution(
    _tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = FastAPI()
    tenant = FastAPI()
    _add_json_route(outer, "/same", "winner")
    _add_json_route(tenant, "/same", "loser")
    _add_json_route(tenant, "/prefixed", "prefixed")
    outer.mount("/", tenant)
    same_id_policies = (
        _policy("outer", "winner", "GET", operation_id="canonical.same.get"),
        _policy("tenant", "loser", "GET", operation_id="canonical.same.get"),
        _policy("tenant", "prefixed", "GET"),
    )
    document = exporter.export_compositions(
        [exporter.Composition("team", outer, "outer")],
        policies=same_id_policies,
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    identities = _operation(document, "canonical.same.get")[
        "x-frisket-effective-identities"
    ]
    assert identities[0]["routeName"] == "winner"
    assert "/prefixed" in document["paths"]

    unexpected_outer = FastAPI()
    unexpected_tenant = FastAPI()
    _add_json_route(unexpected_outer, "/shadow", "unmarked_winner")
    _add_json_route(unexpected_tenant, "/shadow", "marked_loser")
    unexpected_outer.mount("/", unexpected_tenant)
    unexpected_policies = (
        _policy("outer", "unmarked_winner", "GET", member=False),
        _policy("tenant", "marked_loser", "GET"),
    )
    with pytest.raises(exporter.ExportError, match="unexpectedly shadowed"):
        exporter.export_compositions(
            [exporter.Composition("team", unexpected_outer, "outer")],
            policies=unexpected_policies,
            canonical_ids={},
            intentional_distinct_shadows=frozenset(),
        )

    ambiguous = tuple(
        replace(policy, browser_client=True) for policy in unexpected_policies
    )
    with pytest.raises(exporter.ExportError, match="ambiguous selected/effective"):
        exporter.export_compositions(
            [exporter.Composition("team", unexpected_outer, "outer")],
            policies=ambiguous,
            canonical_ids={},
            intentional_distinct_shadows=frozenset(),
        )

    prefixed_outer = FastAPI()
    prefixed_tenant = FastAPI()
    _add_json_route(prefixed_tenant, "/route", "under_mount")
    prefixed_outer.mount("/non-root", prefixed_tenant)
    prefixed = exporter.export_compositions(
        [exporter.Composition("team", prefixed_outer, "outer")],
        policies=(_policy("tenant", "under_mount", "GET"),),
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    assert "/non-root/route" in prefixed["paths"]


def _case_canonical_id_integrity(
    _tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    duplicate = FastAPI()
    _add_json_route(duplicate, "/one", "one")
    _add_json_route(duplicate, "/two", "two")
    with pytest.raises(exporter.ExportError, match="duplicate operationId"):
        exporter.export_compositions(
            [exporter.Composition("local", duplicate, "tenant")],
            policies=(
                _policy("tenant", "one", "GET", operation_id="duplicate.get"),
                _policy("tenant", "two", "GET", operation_id="duplicate.get"),
            ),
            canonical_ids={},
            intentional_distinct_shadows=frozenset(),
        )

    local = FastAPI()
    team = FastAPI()
    routes = (
        ("health", "health", "GET", "/api/health"),
        ("runtime_config", "runtime_config", "GET", "/api/config"),
        (
            "get_blob",
            "get_project_blob",
            "GET",
            "/api/projects/{pid}/blobs/{digest}",
        ),
        (
            "update_project_sensitivity",
            "update_project_sensitivity",
            "PATCH",
            "/api/projects/{pid}/sensitivity",
        ),
    )
    alias_policies: list[EndpointPolicy] = []
    for tenant_name, outer_name, method, path in routes:
        _add_json_route(local, path, tenant_name, method=method)
        _add_json_route(team, path, outer_name, method=method)
        alias_policies.extend(
            (
                _policy("tenant", tenant_name, method),
                _policy("outer", outer_name, method),
            )
        )
    aliases = exporter.export_compositions(
        [
            exporter.Composition("local", local, "tenant"),
            exporter.Composition("team", team, "outer"),
        ],
        policies=tuple(alias_policies),
    )
    assert _operation_ids(aliases) == {
        "outer.update_project_sensitivity.patch",
        "tenant.get_blob.get",
        "tenant.health.get",
        "tenant.runtime_config.get",
    }

    local_projects = FastAPI()
    team_projects = FastAPI()
    for method, name in (("POST", "create_project"), ("GET", "list_projects")):
        _add_json_route(local_projects, "/api/projects", name, method=method)
        _add_json_route(team_projects, "/api/projects", name, method=method)
    project_policies = (
        _policy("tenant", "create_project", "POST"),
        _policy("tenant", "list_projects", "GET"),
        _policy("outer", "create_project", "POST", member=False),
        _policy("outer", "list_projects", "GET", member=False),
    )
    local_document = exporter.export_compositions(
        [exporter.Composition("local", local_projects, "tenant")],
        policies=project_policies,
    )
    team_document = exporter.export_compositions(
        [exporter.Composition("team", team_projects, "outer")],
        policies=tuple(
            replace(policy, browser_client=policy.route_owner == "outer")
            for policy in project_policies
        ),
    )
    assert _operation_ids(local_document) == {
        "tenant.create_project.post",
        "tenant.list_projects.get",
    }
    assert _operation_ids(team_document) == {
        "outer.create_project.post",
        "outer.list_projects.get",
    }


def _case_id_stability_and_version_detection(
    _tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    old = FastAPI()
    moved = FastAPI()
    renamed = FastAPI()
    _add_json_route(old, "/stable", "old_route_name")
    _add_json_route(moved, "/moved", "old_route_name")
    _add_json_route(renamed, "/moved", "renamed_route")
    stable_policy = _policy(
        "tenant",
        "old_route_name",
        "GET",
        operation_id="protected.stable.get",
    )
    old_document = exporter.export_compositions(
        [exporter.Composition("local", old, "tenant")],
        policies=(stable_policy,),
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    moved_document = exporter.export_compositions(
        [exporter.Composition("local", moved, "tenant")],
        policies=(stable_policy,),
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    assert set(old_document["paths"]) == {"/stable"}
    assert set(moved_document["paths"]) == {"/moved"}
    assert exporter.operation_id_changes(old_document, moved_document) == ()

    renamed_policy = _policy(
        "tenant",
        "renamed_route",
        "GET",
        operation_id="protected.stable.get",
    )
    renamed_document = exporter.export_compositions(
        [exporter.Composition("local", renamed, "tenant")],
        policies=(renamed_policy,),
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    assert exporter.operation_id_changes(moved_document, renamed_document) == ()

    changed_document = exporter.export_compositions(
        [exporter.Composition("local", renamed, "tenant")],
        policies=(replace(renamed_policy, id="protected.stable.v2.get"),),
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    assert exporter.operation_id_changes(renamed_document, changed_document) == (
        (
            "GET",
            "/moved",
            "protected.stable.get",
            "protected.stable.v2.get",
        ),
    )


def _case_edition_schema_disagreement(
    _tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    local = FastAPI()
    team = FastAPI()
    _add_json_route(local, "/same", "local_shape", response_model=LocalShape)
    _add_json_route(team, "/same", "team_shape", response_model=TeamShape)
    same_operation = (
        _policy(
            "tenant",
            "local_shape",
            "GET",
            operation_id="declared.same.get",
        ),
        _policy(
            "outer",
            "team_shape",
            "GET",
            operation_id="declared.same.get",
        ),
    )
    with pytest.raises(exporter.ExportError, match="edition schema disagreement"):
        exporter.export_compositions(
            [
                exporter.Composition("local", local, "tenant"),
                exporter.Composition("team", team, "outer"),
            ],
            policies=same_operation,
            canonical_ids={},
            intentional_distinct_shadows=frozenset(),
        )

    distinct_local = FastAPI()
    distinct_team = FastAPI()
    _add_json_route(
        distinct_local, "/local-shape", "local_distinct", response_model=LocalShape
    )
    _add_json_route(
        distinct_team, "/team-shape", "team_distinct", response_model=TeamShape
    )
    distinct = exporter.export_compositions(
        [
            exporter.Composition("local", distinct_local, "tenant"),
            exporter.Composition("team", distinct_team, "outer"),
        ],
        policies=(
            _policy("tenant", "local_distinct", "GET"),
            _policy("outer", "team_distinct", "GET"),
        ),
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    assert exporter.operation_count(distinct) == 2


def _case_untyped_json_marked_only(
    _tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    _add_json_route(app, "/untyped", "untyped", response_model=dict)
    unmarked = _policy("tenant", "untyped", "GET", member=False)
    document = exporter.export_compositions(
        [exporter.Composition("local", app, "tenant")],
        policies=(unmarked,),
        canonical_ids={},
        intentional_distinct_shadows=frozenset(),
    )
    assert exporter.operation_count(document) == 0
    with pytest.raises(exporter.ExportError, match="untyped JSON response"):
        exporter.export_compositions(
            [exporter.Composition("local", app, "tenant")],
            policies=(replace(unmarked, browser_client=True),),
            canonical_ids={},
            intentional_distinct_shadows=frozenset(),
        )


def _case_determinism(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = exporter._generate_once(tmp_path / "in-process-a")
    second = exporter._generate_once(tmp_path / "in-process-b")
    assert first == second

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    command = [
        # subprocess-boundary: fresh-interpreter determinism is the contract
        sys.executable,
        str(SCRIPT),
        "--temp-root",
        str(tmp_path / "fresh"),
    ]
    fresh_a = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )
    fresh_b = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )
    assert fresh_a.stdout == fresh_b.stdout == first
    assert fresh_a.stderr == fresh_b.stderr == b""

    output = tmp_path / "checked.json"
    output.write_bytes(b"stable\n")
    monkeypatch.setattr(exporter, "_generate_once", lambda _root: b"stable\n")
    assert exporter.main(["--check", "--output", str(output)]) == 0
    output.write_bytes(b"stale\n")
    assert exporter.main(["--check", "--output", str(output)]) == 1
    assert output.read_bytes() == b"stale\n"


def _case_unsupported_content(
    _tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()

    async def text_response() -> str:
        return "not json"

    app.add_api_route(
        "/text",
        text_response,
        methods=["GET"],
        name="text_response",
        response_class=PlainTextResponse,
        response_model=None,
    )
    with pytest.raises(exporter.ExportError, match="unsupported response content"):
        exporter.export_compositions(
            [exporter.Composition("local", app, "tenant")],
            policies=(_policy("tenant", "text_response", "GET"),),
            canonical_ids={},
            intentional_distinct_shadows=frozenset(),
        )


def _snapshot_source_tree() -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for top_level in ("scripts", "src", "tests"):
        for path in (ROOT / top_level).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                stat = path.stat()
                snapshot[str(path.relative_to(ROOT))] = (
                    stat.st_size,
                    stat.st_mtime_ns,
                )
    return snapshot


def _case_filesystem_containment(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    sentinel = sandbox / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    state_parent = sandbox / "state"
    output = sandbox / "output" / "openapi.json"
    source_before = _snapshot_source_tree()
    assert (
        exporter.main(
            [
                "--temp-root",
                str(state_parent),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert source_before == _snapshot_source_tree()
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert output.is_file()
    created_files = {
        path.relative_to(sandbox) for path in sandbox.rglob("*") if path.is_file()
    }
    assert created_files == {
        Path("sentinel.txt"),
        Path("output/openapi.json"),
    }


def _case_project_config_identity_and_parity(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> None:
    document = exporter.export_real_compositions(tmp_path / "state")
    operation_ids = _operation_ids(document)
    expected = {
        "outer.update_project_network.patch",
        "tenant.compact_project.post",
        "tenant.delete_project_provider_key.delete",
        "tenant.delete_project_secret.delete",
        "tenant.get_project_network.get",
        "tenant.get_project_provider_keys.get",
        "tenant.get_project_retention.get",
        "tenant.get_project_secrets.get",
        "tenant.get_project_settings.get",
        "tenant.set_project_provider_key.post",
        "tenant.set_project_secret.post",
        "tenant.update_project_retention.patch",
        "tenant.update_project_settings.patch",
        "tenant.validate_project_provider_key.post",
    }
    assert expected <= operation_ids
    assert "tenant.update_project_network.patch" not in operation_ids
    network = _operation(document, "outer.update_project_network.patch")
    assert network["x-frisket-editions"] == ["local", "team"]
    assert {row["owner"] for row in network["x-frisket-effective-identities"]} == {
        "tenant",
        "outer",
    }
    assert "409" not in network["responses"]


Case = Callable[[Path, pytest.MonkeyPatch], None]
MATRIX: tuple[tuple[str, Case], ...] = (
    ("local-team-surface-inclusion", _case_local_team_surface),
    ("implicit-validation-response-truth", _case_implicit_422_truth),
    ("mount-owner-labeling", _case_mount_owner_labeling),
    ("cross-edition-shared-operation", _case_cross_edition_shared),
    ("shadowing-resolution", _case_shadowing_resolution),
    ("canonical-id-integrity", _case_canonical_id_integrity),
    ("id-stability-version-detection", _case_id_stability_and_version_detection),
    ("edition-schema-disagreement", _case_edition_schema_disagreement),
    ("untyped-json-marked-only", _case_untyped_json_marked_only),
    ("determinism", _case_determinism),
    ("unsupported-content", _case_unsupported_content),
    ("filesystem-containment", _case_filesystem_containment),
    ("project-config-identity-and-parity", _case_project_config_identity_and_parity),
)


@pytest.mark.parametrize(
    ("_class_name", "exercise"),
    MATRIX,
    ids=[class_name for class_name, _exercise in MATRIX],
)
def test_http02_export_matrix(
    _class_name: str,
    exercise: Case,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exercise(tmp_path, monkeypatch)

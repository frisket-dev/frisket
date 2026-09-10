"""Public wire, auth, and projection contract for the team admin overview."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.models import HttpError
from frisket.server.app import create_app
from frisket.team.app import TeamConfig, create_team_app
from scripts.ci.export_web_openapi import export_real_compositions
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


_CONTRACT_MODULE = "frisket.contracts.http.admin_overview"
_OPERATION_ID = "outer.admin_overview.get"
_PATH = "/api/admin/overview"


async def _mail(_email: str, _link: str) -> bool:
    return True


def _app(tmp_path: Path) -> Any:
    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Admin overview contracts",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=_mail,
        product_telemetry_destination=None,
    )
    claim_server(app, workspace_name="Admin overview contracts")
    return app


def _sign_in(client: TestClient, app: Any, email: str) -> None:
    if email != "owner@example.com":
        seed_member_invite(app, email)
    sign_in_with_magic_link(app, client, email)


def _contract() -> Any:
    spec = importlib.util.find_spec(_CONTRACT_MODULE)
    assert spec is not None, (
        "INTENDED_ADMIN_OVERVIEW_RED: admin_overview public contract module must exist"
    )
    return importlib.import_module(_CONTRACT_MODULE)


def test_admin_overview_route_binds_the_public_model_and_declared_errors(
    tmp_path: Path,
) -> None:
    contract = _contract()
    app = _app(tmp_path)
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name == "admin_overview"
    )

    assert route.path == _PATH
    assert route.methods == {"GET"}
    assert route.response_model is contract.AdminOverviewResponse
    assert route.response_model_exclude_unset is True
    assert set(route.responses) == {401, 403, 500}
    assert all(value == {"model": HttpError} for value in route.responses.values())


def test_admin_overview_dto_is_strict_for_shared_fields_and_lossless_for_extensions() -> (
    None
):
    contract = _contract()
    team_payload = {
        "totals": {
            "orgs": 1,
            "users": 7,
            "projects": 3,
            "pending_invites": 2,
        }
    }
    cloud_payload = {
        "orgs": [
            {
                "id": 17,
                "name": "Cloud",
                "suspended": False,
                "credits_usd": 12.5,
                "projects": 3,
                "users": ["owner@example.com"],
                "runs_30d": 4,
                "spend_30d": 4.2,
            }
        ],
        "totals": {
            "orgs": 1,
            "pending_invites": 2,
            "credits_usd": 12.5,
            "spend_30d": 4.2,
        },
        "next_invite_expires_at": "2026-08-13T00:00:00Z",
    }
    for payload in (team_payload, cloud_payload):
        assert (
            contract.AdminOverviewResponse.model_validate(payload).model_dump(
                mode="json", exclude_unset=True
            )
            == payload
        )

    response_app = FastAPI()

    @response_app.get(
        "/overview",
        response_model=contract.AdminOverviewResponse,
        response_model_exclude_unset=True,
    )
    def overview_with_managed_extensions() -> dict[str, object]:
        return cloud_payload

    assert TestClient(response_app).get("/overview").json() == cloud_payload

    invalid_totals = (
        {**team_payload["totals"], "users": "7"},
        {"pending_invites": 2},
        {"orgs": 1},
        {**cloud_payload["totals"], "users": None},
        {**cloud_payload["totals"], "projects": None},
    )
    for totals in invalid_totals:
        with pytest.raises(ValidationError):
            contract.AdminOverviewResponse.model_validate({"totals": totals})
    with pytest.raises(ValidationError):
        contract.AdminOverviewResponse.model_validate({"totals": []})


def test_admin_overview_auth_matrix_and_current_team_response_are_unchanged(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    anonymous = TestClient(app)
    owner = TestClient(app)
    member = TestClient(app)
    _sign_in(owner, app, "owner@example.com")
    _sign_in(member, app, "member@example.com")

    token = owner.post("/api/org/tokens", json={"name": "overview-contract"})
    assert token.status_code == 200, token.text
    pat = token.json()["token"]

    assert anonymous.get(_PATH).status_code == 401
    assert member.get(_PATH).status_code == 403
    pat_response = owner.get(_PATH, headers={"Authorization": f"Bearer {pat}"})
    assert pat_response.status_code == 403
    assert pat_response.json() == {"detail": "admin routes require a browser session"}
    assert owner.get(_PATH).json() == {
        "totals": {"orgs": 1, "users": 2, "projects": 0, "pending_invites": 0}
    }


def test_admin_overview_is_team_only_browser_member_and_one_new_operation(
    tmp_path: Path,
) -> None:
    policies = {entry.id: entry for entry in BASE_ENDPOINT_CATALOG}
    policy = policies[_OPERATION_ID]
    assert (
        policy.route_owner,
        policy.method,
        policy.auth,
        policy.browser_client,
        policy.forwards_to_tenant,
        policy.project_role,
        policy.resolvers,
        policy.reserves_funding,
        policy.pat_forbidden_detail,
    ) == (
        "outer",
        "GET",
        "admin",
        True,
        False,
        None,
        (),
        False,
        "admin routes require a browser session",
    )
    assert (
        TestClient(create_app(tmp_path / "local", serve_spa=False))
        .get(_PATH)
        .status_code
        == 404
    )

    document = export_real_compositions(tmp_path / "export")
    operation = document["paths"][_PATH]["get"]
    assert operation["operationId"] == _OPERATION_ID
    assert operation["x-frisket-editions"] == ["team"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AdminOverviewResponse"
    }
    assert set(operation["responses"]) >= {"200", "401", "403", "500"}

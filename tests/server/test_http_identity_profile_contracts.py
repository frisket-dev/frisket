"""Runtime contracts for the shared identity/profile HTTP pair."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from frisket.contracts.http.identity_profile import (
    IdentityProfileResponse,
    ProfilePatchRequest,
)
from frisket.team.app import TeamConfig, create_team_app
from tests.team_setup_helpers import claim_server


def _wire_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def _app(tmp_path: Path) -> Any:
    return create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Identity contracts",
            admin_emails={"owner@example.com"},
        ),
        product_telemetry_destination=None,
    )


def test_identity_response_is_strict_json_with_open_edition_fields() -> None:
    payload = {
        "email": "owner@example.com",
        "display_name": None,
        "avatar_seed": "user:owner@example.com",
        "instance": {
            "display_name": "Investigations Desk",
            "welcome_message": None,
            "support_contact": None,
        },
        "cost_preapproval_usd": None,
        "balance_credits": 2.0,
        "edition_posture": {"features": ["billing", None], "ready": True},
    }
    model = IdentityProfileResponse.model_validate(payload)
    assert _wire_bytes(model.model_dump(exclude_unset=True)) == _wire_bytes(payload)

    with pytest.raises(ValidationError, match="valid JSON value"):
        IdentityProfileResponse.model_validate({**payload, "edition_posture": object()})
    with pytest.raises(ValidationError):
        IdentityProfileResponse.model_validate(
            {**payload, "instance": {"display_name": "x", "surprise": True}}
        )


def test_profile_patch_model_preserves_tolerant_request_behavior() -> None:
    assert ProfilePatchRequest.model_validate({}).model_dump() == {
        "display_name": None,
        "cost_preapproval_usd": None,
    }
    assert ProfilePatchRequest.model_validate(
        {"display_name": " Owner ", "producer_extension": True}
    ).model_dump() == {"display_name": " Owner ", "cost_preapproval_usd": None}
    with pytest.raises(ValidationError):
        ProfilePatchRequest.model_validate({"display_name": 7})
    with pytest.raises(ValidationError):
        ProfilePatchRequest.model_validate({"display_name": "x" * 201})


def test_live_team_identity_profile_wire_auth_and_validation(tmp_path: Path) -> None:
    app = _app(tmp_path)
    owner = claim_server(app, workspace_name="Identity contracts")

    expected = {
        "email": "owner@example.com",
        "display_name": "Owner",
        "avatar_seed": "user:owner@example.com",
        "instance": {
            "display_name": "Identity contracts",
            "welcome_message": None,
            "support_contact": None,
        },
        "cost_preapproval_usd": None,
    }
    response = owner.get("/api/me")
    assert response.status_code == 200, response.text
    assert response.content == _wire_bytes(expected)

    created = owner.post("/api/org/tokens", json={"name": "identity probe"})
    assert created.status_code == 200, created.text
    bearer = {"Authorization": f"Bearer {created.json()['token']}"}
    pat_me = owner.get("/api/me", headers=bearer)
    assert pat_me.status_code == 200, pat_me.text
    assert pat_me.json()["email"] == "owner@example.com"
    assert owner.patch("/api/me/profile", headers=bearer, json={}).status_code == 403

    changed = owner.patch(
        "/api/me/profile",
        json={"display_name": "  Owner Name  ", "ignored": "still ignored"},
    )
    assert changed.status_code == 200, changed.text
    expected["display_name"] = "Owner Name"
    assert changed.content == _wire_bytes(expected)
    preapproved = owner.patch("/api/me/profile", json={"cost_preapproval_usd": "2.50"})
    assert preapproved.status_code == 200, preapproved.text
    assert float(preapproved.json()["cost_preapproval_usd"]) == 2.5
    assert preapproved.json()["display_name"] == "Owner Name"
    renamed = owner.patch("/api/me/profile", json={"display_name": "Renamed"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "Renamed"
    assert float(renamed.json()["cost_preapproval_usd"]) == 2.5
    assert (
        owner.patch("/api/me/profile", json={"cost_preapproval_usd": "NaN"}).status_code
        == 400
    )
    assert (
        owner.patch(
            "/api/me/profile", json={"cost_preapproval_usd": "1000000000000"}
        ).status_code
        == 400
    )
    assert owner.patch("/api/me/profile", json={}).json()["display_name"] == "Renamed"
    assert owner.patch("/api/me/profile", json={"display_name": 7}).status_code == 422
    assert (
        owner.patch("/api/me/profile", json={"display_name": "x" * 201}).status_code
        == 422
    )
    assert (
        owner.patch(
            "/api/me/profile",
            content=b"",
            headers={"content-type": "application/json"},
        ).status_code
        == 422
    )


def test_identity_profile_openapi_is_typed(tmp_path: Path) -> None:
    document = _app(tmp_path).openapi()
    me = document["paths"]["/api/me"]["get"]
    patch = document["paths"]["/api/me/profile"]["patch"]
    success_ref = {"$ref": "#/components/schemas/IdentityProfileResponse"}
    assert (
        me["responses"]["200"]["content"]["application/json"]["schema"] == success_ref
    )
    assert (
        patch["responses"]["200"]["content"]["application/json"]["schema"]
        == success_ref
    )
    assert patch["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProfilePatchRequest"
    }
    schemas = document["components"]["schemas"]
    assert schemas["IdentityProfileResponse"]["additionalProperties"] == {
        "$ref": "#/components/schemas/IdentityEditionValue"
    }
    assert schemas["IdentityInstance"]["additionalProperties"] is False

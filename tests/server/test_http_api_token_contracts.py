"""Runtime HTTP contracts for browser-managed organization API tokens."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.team.app import TeamConfig, create_team_app
from frisket.team.schema import api_tokens, audit_log
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


async def _mail(_email: str, _link: str) -> bool:
    return True


def _app(tmp_path: Path) -> Any:
    config = TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="API token contracts",
        admin_emails={"owner@example.com"},
    )
    return create_team_app(
        config,
        send_magic_email=_mail,
        product_telemetry_destination=None,
    )


def _login(client: TestClient, app: Any, email: str) -> None:
    sign_in_with_magic_link(app, client, email)


def _audit_details(app: Any, action: str) -> list[str]:
    with app.state.control_engine.connect() as cx:
        return list(
            cx.execute(
                sa.select(audit_log.c.detail)
                .where(audit_log.c.action == action)
                .order_by(audit_log.c.id)
            ).scalars()
        )


def test_api_token_create_list_and_revoke_are_truthful(tmp_path: Path) -> None:
    app = _app(tmp_path)
    owner = claim_server(app, workspace_name="API token contracts")

    created = owner.post(
        "/api/org/tokens",
        json={"name": "  automation  "},
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert set(payload) == {"id", "name", "token", "prefix"}
    assert payload["name"] == "automation"
    assert payload["token"].startswith("frisket_pat_")
    assert payload["prefix"] == payload["token"][:20]
    token_id = payload["id"]
    assert isinstance(token_id, int)

    with app.state.control_engine.connect() as cx:
        stored = cx.execute(
            sa.select(api_tokens).where(api_tokens.c.id == token_id)
        ).one()
    assert stored.name == "automation"
    assert stored.prefix == payload["prefix"]
    assert stored.token_hash == hashlib.sha256(payload["token"].encode()).hexdigest()
    assert payload["token"] not in repr(stored._mapping)
    assert _audit_details(app, "pat_created") == [str(token_id)]

    listed = owner.get("/api/org/tokens")
    assert listed.status_code == 200, listed.text
    assert listed.json() == [
        {
            "id": token_id,
            "user_id": stored.user_id,
            "created_by": "owner@example.com",
            "name": "automation",
            "prefix": payload["prefix"],
            "created_at": stored.created_at.isoformat(),
            "last_used_at": None,
            "revoked": False,
        }
    ]
    assert not ({"token", "token_hash", "org_id", "revoked_at"} & set(listed.json()[0]))
    assert payload["token"] not in listed.text

    used_at = datetime(2026, 8, 11, 12, 30, tzinfo=UTC)
    with app.state.control_engine.begin() as cx:
        cx.execute(
            api_tokens.update()
            .where(api_tokens.c.id == token_id)
            .values(last_used_at=used_at)
        )
    used = owner.get("/api/org/tokens").json()[0]
    assert used["last_used_at"] == used_at.replace(tzinfo=None).isoformat()

    revoked = owner.delete(f"/api/org/tokens/{token_id}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() == {"ok": True, "revoked": True}
    assert owner.get("/api/org/tokens").json() == []
    with app.state.control_engine.connect() as cx:
        tombstone = cx.execute(
            sa.select(api_tokens.c.revoked_at).where(api_tokens.c.id == token_id)
        ).scalar_one()
    assert tombstone is not None
    assert _audit_details(app, "pat_revoked") == [str(token_id)]

    repeated = owner.delete(f"/api/org/tokens/{token_id}")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {"ok": True, "revoked": True}
    assert _audit_details(app, "pat_revoked") == [str(token_id), str(token_id)]


def test_api_token_management_remains_browser_only_and_actor_scoped(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    owner = claim_server(app, workspace_name="API token contracts")
    seed_member_invite(app, "member@example.com")
    member = TestClient(app)
    _login(member, app, "member@example.com")

    created = owner.post("/api/org/tokens", json={"name": "owner token"}).json()
    token_id = created["id"]
    pat_headers = {"Authorization": f"Bearer {created['token']}"}

    anonymous = TestClient(app)
    assert anonymous.get("/api/org/tokens").status_code == 401
    assert anonymous.post("/api/org/tokens", json={"name": "nope"}).status_code == 401
    assert owner.get("/api/org/tokens", headers=pat_headers).status_code == 403
    assert (
        owner.post(
            "/api/org/tokens", headers=pat_headers, json={"name": "nested"}
        ).status_code
        == 403
    )

    assert member.get("/api/org/tokens").json() == []
    not_owned = member.delete(f"/api/org/tokens/{token_id}")
    assert not_owned.status_code == 200, not_owned.text
    assert not_owned.json() == {"ok": False, "revoked": False}
    assert [row["id"] for row in owner.get("/api/org/tokens").json()] == [token_id]


def test_api_token_openapi_exposes_only_the_three_json_contracts(
    tmp_path: Path,
) -> None:
    document = _app(tmp_path).openapi()
    paths = document["paths"]

    create = paths["/api/org/tokens"]["post"]
    listed = paths["/api/org/tokens"]["get"]
    revoked = paths["/api/org/tokens/{token_id}"]["delete"]
    assert create["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiTokenCreateRequest"
    }
    assert create["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiTokenCreateResponse"
    }
    assert listed["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiTokenList"
    }
    assert revoked["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiTokenRevokeResponse"
    }

    components = document["components"]["schemas"]
    for name in (
        "ApiTokenCreateRequest",
        "ApiTokenCreateResponse",
        "ApiTokenInfo",
        "ApiTokenRevokeResponse",
    ):
        assert components[name]["additionalProperties"] is False

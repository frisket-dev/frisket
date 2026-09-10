from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from frisket.contracts.http.organization_operations import (
    OrganizationEnvDelete,
    OrganizationEnvList,
    OrganizationEnvSave,
)
from frisket.ops import media_proxy
from frisket.team.app import TeamConfig, create_team_app
from tests.team_setup_helpers import claim_server, sign_in_with_magic_link


async def _mail(_email: str, _link: str) -> bool:
    return True


def test_org_env_contract_accepts_public_omissions_and_hosted_additions() -> None:
    listed = OrganizationEnvList.model_validate(
        [
            {"name": "PUBLIC", "hint": "...blic"},
            {
                "name": "HOSTED",
                "hint": "...sted",
                "created_at": "2026-08-13T00:00:00Z",
            },
        ]
    )
    assert listed.model_dump(exclude_unset=True) == [
        {"name": "PUBLIC", "hint": "...blic"},
        {
            "name": "HOSTED",
            "hint": "...sted",
            "created_at": "2026-08-13T00:00:00Z",
        },
    ]
    assert OrganizationEnvSave.model_validate(
        {"name": "PUBLIC", "hint": "...blic"}
    ).model_dump(exclude_unset=True) == {"name": "PUBLIC", "hint": "...blic"}
    assert OrganizationEnvSave.model_validate(
        {"name": "HOSTED", "ok": True}
    ).model_dump(exclude_unset=True) == {"name": "HOSTED", "ok": True}
    assert OrganizationEnvDelete.model_validate({"deleted": True}).model_dump(
        exclude_unset=True
    ) == {"deleted": True}
    assert OrganizationEnvDelete.model_validate(
        {"deleted": False, "ok": True}
    ).model_dump(exclude_unset=True) == {"deleted": False, "ok": True}


def _app(tmp_path: Path):
    config = TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Desk",
        admin_emails={"owner@example.com"},
        oidc_providers={},
    )
    app = create_team_app(config, send_magic_email=_mail)
    claim_server(app, origin=config.base_url)
    return app


def _login(client: TestClient, app) -> None:
    sign_in_with_magic_link(app, client, "owner@example.com")


def test_org_operations_preserve_public_bytes_and_accept_browser_and_pat_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        media_proxy,
        "read_media_proxy",
        lambda _data_dir: SimpleNamespace(url=None),
    )
    app = _app(tmp_path)
    client = TestClient(app)
    _login(client, app)

    # Missing or unknown-only payloads are malformed transport requests, not
    # domain-level invalid environment-variable values.
    assert client.post("/api/org/env", json={}).status_code == 422
    assert client.post("/api/org/env", json={"unrelated": "value"}).status_code == 422
    request_schema = app.openapi()["components"]["schemas"][
        "OrganizationEnvSaveRequest"
    ]
    assert request_schema["required"] == ["name", "value"]
    assert request_schema["additionalProperties"] is False

    browser_saved = client.post(
        "/api/org/env",
        json={"name": " client_secret ", "value": "browser-secret"},
    )
    assert browser_saved.status_code == 200, browser_saved.text
    assert browser_saved.json() == {"name": "CLIENT_SECRET", "hint": "...cret"}
    assert client.get("/api/org/env").json() == [
        {"name": "CLIENT_SECRET", "hint": "...cret"}
    ]
    assert client.get("/api/org/media-proxy/status").json() == {
        "configured": False,
        "connected": None,
        "can_configure": True,
    }
    assert client.delete("/api/org/env/CLIENT_SECRET").json() == {"deleted": True}

    created = client.post("/api/org/tokens", json={"name": "automation"})
    assert created.status_code == 200, created.text
    bearer = {"Authorization": f"Bearer {created.json()['token']}"}
    client.cookies.clear()

    pat_saved = client.post(
        "/api/org/env",
        json={"name": "automation_token", "value": "pat-secret"},
        headers=bearer,
    )
    assert pat_saved.status_code == 200, pat_saved.text
    assert pat_saved.json() == {"name": "AUTOMATION_TOKEN", "hint": "...cret"}
    assert client.get("/api/org/env", headers=bearer).json() == [
        {"name": "AUTOMATION_TOKEN", "hint": "...cret"}
    ]
    assert client.get("/api/org/media-proxy/status", headers=bearer).json() == {
        "configured": False,
        "connected": None,
        "can_configure": True,
    }
    assert client.delete("/api/org/env/AUTOMATION_TOKEN", headers=bearer).json() == {
        "deleted": True
    }

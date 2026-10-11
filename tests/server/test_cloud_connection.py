from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.cloud_connection import (
    CloudConnectionClient,
    CloudConnectionStore,
    StoredCloudConnection,
    validate_cloud_origin,
)
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudSessionResponse,
    CloudUser,
    ProjectCloudRoutingPatch,
)
from frisket.server.services.cloud_connection import (
    CloudConnectionService,
    CloudConnectionServiceError,
)
from frisket.server.workspace import Workspace


def _session(*, user_id: int = 7, account_id: int = 41) -> CloudSessionResponse:
    return CloudSessionResponse(
        user=CloudUser(
            id=user_id, email=f"user-{user_id}@example.test", display_name="Reporter"
        ),
        expires_at="2099-01-01T00:00:00+00:00",
        accounts=[
            CloudAccount(
                id=account_id,
                name="Personal",
                kind="personal",
                balance_micro=5_000_000,
                available_micro=4_500_000,
                manage_billing=True,
            )
        ],
    )


def test_installation_store_encrypts_token_outside_workspace(tmp_path):
    profile = tmp_path / "profile"
    workspace = tmp_path / "another-data-directory"
    workspace.mkdir()
    store = CloudConnectionStore(profile)

    store.save_connection("secret-cloud-bearer", _session())

    raw = (profile / "cloud_connection.json").read_text()
    assert "secret-cloud-bearer" not in raw
    assert store.load().connection.access_token == "secret-cloud-bearer"
    assert not (workspace / ".frisket").exists()
    if os.name != "nt":
        assert (profile / "cloud_connection.json").stat().st_mode & 0o777 == 0o600
        assert (profile / "secrets" / "cloud_connection.key").stat().st_mode & 0o777 == 0o600


def test_corrupt_connection_can_be_cleared_or_replaced(tmp_path):
    store = CloudConnectionStore(tmp_path / "profile")
    store.profile_dir.mkdir(parents=True)
    store.path.write_text("not-json", encoding="utf-8")

    assert store.clear_connection().connection is None
    assert store.load().connection is None

    store.path.write_text("not-json-again", encoding="utf-8")
    saved = store.save_connection("replacement-token", _session())
    assert saved.connection is not None
    assert store.load().connection.access_token == "replacement-token"
    assert "replacement-token" not in store.path.read_text(encoding="utf-8")


def test_secret_bearing_connection_repr_is_redacted():
    connection = StoredCloudConnection("never-render-this", _session())

    assert "never-render-this" not in repr(connection)


@pytest.mark.parametrize(
    "value",
    [
        "http://app.frisket.dev",
        "https://app.frisket.dev/path",
        "https://user@app.frisket.dev",
        "https://app.frisket.dev?next=bad",
    ],
)
def test_cloud_origin_refuses_non_origin_or_insecure_remote_values(value):
    with pytest.raises(ValueError, match="invalid Frisket Cloud origin"):
        validate_cloud_origin(value)


def test_device_flow_keeps_secrets_server_side_and_coalesces_early_poll(tmp_path):
    requests: list[httpx.Request] = []
    token_polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_polls
        requests.append(request)
        if request.url.path.endswith("device-authorizations"):
            return httpx.Response(
                200,
                json={
                    "device_code": "private-device-secret",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://app.frisket.dev/device",
                    "verification_uri_complete": "https://app.frisket.dev/device?code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if request.url.path.endswith("/token"):
            token_polls += 1
            if token_polls == 1:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(
                200,
                json={
                    "access_token": "private-access-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("/session"):
            assert request.headers["authorization"] == "Bearer private-access-token"
            return httpx.Response(200, json=_session().model_dump(mode="json"))
        raise AssertionError(request.url)

    ticks = [100.0]
    workspace = Workspace(tmp_path / "workspace")
    client = CloudConnectionClient(
        "https://app.frisket.dev", transport=httpx.MockTransport(handler)
    )
    service = CloudConnectionService(
        workspace,
        store=CloudConnectionStore(tmp_path / "profile"),
        client=client,
        clock=lambda: ticks[0],
        wall_clock=lambda: datetime(2026, 10, 11, tzinfo=UTC),
    )

    started = service.begin_authorization()
    public = started.model_dump(mode="json")
    assert "device_code" not in public
    assert "access_token" not in json.dumps(public)
    assert service.poll_authorization(started.id).status == "pending"
    assert token_polls == 0

    ticks[0] = 105.0
    assert service.poll_authorization(started.id).status == "pending"
    assert token_polls == 1
    assert service.poll_authorization(started.id).status == "pending"
    assert token_polls == 1

    ticks[0] = 110.0
    connected = service.poll_authorization(started.id)
    assert connected.status == "connected"
    assert connected.connection.identity.id == 7
    assert "private-access-token" not in connected.model_dump_json()
    assert token_polls == 2
    service.close()


def test_sign_out_clears_local_token_when_remote_revocation_fails(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("token", _session())
    service = CloudConnectionService(
        Workspace(tmp_path / "workspace"),
        store=store,
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
    )

    result = service.sign_out()

    assert result.revocation == "failed"
    assert result.warning
    assert result.connection.state == "disconnected"
    assert store.load().connection is None
    service.close()


def test_sign_out_cancels_pending_device_authorizations(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("device-authorizations"):
            return httpx.Response(
                200,
                json={
                    "device_code": "pending-secret",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://app.frisket.dev/device",
                    "verification_uri_complete": "https://app.frisket.dev/device?code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        raise AssertionError(request.url)

    service = CloudConnectionService(
        Workspace(tmp_path / "workspace"),
        store=CloudConnectionStore(tmp_path / "profile"),
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
    )
    started = service.begin_authorization()

    service.sign_out()

    with pytest.raises(CloudConnectionServiceError) as exc_info:
        service.poll_authorization(started.id)
    assert exc_info.value.status == 404
    service.close()


def test_project_account_is_bound_to_signed_in_user(tmp_path):
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("first", _session(user_id=7, account_id=41))
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Cloud routing")["id"]
    service = CloudConnectionService(
        workspace,
        store=store,
        client=CloudConnectionClient(
            "https://app.frisket.dev",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(500, json={})
            ),
        ),
    )

    selected = service.patch_project_routing(
        project_id,
        ProjectCloudRoutingPatch(
            selected_account_id=41, preference="cloud_first"
        ),
    )
    assert selected.selected_account_id == 41
    assert selected.preference == "cloud_first"
    assert selected.preference_inherited is False

    store.save_connection("second", _session(user_id=8, account_id=41))
    changed_user = service.project_routing(project_id)
    assert changed_user.selected_account_id is None
    assert changed_user.preference == "cloud_first"
    service.close()


def test_cloud_routes_are_registered_only_for_solo(tmp_path):
    from frisket.server.app import create_app

    solo = create_app(
        tmp_path / "solo-workspace",
        cloud_profile_dir=tmp_path / "profile",
        serve_spa=False,
    )
    team = create_app(tmp_path / "team-workspace", edition="team", serve_spa=False)
    assert any(route.path == "/api/cloud-connection" for route in solo.routes)
    assert not any(route.path == "/api/cloud-connection" for route in team.routes)


def test_local_http_endpoints_project_connection_without_exposing_bearer(tmp_path):
    from frisket.server.app import create_app

    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("never-in-renderer", _session())
    cloud_client = CloudConnectionClient(
        "https://app.frisket.dev",
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
    )
    app = create_app(
        tmp_path / "workspace",
        cloud_connection_store=store,
        cloud_connection_client=cloud_client,
        serve_spa=False,
    )
    project_id = app.state.workspace.create("HTTP routing")["id"]

    with TestClient(app) as client:
        connection = client.get("/api/cloud-connection")
        assert connection.status_code == 200
        assert connection.json()["identity"]["id"] == 7
        assert "never-in-renderer" not in connection.text

        default = client.patch(
            "/api/cloud-connection/preferences",
            json={"preference": "cloud_first"},
        )
        assert default.json() == {"preference": "cloud_first"}
        inherited = client.get(f"/api/projects/{project_id}/cloud-routing")
        assert inherited.json()["preference"] == "cloud_first"
        assert inherited.json()["preference_inherited"] is True

        selected = client.patch(
            f"/api/projects/{project_id}/cloud-routing",
            json={"selected_account_id": 41, "preference": "local_first"},
        )
        assert selected.status_code == 200
        assert selected.json()["selected_account_id"] == 41
        assert selected.json()["preference_inherited"] is False

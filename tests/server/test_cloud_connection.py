from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from datetime import UTC, datetime
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.cloud_connection import (
    CloudConnectionClient,
    CloudConnectionStore,
    CloudConnectionStoreError,
    StoredCloudConnection,
    validate_cloud_origin,
)
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudOCROptionSupport,
    CloudSessionResponse,
    CloudUnitPrice,
    CloudUser,
    ProjectCloudRoutingPatch,
)
from frisket.project_settings import ProjectSettings, write_project_settings
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


def _capabilities(
    *, account_id: int = 41, revision: str = "route-1"
) -> CloudCapabilitiesResponse:
    return CloudCapabilitiesResponse(
        account_id=account_id,
        routes=[
            CloudCapabilityRoute(
                capability="ocr",
                engine="rapidocr",
                provider="frisket",
                lane="frisket_credits",
                route_revision=revision,
                ready=True,
                blocker=None,
                price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=2500),
                options=CloudOCROptionSupport(
                    kind="ocr",
                    language=True,
                    geometry=True,
                    searchable_pdf=False,
                ),
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
        assert (
            profile / "secrets" / "cloud_connection.key"
        ).stat().st_mode & 0o777 == 0o600


def test_read_does_not_replace_a_missing_connection_key(tmp_path):
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("secret-cloud-bearer", _session())
    store.key_path.unlink()

    with pytest.raises(CloudConnectionStoreError):
        store.load()

    assert not store.key_path.exists()


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


def test_capabilities_client_uses_selected_account_and_bearer():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/desktop/v1/accounts/41/capabilities"
        assert request.headers["authorization"] == "Bearer installation-token"
        payload = CloudCapabilitiesResponse(
            account_id=41,
            routes=[
                CloudCapabilityRoute(
                    capability="ocr",
                    engine="rapidocr",
                    provider="frisket",
                    lane="frisket_credits",
                    route_revision="route-1",
                    ready=True,
                    blocker=None,
                    price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=2500),
                    options=CloudOCROptionSupport(
                        kind="ocr",
                        language=True,
                        geometry=True,
                        searchable_pdf=False,
                    ),
                )
            ],
        )
        return httpx.Response(200, json=payload.model_dump(mode="json"))

    client = CloudConnectionClient(
        "https://app.frisket.dev", transport=httpx.MockTransport(handler)
    )

    capabilities = client.capabilities("installation-token", 41)

    assert capabilities.account_id == 41
    assert capabilities.routes[0].route_revision == "route-1"


def test_capability_snapshots_are_user_bound_and_survive_same_user_refresh(tmp_path):
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("first", _session(user_id=7, account_id=41))

    saved = store.save_capabilities(_capabilities(account_id=41), expected_user_id=7)

    assert saved.capabilities[41].routes[0].route_revision == "route-1"
    store.save_connection("refreshed", _session(user_id=7, account_id=99))
    assert store.load().capabilities[41].account_id == 41

    store.save_connection("different-user", _session(user_id=8, account_id=41))
    assert store.load().capabilities == {}
    store.save_capabilities(
        _capabilities(account_id=41, revision="route-2"), expected_user_id=8
    )
    assert store.clear_connection().capabilities == {}
    assert store.load().capabilities == {}


def test_capability_snapshot_refuses_a_concurrent_user_change(tmp_path):
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("user-a", _session(user_id=7, account_id=41))
    fetched_as_a = _capabilities(account_id=41)
    store.save_connection("user-b", _session(user_id=8, account_id=41))

    with pytest.raises(CloudConnectionStoreError, match="user changed"):
        store.save_capabilities(fetched_as_a, expected_user_id=7)

    assert store.load().capabilities == {}


def test_capability_snapshot_refuses_a_concurrent_account_change(tmp_path):
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("first", _session(user_id=7, account_id=41))
    fetched_for_41 = _capabilities(account_id=41)
    store.save_connection("refreshed", _session(user_id=7, account_id=99))

    with pytest.raises(CloudConnectionStoreError, match="account changed"):
        store.save_capabilities(fetched_for_41, expected_user_id=7)

    assert store.load().capabilities == {}


def test_explicit_connection_fetch_refreshes_cloud_session(tmp_path):
    session_fetches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal session_fetches
        assert request.url.path.endswith("/session")
        session_fetches += 1
        return httpx.Response(
            200, json=_session(user_id=7, account_id=99).model_dump(mode="json")
        )

    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("token", _session(user_id=7, account_id=41))
    service = CloudConnectionService(
        Workspace(tmp_path / "workspace"),
        store=store,
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
    )

    connection = service.connection()

    assert session_fetches == 1
    assert [account.id for account in connection.accounts] == [99]
    assert store.load().connection.session.accounts[0].id == 99
    service.close()


def test_explicit_connection_fetch_maps_revoked_token_to_expired(tmp_path):
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("revoked", _session())
    service = CloudConnectionService(
        Workspace(tmp_path / "workspace"),
        store=store,
        client=CloudConnectionClient(
            "https://app.frisket.dev",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(401, json={})
            ),
        ),
    )

    connection = service.connection()

    assert connection.state == "expired"
    assert connection.identity.id == 7
    assert connection.capabilities.can_reconnect is True
    service.close()


def test_sign_out_cannot_be_overwritten_by_in_flight_session_refresh(tmp_path):
    refresh_started = Event()
    allow_refresh = Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/session"):
            refresh_started.set()
            assert allow_refresh.wait(timeout=5)
            return httpx.Response(200, json=_session().model_dump(mode="json"))
        if request.method == "DELETE" and request.url.path.endswith("/session"):
            return httpx.Response(204)
        raise AssertionError(request.url)

    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("token", _session())
    service = CloudConnectionService(
        Workspace(tmp_path / "workspace"),
        store=store,
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshing = pool.submit(service.connection)
        assert refresh_started.wait(timeout=5)
        signing_out = pool.submit(service.sign_out)
        allow_refresh.set()
        assert refreshing.result(timeout=5).state == "connected"
        assert signing_out.result(timeout=5).connection.state == "disconnected"

    assert store.load().connection is None
    service.close()


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
    assert (
        sum(
            request.method == "GET" and request.url.path.endswith("/session")
            for request in requests
        )
        == 1
    )
    service.close()


def test_concurrent_completed_polls_reuse_connected_state(tmp_path):
    token_polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_polls
        if request.url.path.endswith("device-authorizations"):
            return httpx.Response(
                200,
                json={
                    "device_code": "single-use-device-secret",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://app.frisket.dev/device",
                    "verification_uri_complete": "https://app.frisket.dev/device?code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if request.url.path.endswith("/token"):
            token_polls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "single-minted-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json=_session().model_dump(mode="json"))
        raise AssertionError(request.url)

    ticks = [100.0]
    service = CloudConnectionService(
        Workspace(tmp_path / "workspace"),
        store=CloudConnectionStore(tmp_path / "profile"),
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
        clock=lambda: ticks[0],
        wall_clock=lambda: datetime(2026, 10, 11, tzinfo=UTC),
    )
    authorization_id = service.begin_authorization().id
    ticks[0] = 105.0

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(service.poll_authorization, [authorization_id] * 4))

    assert [result.status for result in results] == ["connected"] * 4
    assert token_polls == 1

    assert service.sign_out().revocation == "revoked"
    with pytest.raises(CloudConnectionServiceError) as exc_info:
        service.poll_authorization(authorization_id)
    assert exc_info.value.status == 404
    service.close()


@pytest.mark.parametrize("failure_stage", ["session", "save"])
def test_minted_token_is_revoked_if_connection_cannot_be_persisted(
    tmp_path, failure_stage
):
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
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
            return httpx.Response(
                200,
                json={
                    "access_token": "minted-but-unusable",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("/session") and request.method == "GET":
            if failure_stage == "session":
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(200, json=_session().model_dump(mode="json"))
        if request.url.path.endswith("/session") and request.method == "DELETE":
            assert request.headers["authorization"] == "Bearer minted-but-unusable"
            return httpx.Response(204)
        raise AssertionError(request.url)

    ticks = [100.0]

    class Store(CloudConnectionStore):
        def save_connection(self, access_token, session):
            if failure_stage == "save":
                raise OSError("simulated local persistence failure")
            return super().save_connection(access_token, session)

    service = CloudConnectionService(
        Workspace(tmp_path / "workspace"),
        store=Store(tmp_path / "profile"),
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
        clock=lambda: ticks[0],
        wall_clock=lambda: datetime(2026, 10, 11, tzinfo=UTC),
    )
    authorization_id = service.begin_authorization().id
    ticks[0] = 105.0

    with pytest.raises(CloudConnectionServiceError) as exc_info:
        service.poll_authorization(authorization_id)

    assert exc_info.value.status == 503
    assert ("DELETE", "/api/desktop/v1/session") in requests
    service.close()


@pytest.mark.parametrize("existing_token", [None, "still-valid-prior-token"])
def test_write_then_raise_does_not_retain_revoked_token(
    tmp_path, monkeypatch, existing_token
):
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
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
            return httpx.Response(
                200,
                json={
                    "access_token": "new-token-to-revoke",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("/session") and request.method == "GET":
            return httpx.Response(200, json=_session().model_dump(mode="json"))
        if request.url.path.endswith("/session") and request.method == "DELETE":
            assert request.headers["authorization"] == "Bearer new-token-to-revoke"
            return httpx.Response(204)
        raise AssertionError(request.url)

    store = CloudConnectionStore(tmp_path / "profile")
    if existing_token is not None:
        store.save_connection(existing_token, _session(user_id=8, account_id=88))
        store.set_preference("cloud_first")
    write = store._write_unlocked
    should_fail = True

    def write_then_raise(payload):
        nonlocal should_fail
        write(payload)
        if should_fail:
            should_fail = False
            raise OSError("simulated directory fsync failure after replace")

    monkeypatch.setattr(store, "_write_unlocked", write_then_raise)
    ticks = [100.0]
    service = CloudConnectionService(
        Workspace(tmp_path / "workspace"),
        store=store,
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
        clock=lambda: ticks[0],
        wall_clock=lambda: datetime(2026, 10, 11, tzinfo=UTC),
    )
    authorization_id = service.begin_authorization().id
    ticks[0] = 105.0

    with pytest.raises(CloudConnectionServiceError) as exc_info:
        service.poll_authorization(authorization_id)

    assert exc_info.value.status == 503
    profile = store.load()
    connection = profile.connection
    assert (connection.access_token if connection else None) == existing_token
    if existing_token is not None:
        assert connection.session.user.id == 8
        assert connection.session.accounts[0].id == 88
        assert profile.preference == "cloud_first"
    assert ("DELETE", "/api/desktop/v1/session") in requests
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


def test_sign_out_warns_when_missing_key_prevents_remote_revocation(tmp_path):
    remote_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        remote_requests.append(request.url.path)
        return httpx.Response(204)

    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("unreadable-token", _session())
    store.key_path.unlink()
    service = CloudConnectionService(
        Workspace(tmp_path / "workspace"),
        store=store,
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
    )

    result = service.sign_out()

    assert result.revocation == "failed"
    assert result.warning is not None
    assert "Revoke this device in Frisket Cloud" in result.warning
    assert result.connection.state == "disconnected"
    assert remote_requests == []
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
        ProjectCloudRoutingPatch(selected_account_id=41, preference="cloud_first"),
    )
    assert selected.selected_account_id == 41
    assert selected.preference == "cloud_first"
    assert selected.preference_inherited is False

    store.save_connection("second", _session(user_id=8, account_id=41))
    changed_user = service.project_routing(project_id)
    assert changed_user.selected_account_id is None
    assert changed_user.preference == "cloud_first"
    service.close()


def test_project_keeps_same_users_unavailable_selected_account(tmp_path):
    current_session = [_session(user_id=7, account_id=41)]
    session_fetches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal session_fetches
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(503, json={})
        assert request.url.path.endswith("/session")
        session_fetches += 1
        return httpx.Response(200, json=current_session[0].model_dump(mode="json"))

    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("token", current_session[0])
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Unavailable Cloud account")["id"]
    service = CloudConnectionService(
        workspace,
        store=store,
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
    )
    selected = service.patch_project_routing(
        project_id, ProjectCloudRoutingPatch(selected_account_id=41)
    )
    assert selected.selected_account_id == 41

    current_session[0] = _session(user_id=7, account_id=99)
    unavailable = service.project_routing(project_id)

    assert session_fetches == 2
    assert unavailable.selected_account_id == 41
    assert [account.id for account in unavailable.accounts] == [99]
    service.close()


def test_project_routing_refreshes_the_selected_account_capability_snapshot(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json=_session().model_dump(mode="json"))
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(
                200,
                json=_capabilities(revision="route-2").model_dump(mode="json"),
            )
        raise AssertionError(request.url)

    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("token", _session())
    store.save_capabilities(_capabilities(revision="route-1"), expected_user_id=7)
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Refresh Cloud routes")["id"]
    write_project_settings(
        workspace.get(project_id),
        ProjectSettings(cloud_user_id=7, cloud_account_id=41),
    )
    service = CloudConnectionService(
        workspace,
        store=store,
        client=CloudConnectionClient(
            "https://app.frisket.dev", transport=httpx.MockTransport(handler)
        ),
    )

    service.project_routing(project_id)

    assert calls == [
        "/api/desktop/v1/session",
        "/api/desktop/v1/accounts/41/capabilities",
    ]
    assert store.load().capabilities[41].routes[0].route_revision == "route-2"
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


def test_corrupt_connection_does_not_break_project_routing(tmp_path):
    from frisket.server.app import create_app

    store = CloudConnectionStore(tmp_path / "profile")
    store.profile_dir.mkdir(parents=True)
    store.path.write_text("not-json", encoding="utf-8")
    app = create_app(
        tmp_path / "workspace",
        cloud_connection_store=store,
        cloud_connection_client=CloudConnectionClient(
            "https://app.frisket.dev",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(500, json={})
            ),
        ),
        serve_spa=False,
    )
    project_id = app.state.workspace.create("Corrupt Cloud profile")["id"]

    with TestClient(app) as client:
        response = client.get(f"/api/projects/{project_id}/cloud-routing")

    assert response.status_code == 200
    assert response.json()["connection_state"] == "expired"
    assert response.json()["preference"] == "local_first"
    assert response.json()["preference_inherited"] is True

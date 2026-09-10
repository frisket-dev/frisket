"""Focused HTTP behavior for typed notification delivery settings."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_delivery_settings_keep_compatibility_seams_and_public_projections(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "delivery-settings")
    project_id = str(app.state.workspace.create("Delivery settings")["id"])
    base = f"/api/projects/{project_id}"

    with TestClient(app) as client:
        email = client.post(
            f"{base}/notification-channels",
            json={
                "kind": "email",
                "to": "ops@example.com",
                "from": "preferred@example.com",
                "from_address": "legacy@example.com",
                "from_": "internal-name-is-not-wire",
                "raw_enabled": False,
                "enabled": "false",
            },
        )
        assert email.status_code == 200, email.text
        assert email.json()["enabled"] is True
        assert email.json()["config"]["from"] == "preferred@example.com"

        channel = client.post(
            f"{base}/notification-channels",
            json={
                "kind": "webhook",
                "name": "Hook",
                "webhook_url": "https://example.com/hook",
                "webhook_url_secret_ref": "env:hook-url",
                "signing_secret_ref": "env:signing",
            },
        )
        assert channel.status_code == 200, channel.text
        assert channel.json()["has_secret"] is True
        assert "env:" not in channel.text
        channel_id = channel.json()["id"]

        route = client.post(
            f"{base}/notification-routes",
            json={
                "name": "Watch route",
                "channel_id": channel_id,
                "source_ref_match": {"watch_id": 1},
                "source_ref_match_json": {"watch_id": 2},
            },
        )
        assert route.status_code == 200, route.text
        assert route.json()["source_ref_match"] == {"watch_id": 1}
        route_id = route.json()["id"]

        tested = client.post(
            f"{base}/notification-routes/{route_id}/test",
            json={"owner_kind": "ignored"},
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["delivery_kind"] == "test"

        delivery_page = client.get(
            f"{base}/notification-delivery-requests",
            params={"route_id": route_id},
        )
        assert delivery_page.status_code == 200, delivery_page.text
        assert delivery_page.json()["delivery_requests"][0]["route_id"] == route_id
        assert "env:" not in delivery_page.text

    route_schema = app.openapi()["paths"][
        "/api/projects/{pid}/notification-routes/{route_id}/test"
    ]["post"]
    assert "requestBody" not in route_schema

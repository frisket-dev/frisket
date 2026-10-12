from __future__ import annotations

import httpx
import pytest

from frisket.cloud_connection import CloudClientError, CloudConnectionClient


def test_capabilities_opt_in_to_setup_offers():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/desktop/v1/accounts/41/capabilities"
        assert dict(request.url.params) == {"include_unconfigured": "true"}
        return httpx.Response(200, json={"account_id": 41, "routes": []})

    client = CloudConnectionClient(
        "https://cloud.example", transport=httpx.MockTransport(respond)
    )
    try:
        assert client.capabilities("device-token", 41).routes == []
    finally:
        client.close()


def test_provider_key_metadata_uses_selected_account_without_exposing_keys():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/desktop/v1/accounts/41/provider-keys"
        assert request.headers["Authorization"] == "Bearer device-token"
        return httpx.Response(
            200,
            json={
                "providers": [
                    {"provider": "datalab", "label": "Datalab", "configured": True}
                ]
            },
        )

    client = CloudConnectionClient(
        "https://cloud.example", transport=httpx.MockTransport(respond)
    )
    try:
        keys = client.provider_keys("device-token", 41)
        assert keys.model_dump() == {
            "providers": [
                {"provider": "datalab", "label": "Datalab", "configured": True}
            ]
        }
    finally:
        client.close()


def test_provider_key_metadata_refuses_unexpected_secret_fields():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "providers": [
                    {
                        "provider": "datalab",
                        "label": "Datalab",
                        "configured": True,
                        "key": "must-not-cross-boundary",
                    }
                ]
            },
        )

    client = CloudConnectionClient(
        "https://cloud.example", transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(CloudClientError, match="invalid_cloud_response"):
            client.provider_keys("device-token", 41)
    finally:
        client.close()

"""Serve a deterministic Solo stack for the Cloud connection browser E2E."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import PropertyMock, patch
from functools import partial

import httpx
import uvicorn

from frisket.ai.llm import ModelRouter
from frisket.cloud_connection import CloudConnectionClient, CloudConnectionStore
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudDeviceAuthorizationResponse,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudLLMOptionSupport,
    CloudLLMResult,
    CloudLLMUsage,
    CloudOCROptionSupport,
    CloudTranscriptionOptionSupport,
    CloudUnitPrice,
    CloudOAuthError,
    CloudSessionResponse,
    CloudTokenPrice,
    CloudTokenResponse,
    CloudUser,
)
from frisket.server.app import create_app
from frisket.server import provider_config


_ACCESS_TOKEN = "cloud-e2e-access-token"
_ACCOUNT_ID = 41
_USER_ID = 7
_MODEL = "gemini/gemini-3.5-flash-lite"


def _session() -> CloudSessionResponse:
    return CloudSessionResponse(
        user=CloudUser(
            id=_USER_ID,
            email="reporter@example.test",
            display_name="Cloud E2E Reporter",
        ),
        expires_at="2099-01-01T00:00:00Z",
        accounts=[
            CloudAccount(
                id=_ACCOUNT_ID,
                name="Investigations",
                kind="organization",
                balance_micro=5_000_000,
                available_micro=4_500_000,
                manage_billing=True,
            )
        ],
    )


def _route() -> CloudCapabilityRoute:
    return CloudCapabilityRoute(
        capability="llm.complete",
        engine=_MODEL,
        provider="gemini",
        lane="frisket_credits",
        route_revision="cloud-e2e-gemini-v1",
        ready=True,
        blocker=None,
        price=CloudTokenPrice(
            kind="tokens",
            input_micro_per_million_tokens=300_000,
            output_micro_per_million_tokens=2_500_000,
        ),
        options=CloudLLMOptionSupport(
            kind="llm",
            structured_output=True,
            tools=True,
            vision=True,
            max_output_tokens=65_536,
        ),
    )


def _media_routes() -> list[CloudCapabilityRoute]:
    return [
        CloudCapabilityRoute(
            capability="ocr",
            engine="datalab",
            provider="datalab",
            lane="account_key",
            route_revision="cloud-e2e-datalab-missing-key",
            ready=False,
            blocker="account_key_required",
            price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=600),
            options=CloudOCROptionSupport(
                kind="ocr",
                language=False,
                geometry=True,
                searchable_pdf=False,
            ),
        ),
        CloudCapabilityRoute(
            capability="ocr",
            engine="paddleocr-vl",
            provider="models-gateway",
            lane="frisket_credits",
            route_revision="cloud-e2e-ocr-v1",
            ready=True,
            blocker=None,
            price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=100),
            options=CloudOCROptionSupport(
                kind="ocr",
                language=True,
                geometry=True,
                searchable_pdf=False,
            ),
        ),
        CloudCapabilityRoute(
            capability="transcribe",
            engine="whisper-turbo",
            provider="models-gateway",
            lane="frisket_credits",
            route_revision="cloud-e2e-transcribe-v1",
            ready=True,
            blocker=None,
            price=CloudUnitPrice(kind="unit", unit="audio_second", micro_per_unit=10),
            options=CloudTranscriptionOptionSupport(
                kind="transcribe",
                diarization_mode="none",
                speaker_hint="none",
                language=True,
                model_size=False,
                vad=False,
                context=False,
            ),
        ),
    ]


def _provider_probe(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert (
        str(request.url)
        == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    )
    assert request.headers["Authorization"] == "Bearer e2e-personal-gemini-key"
    return httpx.Response(200, json={"data": [{"id": "gemini-3.5-flash-lite"}]})


def _provider_completion(request: httpx.Request) -> httpx.Response:
    assert request.method == "POST"
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert request.headers["Authorization"] == "Bearer e2e-personal-gemini-key"
    body = json.loads(request.content)
    assert body["model"] == "gemini-3.8-flash"
    assert "Alice signed the agreement." in json.dumps(body["messages"])
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"entity": "Alice via own key"}),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2},
        },
    )


class _ExternalCloud:
    def __init__(self) -> None:
        self._token_polls = 0
        self._lock = threading.Lock()

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/desktop/v1/device-authorizations":
            body = json.loads(request.content)
            assert isinstance(body.get("device_name"), str) and body["device_name"]
            issued = CloudDeviceAuthorizationResponse(
                device_code="cloud-e2e-device-code",
                user_code="CLOUD-E2E",
                verification_uri="https://cloud.example.test/device",
                verification_uri_complete=(
                    "https://cloud.example.test/device?code=CLOUD-E2E"
                ),
                expires_in=300,
                interval=1,
            )
            return httpx.Response(200, json=issued.model_dump(mode="json"))
        if request.method == "POST" and path == "/api/desktop/v1/token":
            form = parse_qs(request.content.decode("utf-8"), strict_parsing=True)
            assert form == {
                "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
                "device_code": ["cloud-e2e-device-code"],
            }
            with self._lock:
                self._token_polls += 1
                pending = self._token_polls == 1
            if pending:
                error = CloudOAuthError(error="authorization_pending")
                return httpx.Response(400, json=error.model_dump(mode="json"))
            token = CloudTokenResponse(
                access_token=_ACCESS_TOKEN,
                token_type="Bearer",
                expires_in=3600,
            )
            return httpx.Response(200, json=token.model_dump(mode="json"))
        if request.method == "GET" and path == "/api/desktop/v1/session":
            self._assert_bearer(request)
            return httpx.Response(200, json=_session().model_dump(mode="json"))
        if (
            request.method == "GET"
            and path == f"/api/desktop/v1/accounts/{_ACCOUNT_ID}/capabilities"
        ):
            self._assert_bearer(request)
            capabilities = CloudCapabilitiesResponse(
                account_id=_ACCOUNT_ID,
                routes=[
                    _route(),
                    _route().model_copy(update={"engine": "gemini/gemini-3.5-flash"}),
                    *_media_routes(),
                ],
            )
            return httpx.Response(200, json=capabilities.model_dump(mode="json"))
        if (
            request.method == "GET"
            and path == f"/api/desktop/v1/accounts/{_ACCOUNT_ID}/provider-keys"
        ):
            self._assert_bearer(request)
            return httpx.Response(
                200,
                json={
                    "providers": [
                        {"provider": "datalab", "label": "Datalab", "configured": False}
                    ]
                },
            )
        if request.method == "DELETE" and path == "/api/desktop/v1/session":
            self._assert_bearer(request)
            return httpx.Response(204)
        raise AssertionError(f"unexpected fake Cloud request: {request.method} {path}")

    @staticmethod
    def _assert_bearer(request: httpx.Request) -> None:
        assert request.headers.get("Authorization") == f"Bearer {_ACCESS_TOKEN}"


class _CloudExecutionTransport:
    def __init__(self) -> None:
        self._calls = 0
        self._lock = threading.Lock()

    async def execute_llm(self, attempt, request) -> CloudExecutionResponse:
        payer = attempt.payer
        assert payer.user_id == _USER_ID
        assert payer.account_id == _ACCOUNT_ID
        assert payer.lane == "frisket_credits"
        assert payer.provider == "gemini"
        assert payer.engine == _MODEL
        assert payer.route_revision == "cloud-e2e-gemini-v1"
        assert request.model == _MODEL
        assert request.schema is not None
        assert "Alice signed the agreement." in json.dumps(request.messages)
        with self._lock:
            self._calls += 1
            execution_id = f"cloud-e2e-llm-{self._calls}"
        result = {"entity": "Alice"}
        return CloudExecutionResponse(
            execution_id=execution_id,
            status="completed",
            route=_route(),
            result=CloudLLMResult(
                content=json.dumps(result),
                data=result,
                tokens_in=8,
                tokens_out=2,
                cost=0.000008,
                model=_MODEL,
                cost_source="cloud_receipt",
                cached=False,
                provider="gemini",
                credential_source="platform_key",
                raw={},
                output_limited=False,
            ),
            receipt=CloudExecutionReceipt(
                user_id=_USER_ID,
                account_id=_ACCOUNT_ID,
                lane="frisket_credits",
                provider="gemini",
                engine=_MODEL,
                charge_micro=8,
                provider_cost_micro=6,
                usage=CloudLLMUsage(
                    kind="llm.complete",
                    tokens_in=8,
                    tokens_out=2,
                ),
            ),
        )


def build_app(*, workspace: Path, static_dir: Path):
    external = _ExternalCloud()
    cloud_client = CloudConnectionClient(
        "https://cloud.example.test",
        transport=httpx.MockTransport(external.handle),
    )
    profile_dir = workspace.with_name(f"{workspace.name}-cloud-profile")
    return create_app(
        workspace,
        router=ModelRouter(use_env_keys=False, cache_mode="off", max_retries=0),
        static_dir=static_dir,
        serve_spa=True,
        cloud_connection_store=CloudConnectionStore(profile_dir),
        cloud_connection_client=cloud_client,
        cloud_execution_transport=_CloudExecutionTransport(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--static-dir", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    # Keep admission, adapters, receipts and credential storage real. Only
    # external Cloud/provider responses are deterministic network substitutes.
    inference = httpx.AsyncClient(transport=httpx.MockTransport(_provider_completion))
    try:
        with httpx.Client(transport=httpx.MockTransport(_provider_probe)) as client:
            probe = partial(provider_config.probe_provider, client=client)
            with (
                patch.object(provider_config, "probe_provider", probe),
                patch.object(
                    ModelRouter,
                    "client",
                    new_callable=PropertyMock,
                    return_value=inference,
                ),
            ):
                uvicorn.run(
                    build_app(
                        workspace=args.workspace.resolve(),
                        static_dir=args.static_dir.resolve(),
                    ),
                    host="127.0.0.1",
                    port=args.port,
                )
    finally:
        asyncio.run(inference.aclose())


if __name__ == "__main__":
    main()

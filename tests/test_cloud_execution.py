from __future__ import annotations

import io
import json

import httpx
import pytest

from frisket.cloud_connection import CloudConnectionStore
from frisket.cloud_execution import (
    CloudExecutionAttempt,
    CloudExecutionRefused,
    CloudLLMExecutionBinding,
    CloudMediaExecutionBinding,
    CloudMediaUpload,
    CloudPayerSubject,
    CloudQuoteAuthority,
    CloudConfirmationRequired,
    HttpCloudExecutionTransport,
    cloud_execution_accounting,
    cloud_llm_execution,
    cloud_llm_execution_scope,
    cloud_media_execution,
)
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilityRoute,
    CloudExecutionQuote,
    CloudErrorResponse,
    CloudLLMOptionSupport,
    CloudLLMUsage,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudOCROptionSupport,
    CloudOCROptions,
    CloudOCRResult,
    CloudOCRUsage,
    CloudQuoteChallenge,
    CloudSessionResponse,
    CloudUser,
    CloudLLMRequest,
)
from frisket.ops.base import OpContext
from frisket.engine.store.execution_routes import route_fact_hash
from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    compile_route_promises,
)
from frisket.execution.resolver import RouteRowFacts
from frisket.execution.targets import validated_target_snapshot


def _payer() -> CloudPayerSubject:
    return CloudPayerSubject(
        user_id=7,
        account_id=41,
        lane="frisket_credits",
        provider="anthropic",
        engine="anthropic/claude-haiku-4-5",
        route_revision="route-1",
    )


def test_cloud_attempt_carries_durable_id_and_exact_payer_without_a_secret():
    attempt = CloudExecutionAttempt(
        request_id="cloud-request-1",
        payer=_payer(),
        quote_authority=CloudQuoteAuthority(maximum_charge_micro=25_000),
    )

    assert attempt.request_id == "cloud-request-1"
    assert attempt.payer.account_id == 41
    assert "token" not in repr(attempt).lower()


@pytest.mark.parametrize("name", ["../secret.pdf", "folder/audio.wav", "", ".."])
def test_cloud_upload_refuses_paths(name):
    with pytest.raises(ValueError, match="plain file name"):
        CloudMediaUpload(name, "application/octet-stream", io.BytesIO(b"data"))


def test_cloud_upload_retains_only_a_safe_name_type_and_stream():
    upload = CloudMediaUpload("source.pdf", "application/pdf", io.BytesIO(b"%PDF-test"))

    assert upload.file_name == "source.pdf"
    assert upload.stream.read() == b"%PDF-test"


def _response(*, account_id: int = 41) -> CloudExecutionResponse:
    return CloudExecutionResponse(
        execution_id="cloud-execution-1",
        status="completed",
        route=CloudCapabilityRoute(
            capability="ocr",
            engine="anthropic/claude-haiku-4-5",
            provider="anthropic",
            lane="frisket_credits",
            route_revision="route-1",
            ready=True,
            blocker=None,
            price=None,
            options=CloudOCROptionSupport(
                kind="ocr", language=True, geometry=True, searchable_pdf=False
            ),
        ),
        result=CloudOCRResult(pages=[]),
        receipt=CloudExecutionReceipt(
            user_id=7,
            account_id=account_id,
            lane="frisket_credits",
            provider="anthropic",
            engine="anthropic/claude-haiku-4-5",
            charge_micro=25_000,
            provider_cost_micro=None,
            usage=CloudOCRUsage(kind="ocr", pages=2),
        ),
    )


def _store(tmp_path) -> CloudConnectionStore:
    store = CloudConnectionStore(tmp_path / "cloud-profile")
    store.save_connection(
        "installation-bearer",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[
                CloudAccount(
                    id=41,
                    name="Personal",
                    kind="personal",
                    balance_micro=1_000_000,
                    available_micro=1_000_000,
                    manage_billing=True,
                )
            ],
        ),
    )
    return store


def _quote(*, charge_micro: int | None = 25_000) -> CloudQuoteChallenge:
    return CloudQuoteChallenge(
        error="needs_confirmation",
        confirmation="quote-confirmation",
        quote=CloudExecutionQuote(
            account_id=41,
            route_revision="route-1",
            lane="frisket_credits",
            provider="anthropic",
            engine="anthropic/claude-haiku-4-5",
            estimated_charge_micro=charge_micro,
            provider_cost_micro=None,
            expires_at="2099-01-01T00:00:00Z",
        ),
    )


def _llm_request() -> CloudLLMRequest:
    return CloudLLMRequest(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=32,
        temperature=0,
    )


@pytest.mark.asyncio
async def test_http_transport_echoes_only_an_exact_authorized_quote(tmp_path):
    seen: list[tuple[str | None, str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.headers.get("X-Frisket-Confirmation"),
                request.headers["Idempotency-Key"],
                request.headers["Authorization"],
            )
        )
        if len(seen) == 1:
            return httpx.Response(409, json=_quote().model_dump(mode="json"))
        return httpx.Response(200, json=_llm_response().model_dump(mode="json"))

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    attempt = CloudExecutionAttempt(
        "durable-request-1", _payer(), CloudQuoteAuthority(25_000)
    )

    response = await transport.execute_llm(attempt, _llm_request())

    assert response.execution_id == "cloud-execution-1"
    assert seen == [
        (None, "durable-request-1", "Bearer installation-bearer"),
        ("quote-confirmation", "durable-request-1", "Bearer installation-bearer"),
    ]


@pytest.mark.asyncio
async def test_http_transport_does_not_echo_quote_above_local_ceiling(tmp_path):
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            409, json=_quote(charge_micro=25_001).model_dump(mode="json")
        )

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    attempt = CloudExecutionAttempt(
        "durable-request-1", _payer(), CloudQuoteAuthority(25_000)
    )

    with pytest.raises(CloudConfirmationRequired):
        await transport.execute_llm(attempt, _llm_request())
    assert requests == 1


@pytest.mark.asyncio
async def test_http_transport_retries_uncertain_post_with_identical_key_and_body(
    tmp_path,
):
    seen: list[tuple[str, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        seen.append((request.headers["Idempotency-Key"], body))
        if len(seen) == 1:
            raise httpx.ConnectError("lost response", request=request)
        return httpx.Response(200, json=_llm_response().model_dump(mode="json"))

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    attempt = CloudExecutionAttempt("durable-request-1", _payer(), None)

    await transport.execute_llm(attempt, _llm_request())

    assert len(seen) == 2
    assert seen[0] == seen[1]


@pytest.mark.asyncio
async def test_http_media_transport_repeats_plural_files_field(tmp_path):
    captured = b""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = await request.aread()
        return httpx.Response(200, json=_response().model_dump(mode="json"))

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    attempt = CloudExecutionAttempt("durable-request-1", _payer(), None)

    await transport.execute_media(
        attempt,
        capability="ocr",
        engine=attempt.payer.engine,
        options=CloudOCROptions(geometry=True),
        uploads=[
            CloudMediaUpload("page-1.png", "image/png", io.BytesIO(b"one")),
            CloudMediaUpload("page-2.png", "image/png", io.BytesIO(b"two")),
        ],
    )

    assert captured.count(b'name="files"') == 2
    assert b'name="file"' not in captured
    metadata = json.loads(
        captured.split(b'name="metadata"', 1)[1]
        .split(b"\r\n\r\n", 1)[1]
        .split(b"\r\n", 1)[0]
    )
    assert metadata["account_id"] == 41
    assert metadata["options"]["geometry"] is True


@pytest.mark.asyncio
async def test_http_transport_projects_execution_conflict_as_safe_refusal(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": "execution_conflict",
                "message": "request differs",
                "retryable": False,
            },
        )

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CloudExecutionRefused) as error:
        await transport.execute_llm(
            CloudExecutionAttempt("durable-request-1", _payer(), None),
            _llm_request(),
        )
    assert error.value.code == "execution_conflict"


@pytest.mark.asyncio
async def test_http_transport_refuses_reconnected_user_before_network(tmp_path):
    store = _store(tmp_path)
    store.save_connection(
        "user-8-bearer",
        CloudSessionResponse(
            user=CloudUser(id=8, email="other@example.test", display_name="Other"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[
                CloudAccount(
                    id=41,
                    name="Shared",
                    kind="organization",
                    balance_micro=1_000_000,
                    available_micro=1_000_000,
                    manage_billing=True,
                )
            ],
        ),
    )
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=_llm_response().model_dump(mode="json"))

    transport = HttpCloudExecutionTransport(
        store,
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CloudExecutionRefused) as error:
        await transport.execute_llm(
            CloudExecutionAttempt("durable-request-1", _payer(), None),
            _llm_request(),
        )
    assert error.value.code == "account_forbidden"
    assert requests == 0


@pytest.mark.asyncio
async def test_http_transport_preserves_terminal_failure_code(tmp_path):
    terminal = _llm_response().model_copy(
        update={
            "status": "failed",
            "result": None,
            "receipt": None,
            "error": CloudErrorResponse(
                error="provider_failed",
                message="Provider refused the request",
                retryable=False,
                execution_id="cloud-execution-1",
            ),
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=terminal.model_dump(mode="json"))

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CloudExecutionRefused) as error:
        await transport.execute_llm(
            CloudExecutionAttempt("durable-request-1", _payer(), None),
            _llm_request(),
        )
    assert error.value.code == "provider_failed"
    assert str(error.value) == "Provider refused the request"
    assert error.value.retryable is False


def _llm_response() -> CloudExecutionResponse:
    response = _response()
    return response.model_copy(
        update={
            "route": response.route.model_copy(
                update={
                    "capability": "llm.complete",
                    "options": CloudLLMOptionSupport(
                        kind="llm",
                        structured_output=True,
                        tools=True,
                        vision=False,
                    ),
                }
            ),
            "result": None,
            "receipt": response.receipt.model_copy(
                update={
                    "usage": CloudLLMUsage(
                        kind="llm.complete", tokens_in=2, tokens_out=3
                    )
                }
            ),
        }
    )


def test_media_binding_refuses_an_engine_other_than_the_admitted_route():
    attempt = CloudExecutionAttempt("request-1", _payer(), None)
    ctx = OpContext(
        cloud_execution=CloudMediaExecutionBinding(object(), attempt, "ocr")
    )

    with pytest.raises(CloudExecutionRefused, match="engine differs"):
        cloud_media_execution(ctx, "ocr", "another/model")


def test_llm_binding_is_scoped_and_validates_the_admitted_model():
    attempt = CloudExecutionAttempt("request-1", _payer(), None)
    transport = object()
    binding = CloudLLMExecutionBinding(transport, attempt)

    with pytest.raises(CloudExecutionRefused) as unbound:
        cloud_llm_execution(attempt.payer.engine)
    assert unbound.value.code == "cloud_execution_unbound"

    with cloud_llm_execution_scope(binding):
        assert cloud_llm_execution(attempt.payer.engine) == (transport, attempt)
        with pytest.raises(CloudExecutionRefused) as changed:
            cloud_llm_execution("another/model")
        assert changed.value.code == "route_changed"

    with pytest.raises(CloudExecutionRefused) as reset:
        cloud_llm_execution(attempt.payer.engine)
    assert reset.value.code == "cloud_execution_unbound"


def test_llm_binding_nested_scope_restores_the_outer_attempt():
    outer = CloudExecutionAttempt("request-outer", _payer(), None)
    inner_payer = _payer()
    inner = CloudExecutionAttempt("request-inner", inner_payer, None)
    outer_binding = CloudLLMExecutionBinding(object(), outer)
    inner_binding = CloudLLMExecutionBinding(object(), inner)

    with cloud_llm_execution_scope(outer_binding):
        with cloud_llm_execution_scope(inner_binding):
            assert cloud_llm_execution(inner_payer.engine)[1] is inner
        assert cloud_llm_execution(outer.payer.engine)[1] is outer


def test_cloud_accounting_preserves_charge_and_unknown_provider_cost():
    attempt = CloudExecutionAttempt("request-1", _payer(), None)

    accounting = cloud_execution_accounting(attempt, _response(), capability="ocr")

    assert accounting["cost"] == 0.025
    assert accounting["cloud_receipt"]["charge_micro"] == 25_000
    [call] = accounting["model_calls"]
    assert call["id"] == "cloud-execution-1"
    assert call["provider_cost_usd"] is None
    assert call["units"]["cloud_account_id"] == 41
    assert call["units"]["cloud_lane"] == "frisket_credits"


def test_cloud_accounting_refuses_a_changed_account():
    attempt = CloudExecutionAttempt("request-1", _payer(), None)

    with pytest.raises(CloudExecutionRefused, match="differs"):
        cloud_execution_accounting(attempt, _response(account_id=99), capability="ocr")


def test_cloud_payer_changes_route_and_consent_identity_only_when_present():
    payer = _payer()
    common = {
        "operator": "frisket",
        "egress_class": "frisket_shared",
        "region": None,
        "credential_source": "platform_key",
        "cost_posture": "platform_metered",
    }

    legacy = route_fact_hash(**common)
    cloud = route_fact_hash(**common, cloud_payer=payer.to_snapshot())
    assert cloud != legacy

    facts = RouteRowFacts(
        target_id="frisket-cloud:41:anthropic",
        engine=payer.engine,
        cloud_payer=payer,
        **common,
    )
    promises = compile_route_promises(facts, OperatorBorneZeroCost())
    cloud_claim = next(
        promise for promise in promises.promises if promise.field == "cloud_payer"
    )
    assert cloud_claim.value == payer.to_snapshot()
    assert cloud_claim.audience == "user_claim"

    snapshot = validated_target_snapshot(
        {
            "target_id": facts.target_id,
            "capability": "ocr",
            "transport": "remote",
            "run_scoped": False,
            "cloud_payer": payer.to_snapshot(),
        }
    )
    assert snapshot["cloud_payer"] == payer.to_snapshot()

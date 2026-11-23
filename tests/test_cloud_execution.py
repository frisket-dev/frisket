from __future__ import annotations
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio
import io
import json
import traceback
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default as email_policy

import httpx
import pytest

from frisket.execution.dispatch_control import STANDALONE
from frisket.cloud_connection import CloudConnectionStore
from frisket.cloud_execution import (
    CloudExecutionAttempt,
    CloudExecutionRefused,
    CloudLLMExecutionBinding,
    CloudMediaExecutionBinding,
    CloudMediaUpload,
    CloudPayerSubject,
    CloudEstimateAuthority,
    CloudConfirmationRequired,
    HttpCloudExecutionTransport,
    cloud_execution_accounting,
    cloud_estimate_authority_for_row,
    cloud_llm_execution,
    cloud_llm_execution_scope,
    cloud_media_execution,
)
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilityRoute,
    CloudEstimateChallenge,
    CloudExecutionEstimate,
    CloudExecutionAccounting,
    CloudErrorResponse,
    CloudLLMOptionSupport,
    CloudLLMResult,
    CloudLLMUsage,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudOCROptionSupport,
    CloudOCROptions,
    CloudOCRResult,
    CloudOCRUsage,
    CloudSessionResponse,
    CloudTokenPrice,
    CloudUser,
    CloudLLMRequest,
    CloudLLMExecutionRequest,
)
from frisket.ops.base import ActionContext
from frisket.engine.store.execution_routes import route_fact_hash
from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    PricedCostBasis,
    UnpriceableCost,
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
        estimate_authority=CloudEstimateAuthority(maximum_charge_micro=25_000),
    )

    assert attempt.request_id == "cloud-request-1"
    assert attempt.payer.account_id == 41
    assert "token" not in repr(attempt).lower()


def _llm_request() -> CloudLLMRequest:
    return CloudLLMRequest(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": "Read this image"}],
        max_tokens=128,
        temperature=0,
    )


def test_cloud_llm_execution_request_defaults_to_llm_and_accepts_exact_ocr():
    default_request = CloudLLMExecutionRequest(
        account_id=41,
        route_revision="route-1",
        request=_llm_request(),
    )
    ocr_request = CloudLLMExecutionRequest(
        capability="ocr",
        account_id=41,
        route_revision="route-2",
        request=_llm_request(),
    )

    assert default_request.capability == "llm.complete"
    assert ocr_request.capability == "ocr"


def test_cloud_ocr_usage_accepts_known_shape_or_explicitly_unknown_usage():
    page_usage = CloudOCRUsage(kind="ocr", meter="pages", pages=2)
    assert page_usage.pages == 2
    assert page_usage.model_dump(mode="json") == {
        "kind": "ocr",
        "meter": "pages",
        "pages": 2,
    }
    token_usage = CloudOCRUsage(
        kind="ocr", meter="tokens", tokens_in=120, tokens_out=24
    )
    assert (token_usage.tokens_in, token_usage.tokens_out) == (120, 24)
    assert token_usage.model_dump(mode="json") == {
        "kind": "ocr",
        "meter": "tokens",
        "tokens_in": 120,
        "tokens_out": 24,
    }

    unknown = CloudOCRUsage(kind="ocr", meter="tokens")
    assert unknown.model_dump(mode="json") == {"kind": "ocr", "meter": "tokens"}
    assert unknown.pages is unknown.tokens_in is unknown.tokens_out is None

    for usage in (
        {"kind": "ocr", "meter": "tokens", "tokens_in": 120},
        {"kind": "ocr", "meter": "tokens", "tokens_out": 24},
        {
            "kind": "ocr",
            "meter": "pages",
            "pages": 2,
            "tokens_in": 120,
            "tokens_out": 24,
        },
    ):
        with pytest.raises(ValueError, match="cannot mix pages and token counts"):
            CloudOCRUsage.model_validate(usage)


def test_cloud_row_estimate_authority_keeps_cohort_without_price_cap():
    basis = PricedCostBasis(
        pricing_key="cloud.ocr",
        unit_rate="0.0025",
        estimated_quantity="5",
        quantity_unit="page",
        terms_version="route-1",
        quantity_rounding_mode="half_even",
        quantity_rounding_decimal_places=6,
        meter_key="pages",
        meter_units_per_quantity_unit="1",
        ceiling_mode="consented_quantity",
        row_settlement_mode="estimated_successful_rows",
        charge_authority="frisket_cloud_receipt",
        row_estimate_quantities=((11, "2"), (12, "3")),
    )

    authority = cloud_estimate_authority_for_row(basis, row_id=11)

    assert authority.maximum_charge_micro is None


def test_cloud_unknown_cost_consent_is_explicitly_unbounded():
    authority = cloud_estimate_authority_for_row(UnpriceableCost(), row_id=11)

    assert authority.maximum_charge_micro is None


def test_cloud_priced_unknown_quantity_consent_is_explicitly_unbounded():
    basis = PricedCostBasis(
        pricing_key="cloud.document.convert",
        unit_rate="0.01",
        estimated_quantity=None,
        quantity_unit="page",
        terms_version="route-1",
        quantity_rounding_mode="half_even",
        quantity_rounding_decimal_places=6,
        meter_key="pages",
        meter_units_per_quantity_unit="1",
        ceiling_mode="consented_quantity",
        row_settlement_mode="estimated_successful_rows",
        charge_authority="frisket_cloud_receipt",
    )

    authority = cloud_estimate_authority_for_row(basis, row_id=11)

    assert authority.maximum_charge_micro is None


def test_cloud_fractional_unit_approval_does_not_become_a_price_cap():
    basis = PricedCostBasis(
        pricing_key="cloud.transcribe",
        unit_rate="0.000133",
        estimated_quantity="10.5",
        quantity_unit="audio_second",
        terms_version="route-1",
        quantity_rounding_mode="half_even",
        quantity_rounding_decimal_places=6,
        meter_key="audio_seconds",
        meter_units_per_quantity_unit="1",
        ceiling_mode="consented_quantity",
        row_settlement_mode="estimated_successful_rows",
        charge_authority="frisket_cloud_receipt",
        row_estimate_quantities=((11, "10.5"),),
    )

    authority = cloud_estimate_authority_for_row(basis, row_id=11)

    assert authority.maximum_charge_micro is None


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
            usage=CloudOCRUsage(kind="ocr", meter="pages", pages=2),
        ),
    )


@pytest.mark.asyncio
async def test_cloud_pdf_companion_http_poll_and_recovery_keep_one_purchase(tmp_path):
    payload = _response().model_dump(mode="json")
    payload["route"].update(engine="pp-ocrv6", provider="frisket_models")
    payload["route"]["price"] = {"kind": "unit", "unit": "page", "micro_per_unit": 2500}
    payload["route"]["options"]["pdf_input"] = True
    payload["receipt"].update(
        engine="pp-ocrv6", provider="frisket_models", charge_micro=5000
    )
    payload["result"] = {
        "pages": [
            {"page": 1, "text": "", "blocks": [], "width": 100, "height": 200},
            {"page": 2, "text": "last", "blocks": [], "width": 200, "height": 100},
        ]
    }
    original = b"%PDF-original-document"
    requests = []

    async def handler(request):
        assert request.headers.get("X-Frisket-OCR-Contract") == "pdf-v1"
        assert request.headers.get("X-Frisket-Cloud-Protocol") == "2"
        assert request.headers["Idempotency-Key"] == "one-pdf-purchase"
        requests.append(request.method)
        if request.method == "POST":
            body = await request.aread()
            assert original in body
            assert b'"pdf_dpi":200' in body
            running = {**payload, "status": "running", "result": None, "receipt": None}
            return httpx.Response(202, json=running)
        assert request.method in {"GET", "DELETE"}
        return httpx.Response(200, json=payload)

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    attempt = CloudExecutionAttempt(
        "one-pdf-purchase",
        replace(_payer(), engine="pp-ocrv6", provider="frisket_models"),
        CloudEstimateAuthority(5000),
    )
    result = await transport.execute_media(
        attempt,
        capability="ocr",
        engine="pp-ocrv6",
        options=CloudOCROptions.model_validate({"geometry": True, "pdf_dpi": 200}),
        uploads=[
            CloudMediaUpload("source.pdf", "application/pdf", io.BytesIO(original))
        ],
    )
    recovered = await transport.recover(attempt, result.execution_id)
    assert recovered == result
    assert await transport.cancel(attempt, result.execution_id) == result
    assert requests == ["POST", "GET", "GET", "DELETE"]
    assert recovered.receipt.usage.pages == 2
    assert recovered.result.pages[1].width == 200


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


def _estimate(*, charge_micro: int | None = 25_000) -> CloudEstimateChallenge:
    return CloudEstimateChallenge(
        error="needs_confirmation",
        confirmation="quote-confirmation",
        estimate=CloudExecutionEstimate(
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


def _account_key_estimate(
    *,
    provider_cost_micro: int | None,
) -> CloudEstimateChallenge:
    estimate = _estimate(charge_micro=0).estimate.model_copy(
        update={"lane": "account_key", "provider_cost_micro": provider_cost_micro}
    )
    return CloudEstimateChallenge(
        error="needs_confirmation",
        confirmation="account-key-confirmation",
        estimate=estimate,
    )


def _llm_request() -> CloudLLMRequest:
    return CloudLLMRequest(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=32,
        temperature=0,
    )


@pytest.mark.asyncio
async def test_http_transport_echoes_only_an_exact_authorized_estimate(tmp_path):
    seen: list[tuple[str | None, str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Frisket-Cloud-Protocol"] == "2"
        seen.append(
            (
                request.headers.get("X-Frisket-Confirmation"),
                request.headers["Idempotency-Key"],
                request.headers["Authorization"],
            )
        )
        if len(seen) == 1:
            return httpx.Response(
                409,
                json={
                    "error": "needs_confirmation",
                    "confirmation": "quote-confirmation",
                    "estimate": {
                        "account_id": 41,
                        "route_revision": "route-1",
                        "lane": "frisket_credits",
                        "provider": "anthropic",
                        "engine": "anthropic/claude-haiku-4-5",
                        "estimated_charge_micro": 25_000,
                        "provider_cost_micro": None,
                        "expires_at": "2099-01-01T00:00:00Z",
                    },
                },
            )
        return httpx.Response(200, json=_llm_response().model_dump(mode="json"))

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    attempt = CloudExecutionAttempt(
        "durable-request-1", _payer(), CloudEstimateAuthority(25_000)
    )

    response = await transport.execute_llm(attempt, _llm_request())

    assert response.execution_id == "cloud-execution-1"
    assert seen == [
        (None, "durable-request-1", "Bearer installation-bearer"),
        ("quote-confirmation", "durable-request-1", "Bearer installation-bearer"),
    ]


@pytest.mark.asyncio
async def test_http_transport_does_not_echo_estimate_above_local_ceiling(tmp_path):
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            409, json=_estimate(charge_micro=25_001).model_dump(mode="json")
        )

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    attempt = CloudExecutionAttempt(
        "durable-request-1", _payer(), CloudEstimateAuthority(25_000)
    )

    with pytest.raises(CloudConfirmationRequired):
        await transport.execute_llm(attempt, _llm_request())
    assert requests == 1


def test_account_key_estimate_ceiling_binds_provider_cost_not_zero_cloud_charge():
    authority = CloudEstimateAuthority(25_000)
    attempt = CloudExecutionAttempt(
        "durable-request-1",
        replace(_payer(), lane="account_key"),
        authority,
    )

    assert HttpCloudExecutionTransport._estimate_is_authorized(
        attempt, _account_key_estimate(provider_cost_micro=25_000), authority
    )
    assert not HttpCloudExecutionTransport._estimate_is_authorized(
        attempt, _account_key_estimate(provider_cost_micro=25_001), authority
    )
    assert not HttpCloudExecutionTransport._estimate_is_authorized(
        attempt, _account_key_estimate(provider_cost_micro=None), authority
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.RemoteProtocolError,
    ],
)
async def test_http_transport_retries_uncertain_post_with_identical_key_and_body(
    tmp_path,
    transport_error,
):
    seen: list[tuple[str, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        seen.append((request.headers["Idempotency-Key"], body))
        if len(seen) == 1:
            raise transport_error("lost response", request=request)
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
async def test_http_model_transport_sends_and_validates_exact_ocr_capability(tmp_path):
    captured: dict = {}
    base = _response()
    response = base.model_copy(
        update={
            "route": base.route.model_copy(
                update={
                    "price": CloudTokenPrice(
                        kind="tokens",
                        input_micro_per_million_tokens=250_000,
                        output_micro_per_million_tokens=2_000_000,
                    )
                }
            ),
            "result": CloudLLMResult(
                content='{"text":"Cloud OCR"}',
                data={"text": "Cloud OCR"},
                tokens_in=12,
                tokens_out=4,
                cost=0.00003,
                model=base.route.engine,
                cost_source="cloud_receipt",
                cached=False,
                provider=base.route.provider,
                credential_source="platform_key",
                raw={},
                output_limited=False,
            ),
            "receipt": base.receipt.model_copy(
                update={
                    "provider_cost_micro": 20,
                    "usage": CloudOCRUsage(
                        kind="ocr", meter="tokens", tokens_in=12, tokens_out=4
                    ),
                }
            ),
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(await request.aread()))
        return httpx.Response(200, json=response.model_dump(mode="json"))

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    result = await transport.execute_llm(
        CloudExecutionAttempt("durable-ocr-request-1", _payer(), None),
        _llm_request(),
        capability="ocr",
    )

    assert captured["capability"] == "ocr"
    assert result.route.capability == "ocr"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", [httpx.ConnectError, httpx.ReadTimeout])
async def test_http_transport_marks_lost_create_response_as_ambiguous(
    tmp_path, transport_error
):
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise transport_error("sensitive proxy URL/token", request=request)

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

    assert error.value.code == "cloud_unreachable"
    assert error.value.post_egress_ambiguous is True
    assert error.value.retryable is True
    assert error.value.cloud_request_id is None
    assert error.value.accounting is None
    assert "sensitive" not in "".join(traceback.format_exception(error.value))
    assert requests == 2


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
    assert error.value.post_egress_ambiguous is False


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


@pytest.mark.asyncio
async def test_http_transport_preserves_terminal_failure_receipt_accounting(tmp_path):
    terminal = _llm_response().model_copy(
        update={
            "status": "failed",
            "error": CloudErrorResponse(
                error="provider_failed",
                message="Provider charged before failing",
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
    assert error.value.accounting is not None
    assert error.value.accounting["cost"] == 0.025
    assert error.value.accounting["cloud_receipt"]["charge_micro"] == 25_000
    assert error.value.provider_job_accepted is False
    assert error.value.post_egress_ambiguous is False


@pytest.mark.asyncio
@pytest.mark.parametrize("with_receipt", [False, True])
async def test_http_transport_marks_reconciliation_as_ambiguous(tmp_path, with_receipt):
    response = _llm_response()
    terminal = response.model_copy(
        update={
            "status": "reconciliation_required",
            "receipt": response.receipt if with_receipt else None,
            "error": CloudErrorResponse(
                error="reconciliation_required",
                message="Provider outcome needs reconciliation",
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

    assert error.value.code == "reconciliation_required"
    assert error.value.provider_job_accepted is True
    assert error.value.post_egress_ambiguous is True
    assert (error.value.accounting is not None) is with_receipt


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
    ctx = ActionContext(
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


def test_account_key_accounting_uses_provider_cost_not_zero_cloud_charge():
    payer = replace(_payer(), lane="account_key")
    attempt = CloudExecutionAttempt("request-1", payer, None)
    response = _response().model_copy(
        update={
            "route": _response().route.model_copy(update={"lane": "account_key"}),
            "receipt": _response().receipt.model_copy(
                update={
                    "lane": "account_key",
                    "charge_micro": 0,
                    "provider_cost_micro": 25_000,
                }
            ),
        }
    )

    accounting = cloud_execution_accounting(attempt, response, capability="ocr")

    assert accounting["cost"] == 0.025
    [call] = accounting["model_calls"]
    assert call["provider_cost_usd"] == 0.025
    assert call["units"]["cloud_charge_micro"] == 0


async def _media_request_parts(request: httpx.Request):
    # Multipart boundaries may vary; compare the complete metadata and file parts.
    body = await request.aread()
    message = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {request.headers['Content-Type']}\r\n\r\n".encode() + body
    )
    return tuple(
        (
            part.get_param("name", header="content-disposition"),
            part.get_filename(),
            part.get_content_type(),
            part.get_payload(decode=True),
        )
        for part in message.iter_parts()
    )


async def _execute_admission_test_media(transport, request_id="media-admission"):
    return await transport.execute_media(
        CloudExecutionAttempt(request_id, _payer(), CloudEstimateAuthority(25_000)),
        capability="ocr",
        engine=_payer().engine,
        options=CloudOCROptions(geometry=True),
        uploads=[
            CloudMediaUpload(
                "source.png", "image/png", io.BytesIO(b"complete-original-image")
            )
        ],
    )


def _admission_refusal(**changes):
    return {
        "error": "rate_limited",
        "message": "Concurrent media admission is full",
        "retryable": True,
        **changes,
    }


@pytest.mark.asyncio
async def test_parallel_media_admission_waits_for_capacity_without_losing_files(
    tmp_path, monkeypatch
):
    real_sleep = asyncio.sleep

    async def fast_wait(_seconds):
        await real_sleep(0.001)

    monkeypatch.setattr("frisket.cloud_execution.asyncio.sleep", fast_wait)
    active = 0
    refused = 0
    accepted = []
    seen = {}

    async def handler(request):
        nonlocal active, refused
        assert request.method == "POST"
        key = request.headers["Idempotency-Key"]
        seen.setdefault(key, []).append(await _media_request_parts(request))
        if active >= 2:
            refused += 1
            return httpx.Response(429, json=_admission_refusal())
        active += 1
        try:
            await real_sleep(0.01)
            accepted.append(key)
            return httpx.Response(200, json=_response().model_dump(mode="json"))
        finally:
            active -= 1

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    results = await asyncio.gather(
        *(_execute_admission_test_media(transport, f"parallel-{i}") for i in range(6)),
        return_exceptions=True,
    )
    assert all(isinstance(result, CloudExecutionResponse) for result in results), (
        results
    )
    assert refused >= 4
    assert sorted(accepted) == [f"parallel-{i}" for i in range(6)]
    assert all(parts == attempts[0] for attempts in seen.values() for parts in attempts)
    assert all(
        attempts[0][1][3] == b"complete-original-image" for attempts in seen.values()
    )


@pytest.mark.asyncio
async def test_media_admission_retry_keeps_estimate_confirmation_and_complete_body(
    tmp_path, monkeypatch
):
    real_sleep = asyncio.sleep

    async def fast_wait(_seconds):
        await real_sleep(0)

    monkeypatch.setattr("frisket.cloud_execution.asyncio.sleep", fast_wait)
    seen = []

    async def handler(request):
        seen.append(
            (
                request.headers["Idempotency-Key"],
                request.headers.get("X-Frisket-Confirmation"),
                await _media_request_parts(request),
            )
        )
        if len(seen) in {1, 3}:
            return httpx.Response(429, json=_admission_refusal())
        if len(seen) == 2:
            return httpx.Response(409, json=_estimate().model_dump(mode="json"))
        return httpx.Response(200, json=_response().model_dump(mode="json"))

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    result = await _execute_admission_test_media(transport)

    assert result.status == "completed"
    assert [key for key, _, _ in seen] == ["media-admission"] * 4
    assert [confirmation for _, confirmation, _ in seen] == [
        None,
        None,
        "quote-confirmation",
        "quote-confirmation",
    ]
    assert all(parts == seen[0][2] for _, _, parts in seen)
    assert seen[0][2][1][3] == b"complete-original-image"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error,retryable,execution_id",
    [
        (401, "invalid_token", False, None),
        (403, "account_forbidden", False, None),
        (429, "rate_limited", False, None),
        (429, "provider_failed", True, None),
        (503, "rate_limited", True, None),
        (429, "rate_limited", True, "existing-execution"),
    ],
)
async def test_media_admission_does_not_retry_other_refusals(
    tmp_path, status, error, retryable, execution_id
):
    seen = []

    def handler(request):
        seen.append(request.method)
        return httpx.Response(
            status,
            json=_admission_refusal(
                error=error, retryable=retryable, execution_id=execution_id
            ),
        )

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
        max_poll_seconds=0.02,
    )
    with pytest.raises(CloudExecutionRefused) as caught:
        await _execute_admission_test_media(transport)
    assert caught.value.code == error
    assert seen == ["POST"]


@pytest.mark.asyncio
async def test_media_admission_does_not_retry_terminal_provider_failure(tmp_path):
    seen = []
    failed = _response().model_dump(mode="json")
    failed.update(
        status="failed",
        result=None,
        error={
            "error": "provider_failed",
            "message": "Provider failed",
            "retryable": True,
        },
    )

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200, json=failed)

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CloudExecutionRefused, match="Provider failed"):
        await _execute_admission_test_media(transport)
    assert seen == ["POST"]


@pytest.mark.asyncio
async def test_media_admission_wait_is_cancellable_before_another_post(
    tmp_path, monkeypatch
):
    waiting = asyncio.Event()
    never_ready = asyncio.Event()
    seen = []

    async def wait_for_capacity(_seconds):
        waiting.set()
        await never_ready.wait()

    monkeypatch.setattr("frisket.cloud_execution.asyncio.sleep", wait_for_capacity)

    def handler(request):
        seen.append(request.method)
        return httpx.Response(429, json=_admission_refusal())

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(_execute_admission_test_media(transport))
    waiting_task = asyncio.create_task(waiting.wait())
    try:
        done, _ = await asyncio.wait(
            [task, waiting_task], timeout=1, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            await task
        assert waiting_task in done, "admission must enter a cancellable wait"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert seen == ["POST"]
    finally:
        task.cancel()
        waiting_task.cancel()
        await asyncio.gather(task, waiting_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_media_admission_wait_has_a_deadline(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.method)
        return httpx.Response(429, json=_admission_refusal())

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
        max_poll_seconds=0.01,
    )
    with pytest.raises(CloudExecutionRefused) as caught:
        await asyncio.wait_for(_execute_admission_test_media(transport), timeout=1)
    assert caught.value.code == "rate_limited"
    assert len(seen) >= 1


def test_account_key_accounting_keeps_unknown_provider_cost_unknown():
    payer = replace(_payer(), lane="account_key")
    attempt = CloudExecutionAttempt("request-1", payer, None)
    response = _response().model_copy(
        update={
            "route": _response().route.model_copy(update={"lane": "account_key"}),
            "receipt": _response().receipt.model_copy(
                update={
                    "lane": "account_key",
                    "charge_micro": 0,
                    "provider_cost_micro": None,
                }
            ),
        }
    )

    accounting = cloud_execution_accounting(attempt, response, capability="ocr")

    assert accounting["cost"] is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, payload, code, retryable",
    [
        (
            401,
            {
                "error": "connection_expired",
                "message": "Reconnect Frisket Cloud",
                "retryable": False,
            },
            "connection_expired",
            False,
        ),
        (
            429,
            {"error": "rate_limited", "message": "Try again later", "retryable": True},
            "rate_limited",
            True,
        ),
        (502, "<html>private proxy details</html>", "cloud_unavailable", True),
        (413, {"detail": "private input"}, "cloud_unavailable", False),
        (200, "not JSON: private input", "cloud_protocol_error", False),
        (202, {"wrong": "private input"}, "cloud_protocol_error", False),
        (409, {"error": "needs_confirmation"}, "cloud_protocol_error", False),
        (302, "private redirect target", "cloud_redirect_refused", False),
    ],
)
async def test_cloud_response_errors_keep_server_correlation_without_changing_identity(
    tmp_path, status, payload, code, retryable
):
    request_id = "e3a16f6a7b0049caa30abf49c9e25425"
    seen = []

    def handler(request):
        seen.append(request.headers["Idempotency-Key"])
        kwargs = {"json": payload} if isinstance(payload, dict) else {"text": payload}
        return httpx.Response(status, headers={"X-Request-ID": request_id}, **kwargs)

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
        max_poll_seconds=0,
    )
    with pytest.raises(CloudExecutionRefused) as caught:
        await transport.execute_llm(
            CloudExecutionAttempt("durable-purchase", _payer(), None), _llm_request()
        )
    error = caught.value
    assert error.code == code
    assert error.retryable is retryable
    assert error.cloud_request_id == request_id
    assert str(error).endswith(f"(Cloud request: {request_id})")
    assert "private" not in "".join(traceback.format_exception(error))
    assert error.accounting is None
    assert error.post_egress_ambiguous is False
    assert seen == ["durable-purchase"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        "",
        "secret-token",
        "a" * 33,
        "A" * 32,
        "a" * 31 + "\n",
        "a" * 32 + "," + "b" * 32,
    ],
)
async def test_cloud_request_correlation_rejects_untrusted_headers(tmp_path, header):
    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(
            lambda _: httpx.Response(
                503, text="private body", headers={"X-Request-ID": header}
            )
        ),
    )
    with pytest.raises(CloudExecutionRefused) as caught:
        await transport.execute_llm(
            CloudExecutionAttempt("durable-purchase", _payer(), None), _llm_request()
        )
    assert caught.value.cloud_request_id is None
    assert str(caught.value) == "Frisket Cloud could not complete the request"


@pytest.mark.asyncio
@pytest.mark.parametrize("poll", [False, True])
async def test_cloud_terminal_failure_keeps_response_correlation_and_receipt(
    tmp_path, poll
):
    request_id = "e3a16f6a7b0049caa30abf49c9e25425"
    execution_id = "b" * 32
    terminal = _llm_response().model_copy(
        update={
            "execution_id": execution_id,
            "status": "failed",
            "error": CloudErrorResponse(
                error="provider_failed", message="Provider failed", retryable=False
            ),
        }
    )
    seen = []

    def handler(request):
        seen.append(request.method)
        if poll and request.method == "POST":
            running = terminal.model_copy(
                update={
                    "status": "running",
                    "result": None,
                    "receipt": None,
                    "error": None,
                }
            )
            return httpx.Response(
                202,
                json=running.model_dump(mode="json"),
                headers={"X-Request-ID": "a" * 32},
            )
        return httpx.Response(
            200,
            json=terminal.model_dump(mode="json"),
            headers={"X-Request-ID": request_id},
        )

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
        poll_interval_seconds=0,
    )
    with pytest.raises(CloudExecutionRefused) as caught:
        await transport.execute_llm(
            CloudExecutionAttempt("durable-purchase", _payer(), None), _llm_request()
        )
    error = caught.value
    assert error.cloud_request_id == request_id
    assert error.cloud_execution_id == execution_id
    assert str(error) == (
        f"Provider failed (Cloud execution: {execution_id}) (Cloud request: {request_id})"
    )
    assert error.code == "provider_failed"
    assert error.retryable is False
    assert error.accounting["cloud_receipt"]["charge_micro"] == 25_000
    assert error.post_egress_ambiguous is False
    assert seen == (["POST", "GET"] if poll else ["POST"])


@pytest.mark.asyncio
async def test_cloud_lost_confirmed_response_reuses_exact_estimate_and_purchase(
    tmp_path,
):
    seen = []

    def handler(request):
        seen.append(
            (
                request.headers["Idempotency-Key"],
                request.headers.get("X-Frisket-Confirmation"),
                request.content,
            )
        )
        if len(seen) == 1:
            return httpx.Response(409, json=_estimate().model_dump(mode="json"))
        if len(seen) == 2:
            raise httpx.ReadTimeout("response lost after charge", request=request)
        return httpx.Response(200, json=_llm_response().model_dump(mode="json"))

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    result = await transport.execute_llm(
        CloudExecutionAttempt(
            "durable-purchase",
            _payer(),
            CloudEstimateAuthority(maximum_charge_micro=25_000),
        ),
        _llm_request(),
    )
    assert result.status == "completed"
    assert len(seen) == 3
    assert seen[0][1] is None
    assert seen[1][1] == "quote-confirmation"
    assert seen[1] == seen[2]
    assert {entry[0] for entry in seen} == {"durable-purchase"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "wrong_capability", "deadline"])
async def test_cloud_poll_failure_does_not_resubmit_purchase(tmp_path, failure):
    seen = []
    execution_id = "b" * 32
    completed = _llm_response().model_copy(update={"execution_id": execution_id})

    def handler(request):
        seen.append(request.method)
        if request.method == "POST":
            running = completed.model_copy(
                update={"status": "running", "result": None, "receipt": None}
            )
            return httpx.Response(202, json=running.model_dump(mode="json"))
        if failure == "network":
            raise httpx.ReadTimeout("private connection details", request=request)
        changed = _response()
        return httpx.Response(
            200,
            json=changed.model_dump(mode="json"),
            headers={"X-Request-ID": "a" * 32},
        )

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
        poll_interval_seconds=0,
        max_poll_seconds=0 if failure == "deadline" else 60,
    )
    with pytest.raises(CloudExecutionRefused) as caught:
        await transport.execute_llm(
            CloudExecutionAttempt("durable-purchase", _payer(), None), _llm_request()
        )
    assert (
        caught.value.code
        == {
            "network": "cloud_unreachable",
            "wrong_capability": "route_changed",
            "deadline": "cloud_execution_pending",
        }[failure]
    )
    assert caught.value.cloud_request_id == (
        "a" * 32 if failure == "wrong_capability" else None
    )
    assert caught.value.cloud_execution_id == execution_id
    assert str(caught.value).count(f"Cloud execution: {execution_id}") == 1
    assert "private" not in "".join(traceback.format_exception(caught.value))
    assert seen == (["POST"] if failure == "deadline" else ["POST", "GET"])


@pytest.mark.parametrize(
    "execution_id", ["a" * 32, "invalid secret URL", "a" * 33, "A" * 32]
)
@pytest.mark.parametrize("with_error", [False, True])
def test_cloud_terminal_failure_without_receipt_has_safe_execution_identity(
    execution_id, with_error
):
    terminal = _llm_response().model_copy(
        update={
            "execution_id": execution_id,
            "status": "failed",
            "receipt": None,
            "result": None,
            "error": CloudErrorResponse(
                error="provider_failed", message="Provider failed", retryable=False
            )
            if with_error
            else None,
        }
    )
    with pytest.raises(CloudExecutionRefused) as caught:
        HttpCloudExecutionTransport._require_success(
            CloudExecutionAttempt("durable-purchase", _payer(), None),
            terminal,
            "llm.complete",
        )
    error = caught.value
    assert error.cloud_execution_id == (
        execution_id if execution_id == "a" * 32 else None
    )
    assert (f"Cloud execution: {execution_id}" in str(error)) is (
        execution_id == "a" * 32
    )
    assert "secret" not in str(error)
    assert error.accounting is None
    assert error.cloud_request_id is None
    assert error.code == "provider_failed"
    assert error.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["recover", "cancel"])
async def test_cloud_known_execution_survives_standalone_network_failure(
    tmp_path, method
):
    seen = []
    execution_id = "b" * 32

    def handler(request):
        seen.append(request.method)
        raise httpx.ReadTimeout("private connection details", request=request)

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CloudExecutionRefused) as caught:
        await getattr(transport, method)(
            CloudExecutionAttempt("durable-purchase", _payer(), None), execution_id
        )
    assert caught.value.cloud_execution_id == execution_id
    assert caught.value.cloud_request_id is None
    assert caught.value.retryable is True
    assert caught.value.post_egress_ambiguous is True
    assert caught.value.accounting is None
    assert seen == (["GET"] if method == "recover" else ["DELETE"])


@pytest.mark.asyncio
@pytest.mark.parametrize("body_kind", ["valid", "html", "malformed_json"])
async def test_update_required_keeps_instruction_and_never_retries_cloud_purchase(
    tmp_path,
    body_kind,
):
    from frisket.ai.llm import LLMRequest, ModelRouter

    seen = []
    message = (
        "Update Frisket Desktop at https://frisket.dev/ before using Frisket Cloud."
    )

    if body_kind == "valid":
        message = (
            "Update Frisket Desktop at https://frisket.dev/ to use this Cloud release."
        )

    def handler(request):
        seen.append(request)
        if body_kind == "html":
            return httpx.Response(426, text="<html>Upgrade required</html>")
        if body_kind == "malformed_json":
            return httpx.Response(426, json={"detail": "Upgrade required"})
        return httpx.Response(
            426,
            json={"error": "invalid_request", "message": message, "retryable": False},
        )

    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(handler),
    )
    attempt = CloudExecutionAttempt(
        "update-required", _payer(), CloudEstimateAuthority(None)
    )
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        use_env_keys=False,
        cache_mode="off",
        max_retries=3,
    )
    try:
        with cloud_llm_execution_scope(CloudLLMExecutionBinding(transport, attempt)):
            with pytest.raises(CloudExecutionRefused) as caught:
                await router.for_execution(STANDALONE).complete_transport(
                    LLMRequest(
                        model=_payer().engine,
                        messages=[{"role": "user", "content": "hello"}],
                        max_tokens=128,
                    )
                )
    finally:
        await router.aclose()
    assert caught.value.code == "desktop_update_required"
    assert str(caught.value) == message
    assert caught.value.retryable is False
    assert len(seen) == 1
    assert seen[0].headers["X-Frisket-Cloud-Protocol"] == "2"


@pytest.mark.asyncio
async def test_failed_cloud_purchase_preserves_usage_while_accounting_is_pending(
    tmp_path,
):
    terminal = _llm_response().model_copy(
        update={
            "status": "failed",
            "receipt": None,
            "accounting": CloudExecutionAccounting(
                status="pending",
                usage=CloudLLMUsage(kind="llm.complete", tokens_in=20, tokens_out=5),
                provider_cost_micro=25000,
            ),
            "error": CloudErrorResponse(
                error="provider_failed",
                message="Provider failed after usage",
                retryable=False,
            ),
        }
    )
    transport = HttpCloudExecutionTransport(
        _store(tmp_path),
        "https://cloud.example.test",
        http_transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=terminal.model_dump(mode="json"))
        ),
    )
    with pytest.raises(CloudExecutionRefused) as caught:
        await transport.execute_llm(
            CloudExecutionAttempt("failed-known-usage", _payer(), None), _llm_request()
        )
    error = caught.value
    assert error.code == "provider_failed"
    assert error.accounting is not None
    assert error.accounting["cloud_accounting_status"] == "pending"
    assert error.accounting["cloud_receipt"] is None
    assert error.accounting["cost"] is None
    [call] = error.accounting["model_calls"]
    assert call["units"]["tokens_in"] == 20
    assert call["units"]["tokens_out"] == 5
    assert call["provider_cost_usd"] == 0.025

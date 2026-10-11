from __future__ import annotations

import io

import pytest

from frisket.cloud_execution import (
    CloudExecutionAttempt,
    CloudExecutionRefused,
    CloudMediaExecutionBinding,
    CloudMediaUpload,
    CloudPayerSubject,
    CloudQuoteAuthority,
    cloud_execution_accounting,
    cloud_media_execution,
)
from frisket.contracts.http.cloud_connection import (
    CloudCapabilityRoute,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudOCROptionSupport,
    CloudOCRResult,
    CloudOCRUsage,
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
    upload = CloudMediaUpload(
        "source.pdf", "application/pdf", io.BytesIO(b"%PDF-test")
    )

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


def test_media_binding_refuses_an_engine_other_than_the_admitted_route():
    attempt = CloudExecutionAttempt("request-1", _payer(), None)
    ctx = OpContext(
        cloud_execution=CloudMediaExecutionBinding(object(), attempt, "ocr")
    )

    with pytest.raises(CloudExecutionRefused, match="engine differs"):
        cloud_media_execution(ctx, "ocr", "another/model")


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
        cloud_execution_accounting(
            attempt, _response(account_id=99), capability="ocr"
        )


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

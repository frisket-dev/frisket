"""Chat search owns mandatory accounting without dropping returned results."""

import asyncio

import httpx
import pytest

from frisket.ai.research.search import SearchService
from frisket.execution.dispatch_control import STANDALONE, TransientDispatch
from frisket.execution.purchases import PurchaseAdmission, PurchasePolicyRequired
from frisket.server.services.project_qa_runner import _search_with_purchase


class RecordingPolicy:
    def __init__(self, *, fail_record=False):
        self.fail_record = fail_record
        self.admissions = []
        self.observations = []

    def admit_purchase(self, request):
        self.admissions.append(request)
        return PurchaseAdmission("metered", "operation-test")

    def record_purchase(self, request, admission, observation):
        self.observations.append(observation)
        if self.fail_record:
            raise RuntimeError("private accounting failure details")

    def fail_purchase(self, request, admission, error):
        pytest.fail("A known returned search must not be recorded as failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_when", ["before", "after_response"])
async def test_search_cancellation_stops_new_work_but_keeps_returned_usage(cancel_when):
    calls = []
    cancelled = cancel_when == "before"
    policy = RecordingPolicy()

    def transport(request):
        nonlocal cancelled
        calls.append(request)
        cancelled = True
        return httpx.Response(
            200, json={"results": [], "costDollars": {"total": 0.007}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service = SearchService(
            preference="exa",
            effective_keys={"exa": "fake-key"},
            credential_source="platform_key",
            http=http,
        )
        operation = _search_with_purchase(
            service,
            "query",
            policy=policy,
            dispatch=TransientDispatch(lambda: cancelled),
        )
        if cancel_when == "before":
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert calls == policy.admissions == policy.observations == []
        else:
            result = await operation
            assert result.usage.provider_cost_usd == 0.007
            assert len(calls) == len(policy.admissions) == len(policy.observations) == 1
            assert policy.observations[0].provider_cost_micro == 7000


@pytest.mark.asyncio
async def test_search_accounting_failure_retains_result_without_retry(caplog):
    calls = []
    policy = RecordingPolicy(fail_record=True)

    def transport(request):
        calls.append(request)
        return httpx.Response(
            200, json={"results": [], "costDollars": {"total": 0.007}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service = SearchService(
            preference="exa",
            effective_keys={"exa": "fake-key"},
            credential_source="platform_key",
            http=http,
        )
        result = await _search_with_purchase(
            service,
            "private search query",
            policy=policy,
            dispatch=STANDALONE,
        )
    assert result.usage.provider_cost_usd == 0.007
    assert len(calls) == len(policy.admissions) == len(policy.observations) == 1
    assert "purchase accounting could not be recorded" in caplog.text
    assert "private search query" not in caplog.text
    assert "private accounting failure details" not in caplog.text


@pytest.mark.asyncio
async def test_search_without_accounting_owner_refuses_before_egress():
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as http:
        service = SearchService(
            preference="exa",
            effective_keys={"exa": "fake-key"},
            credential_source="platform_key",
            http=http,
        )
        with pytest.raises(PurchasePolicyRequired):
            await _search_with_purchase(
                service,
                "query",
                policy=None,
                dispatch=STANDALONE,
            )
    assert calls == []

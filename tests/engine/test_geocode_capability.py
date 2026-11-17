"""Geocoder domain behavior through its typed, admitted capability."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest
from deterministic_time import controlled_time

from frisket.actions.geocode import GeocodeParams, geocode
from frisket.actions.types import Row
from frisket.engine.executor import geocode_capability as module
from frisket.engine.executor.geocode_capability import (
    AdmittedGeocoder,
    GeocodeEngineError,
)
from frisket.execution.credential_use import CredentialUseContext
from frisket.ops.base import OpContext, RecipeInvocationHalt


def _route(**overrides):
    from frisket.engine.store.execution_routes import RouteRow

    base = dict(
        id="route_TESTROUTE0000000000000000",
        subject_kind="run",
        subject_id="1",
        seq=1,
        predecessor_id=None,
        promise_set_id="pset_TEST",
        engine="nominatim",
        options={},
        target_snapshot={
            "target_id": "nominatim",
            "capability": "geocode",
            "transport": "nominatim.search",
            "run_scoped": False,
        },
        route_fact_hash="hash",
        operator="nominatim",
        egress_class="third_party_api",
        region=None,
        credential_source="none",
        cost_posture="operator_borne",
        created_at="2026-07-24T00:00:00+00:00",
    )
    base.update(overrides)
    return RouteRow(**base)


def _routed_ctx(handler, route) -> OpContext:
    """A dispatch-scoped OpContext carrying a routed admission pinned to
    ``route`` — the same ``extras[ATTEMPT_EXTRA]`` shape
    ``routed_admission_in_scope`` reads, built the way
    ``tests/ops/test_transcribe_route_binding.py`` builds it for
    transcription."""
    from frisket.execution.attempt import (
        ATTEMPT_EXTRA,
        AttemptCommitment,
        RoutedAdmission,
    )
    from frisket.execution.promise_compiler import OperatorBorneZeroCost
    from frisket.execution.provider import ConnectionConfig
    from frisket.execution.resolver import CandidateBinding, RouteRowFacts
    from frisket.execution.targets import ExecutionTarget

    snapshot = route.target_snapshot
    binding = CandidateBinding(
        facts=RouteRowFacts(
            target_id=snapshot["target_id"],
            engine=route.engine,
            operator=route.operator,
            egress_class=route.egress_class,
            region=route.region,
            credential_source=route.credential_source,
            cost_posture=route.cost_posture,
        ),
        connection=ConnectionConfig(),
        target=ExecutionTarget(
            id=snapshot["target_id"],
            operator=route.operator,
            egress_class=route.egress_class,
        ),
    )
    from frisket.execution.dispatch_control import STANDALONE

    extras = {
        "standalone_dispatch": STANDALONE,
        "row_id": 1,
        ATTEMPT_EXTRA: AttemptCommitment(
            attempt_id="attempt_TESTATTEMPT000000000000",
            run_id=1,
            seq=0,
            identity="identity",
            scope=(1,),
            admission=RoutedAdmission(
                head_route_id=route.id,
                head_promise_set_id=route.promise_set_id,
                route=route,
                promise_set=None,
                binding=binding,
                evaluation=None,
                admitted_by_consent_id=None,
            ),
            cost_basis=OperatorBorneZeroCost(),
            price_card_version=None,
        ),
    }
    return OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras=extras,
        credential_use_context=CredentialUseContext.open(),
    )


def _context(handler, *, engine="nominatim"):
    return _routed_ctx(
        handler,
        _route(
            engine=engine,
            operator=engine,
            credential_source="local" if engine == "opencage" else "none",
            target_snapshot={
                "target_id": engine,
                "capability": "geocode",
                "transport": "opencage.v1"
                if engine == "opencage"
                else "nominatim.search",
                "run_scoped": False,
            },
        ),
    )


async def _lookup(address, ctx):
    capability = AdmittedGeocoder(ctx)
    try:
        result = await capability.bind_row(ctx).lookup(address)
        return result, capability.accounting_by_row.get(1, {})
    finally:
        await capability.aclose()
        await ctx.http.aclose()


@pytest.fixture(autouse=True)
def fast_pace(monkeypatch):
    monkeypatch.setattr(module, "NOMINATIM_MIN_INTERVAL", 0.0)


def test_international_opencage_result_confidence_and_optional_coordinates(monkeypatch):
    monkeypatch.setenv("OPENCAGE_API_KEY", "oc-test")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "geometry": {"lat": 51.5034, "lng": -0.1276},
                        "formatted": "London, United Kingdom",
                        "confidence": 9,
                    }
                ]
            },
        )

    ctx = _context(handler, engine="opencage")

    async def run():
        cap = AdmittedGeocoder(ctx)
        try:
            result = await geocode(
                GeocodeParams(source="address", include_lat_lon=True),
                Row({"address": "10 Downing Street, London"}),
                cap.bind_row(ctx),
            )
            return result.output
        finally:
            await cap.aclose()
            await ctx.http.aclose()

    output = asyncio.run(run())
    assert seen[0].url.params["key"] == "oc-test"
    assert seen[0].url.params["q"] == "10 Downing Street, London"
    assert output.geo_point.value.lat == output.latitude == 51.5034
    assert output.geo_point.value.lon == output.longitude == -0.1276
    assert output.geo_point.confidence == 0.9
    assert output.geo_point.justification == "geocoded by opencage"
    assert "United Kingdom" in output.formatted_address


def test_pinned_nominatim_keeps_venue_when_key_appears(monkeypatch):
    monkeypatch.setenv("OPENCAGE_API_KEY", "added-after-consent")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json=[{"lat": "35.6764", "lon": "139.65", "display_name": "Tokyo, Japan"}],
        )

    result, accounting = asyncio.run(_lookup("東京都新宿区", _context(handler)))
    assert seen[0].url.host == "nominatim.openstreetmap.org"
    assert seen[0].headers["user-agent"].startswith("frisket/")
    assert result.geo_point.value.lat == 35.6764
    assert result.geo_point.justification == "geocoded by nominatim"
    assert accounting["cost"] == 0
    assert accounting["model_calls"][0]["cost_source"] == "free_public_api"
    assert accounting["model_calls"][0]["provider"] == "nominatim"


def test_pinned_opencage_refuses_missing_key_without_fallback(monkeypatch):
    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    calls = []
    with pytest.raises(RecipeInvocationHalt, match="credential"):
        asyncio.run(
            _lookup(
                "London",
                _context(lambda request: calls.append(request), engine="opencage"),
            )
        )
    assert not calls


@pytest.mark.parametrize("engine", ["google", "census"])
def test_no_undeclared_engine_can_dispatch(engine):
    calls = []
    with pytest.raises(RuntimeError, match="not supported"):
        asyncio.run(
            _lookup(
                "somewhere",
                _context(lambda request: calls.append(request), engine=engine),
            )
        )
    assert not calls


def test_unrouted_and_wrong_transport_refuse_without_http():
    calls = []
    ctx = _context(lambda request: calls.append(request))
    ctx.extras.clear()
    ctx.extras["row_id"] = 1
    with pytest.raises(RuntimeError, match="admitted"):
        asyncio.run(_lookup("Oxford", ctx))
    assert not calls
    ctx = _context(lambda request: calls.append(request))
    from frisket.execution.attempt import ATTEMPT_EXTRA

    attempt = ctx.extras[ATTEMPT_EXTRA]
    route = replace(
        attempt.admission.route,
        target_snapshot={"capability": "geocode", "transport": "other"},
    )
    ctx.extras[ATTEMPT_EXTRA] = replace(
        attempt, admission=replace(attempt.admission, route=route)
    )
    with pytest.raises(RecipeInvocationHalt, match="transport"):
        asyncio.run(_lookup("Oxford", ctx))
    assert not calls


def test_blank_address_does_no_work_and_no_match_keeps_provider_justification():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    result, accounting = asyncio.run(_lookup("  ", _context(handler)))
    assert not calls and result.geo_point.value is None
    assert accounting["model_calls"] == []
    result, accounting = asyncio.run(_lookup("Imaginary address", _context(handler)))
    assert len(calls) == 1 and result.geo_point.value is None
    assert result.geo_point.confidence == 0
    assert "nominatim" in result.geo_point.justification
    assert "Imaginary address" in result.geo_point.justification


def test_one_logical_call_and_closed_scope_refuse_reuse():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    ctx = _context(handler)

    async def run():
        cap = AdmittedGeocoder(ctx)
        bound = cap.bind_row(ctx)
        await bound.lookup("Oxford")
        with pytest.raises(RuntimeError, match="one lookup"):
            await bound.lookup("London")
        await cap.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await bound.lookup("Paris")
        await ctx.http.aclose()

    asyncio.run(run())
    assert len(calls) == 1


def test_pacing_is_shared_across_concurrent_rows(monkeypatch):
    monkeypatch.setattr(module, "NOMINATIM_MIN_INTERVAL", 1.1)
    monkeypatch.setattr(module, "_nominatim_last", 0.0)
    with controlled_time() as clock:
        clock.advance_seconds(100)
        sleeper = clock.async_sleeper()
        pace = module._nominatim_pace
        monkeypatch.setattr(
            module,
            "_nominatim_pace",
            lambda: pace(clock=clock.monotonic, sleep=sleeper),
        )
        stamps = []

        def handler(request):
            stamps.append(clock.monotonic())
            return httpx.Response(200, json=[])

        async def run():
            await asyncio.gather(
                _lookup("Paris", _context(handler)), _lookup("Lyon", _context(handler))
            )

        asyncio.run(run())
    assert stamps[1] - stamps[0] == pytest.approx(1.1)
    assert sleeper.sleeps == [pytest.approx(1.1)]


@pytest.mark.parametrize("final", ["success", "transport", "http", "json", "geometry"])
def test_retry_and_terminal_errors_keep_response_proven_accounting(monkeypatch, final):
    monkeypatch.setenv("OPENCAGE_API_KEY", "oc-test")

    async def sleep(_delay):
        pass

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429, text="slow down", headers={"x-request-id": "retry-1"}
            )
        if final == "transport":
            raise httpx.ConnectError("disconnected", request=request)
        if final == "http":
            return httpx.Response(402, text="quota")
        if final == "json":
            return httpx.Response(200, text="not-json")
        if final == "geometry":
            return httpx.Response(200, json={"results": [{"geometry": {}}]})
        return httpx.Response(
            200,
            json={"results": [{"geometry": {"lat": 1, "lng": 2}, "formatted": "x"}]},
        )

    if final == "success":
        result, accounting = asyncio.run(
            _lookup("address", _context(handler, engine="opencage"))
        )
        assert result.geo_point.value.lat == 1
    else:
        with pytest.raises(GeocodeEngineError) as error:
            asyncio.run(_lookup("address", _context(handler, engine="opencage")))
        accounting = error.value.accounting
    assert len(calls) == 2
    assert len(accounting["model_calls"]) == (1 if final == "transport" else 2)
    first = accounting["model_calls"][0]
    assert first["request_id"] == "retry-1"
    assert first["provider_cost_usd"] is None
    assert first["units"]["requests"] == 1
    if final != "success":
        assert all(
            fact["provider_cost_usd"] is None for fact in accounting["model_calls"]
        )


@pytest.mark.parametrize("kind", ["transport", "http", "geometry", "json"])
def test_owned_secret_is_redacted_before_error_truncation_and_chaining(
    monkeypatch, kind
):
    import traceback

    key = "opaque-owned-fixture-value-5379"
    monkeypatch.setenv("OPENCAGE_API_KEY", key)

    def handler(request):
        if kind == "transport":
            raise httpx.ConnectError(f"connection rejected {key}", request=request)
        if kind == "http":
            return httpx.Response(400, text="x" * 265 + f" authorization value {key}")
        if kind == "json":
            return httpx.Response(200, text=f"not JSON: {key}")
        return httpx.Response(
            200, json={"results": [{"geometry": {"lat": key, "lng": 0}}]}
        )

    with pytest.raises(GeocodeEngineError) as error:
        asyncio.run(_lookup("address", _context(handler, engine="opencage")))
    assert key[:12] not in str(error.value)
    assert key not in "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    if kind != "http":
        assert error.value.__suppress_context__


@pytest.mark.asyncio
async def test_geocode_refused_purchase_does_not_record_provider_attempt():
    from frisket.execution.purchases import PurchasePolicyRequired

    calls = []

    class RefusingPolicy:
        def admit_purchase(self, request):
            raise PurchasePolicyRequired("refused")

    ctx = _context(lambda request: calls.append(request), engine="opencage")
    ctx.extras["service_operation_policy"] = RefusingPolicy()
    geocoder = AdmittedGeocoder(ctx)
    try:
        with pytest.raises(PurchasePolicyRequired, match="refused"):
            await geocoder._get(
                ctx,
                module.OPENCAGE_URL,
                {},
                engine="opencage",
                pricing_key=module.GEOCODE_EXTERNAL_GEOCODER,
                credential_source="project_key",
            )
        assert calls == []
        assert geocoder.request_count == 0
        assert geocoder.accounting_by_row == {}
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "uncertain"),
    [
        (httpx.ReadTimeout, True),
        (httpx.WriteError, True),
        (httpx.ConnectError, False),
        (httpx.ConnectTimeout, False),
        (httpx.PoolTimeout, False),
    ],
)
async def test_geocode_transport_certainty_reaches_purchase_owner(
    error_type, uncertain
):
    from frisket.execution.purchases import LocalPurchasePolicy

    calls, failures = [], []

    class RecordingPolicy(LocalPurchasePolicy):
        def fail_purchase(self, request, admission, error):
            failures.append(error)

    def send(request):
        calls.append(request)
        raise error_type("fixture transport failure", request=request)

    ctx = _context(send, engine="opencage")
    ctx.extras["service_operation_policy"] = RecordingPolicy()
    geocoder = AdmittedGeocoder(ctx)
    try:
        with pytest.raises(GeocodeEngineError) as caught:
            await geocoder._get(
                ctx,
                module.OPENCAGE_URL,
                {},
                engine="opencage",
                pricing_key=module.GEOCODE_EXTERNAL_GEOCODER,
                credential_source="project_key",
            )
        assert failures == [caught.value]
        assert caught.value.post_egress_ambiguous is uncertain
        assert caught.value.purchase_dispatched is (None if uncertain else False)
        assert caught.value.accounting == geocoder.accounting_by_row.get(1)
        assert geocoder.request_count == int(uncertain)
        assert len(calls) == 1
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_geocode_record_failure_keeps_one_returned_fact(monkeypatch, cancelled):
    from frisket.execution.purchases import LocalPurchasePolicy, PurchaseRecordingError

    monkeypatch.setattr(
        module,
        "estimate_external_cost",
        lambda key, count: {
            "cost": 0.01 * count,
            "cost_source": "estimated",
            "rows": count,
        },
    )
    calls, observations = [], []

    class FailingRecorder(LocalPurchasePolicy):
        def record_purchase(self, request, admission, observation):
            observations.append(observation)
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("recording unavailable")

    def send(request):
        calls.append(request)
        return httpx.Response(
            200, json={"results": []}, headers={"x-request-id": "known"}
        )

    ctx = _context(send, engine="opencage")
    ctx.extras["service_operation_policy"] = FailingRecorder()
    geocoder = AdmittedGeocoder(ctx)
    try:
        with pytest.raises(
            asyncio.CancelledError if cancelled else PurchaseRecordingError
        ) as caught:
            await geocoder._get(
                ctx,
                module.OPENCAGE_URL,
                {},
                engine="opencage",
                pricing_key=module.GEOCODE_EXTERNAL_GEOCODER,
                credential_source="project_key",
            )
        assert len(calls) == len(observations) == 1
        assert observations[0].provider_cost_micro == 10_000
        accounting = caught.value.accounting
        assert accounting is geocoder.accounting_by_row[1]
        assert len(accounting["model_calls"]) == 1
        fact = accounting["model_calls"][0]
        assert fact["request_id"] == "known"
        assert fact["units"] == {"requests": 1, "rows": 1}
        assert fact["provider_cost_usd"] == 0.01
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
@pytest.mark.parametrize("prior_response", [False, True])
async def test_geocode_unsent_failure_preserves_only_previous_response(
    monkeypatch, error_type, prior_response
):
    from frisket.execution.purchases import LocalPurchasePolicy

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    calls, returned = [], []

    class Policy(LocalPurchasePolicy):
        def record_purchase(self, request, admission, observation):
            returned.append(geocoder.accounting_by_row[1])

    def send(request):
        calls.append(request)
        if prior_response and len(calls) == 1:
            return httpx.Response(429, headers={"x-request-id": "prior-response"})
        raise error_type("not sent", request=request)

    ctx = _context(send, engine="opencage")
    ctx.extras["service_operation_policy"] = Policy()
    geocoder = AdmittedGeocoder(ctx)
    try:
        with pytest.raises(GeocodeEngineError) as caught:
            await geocoder._get(
                ctx,
                module.OPENCAGE_URL,
                {},
                engine="opencage",
                pricing_key=module.GEOCODE_EXTERNAL_GEOCODER,
                credential_source="project_key",
            )
        assert caught.value.purchase_dispatched is False
        assert len(calls) == (2 if prior_response else 1)
        assert geocoder.request_count == int(prior_response)
        if prior_response:
            assert caught.value.accounting is returned[0]
            assert geocoder.accounting_by_row[1] is returned[0]
            assert len(returned[0]["model_calls"]) == 1
            assert returned[0]["model_calls"][0]["request_id"] == "prior-response"
        else:
            assert caught.value.accounting is None
            assert geocoder.accounting_by_row == {}
    finally:
        await ctx.http.aclose()

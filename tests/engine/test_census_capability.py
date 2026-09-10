from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from frisket.actions.census_types import CensusRecord
from frisket.actions.types import GeoPoint, RowError
from frisket.engine.executor.census_capability import (
    AdmittedCensusDemographics,
    CensusGeo,
)
from frisket.execution.credential_use import CredentialUseContext
from frisket.ops.base import OpContext, RecipeInvocationHalt

FIXTURES = Path(__file__).parent.parent / "fixtures" / "census"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _fixture_json(name: str):
    return json.loads(_fixture_text(name))


def _routed_extras(row_ids):
    from frisket.engine.store.execution_routes import RouteRow
    from frisket.execution.attempt import (
        ATTEMPT_EXTRA,
        AttemptCommitment,
        RoutedAdmission,
    )
    from frisket.execution.promise_compiler import OperatorBorneZeroCost
    from frisket.execution.provider import ConnectionConfig
    from frisket.execution.resolver import CandidateBinding, RouteRowFacts
    from frisket.execution.targets import ExecutionTarget

    route = RouteRow(
        id="route_CENSUSTEST",
        subject_kind="run",
        subject_id="1",
        seq=1,
        predecessor_id=None,
        promise_set_id="pset_CENSUS",
        engine="us_census_acs",
        options={},
        target_snapshot={
            "target_id": "us-census",
            "capability": "census_demographics",
            "transport": "census.acs5",
            "run_scoped": False,
        },
        route_fact_hash="hash",
        operator="us-census",
        egress_class="third_party_api",
        region=None,
        credential_source="local",
        cost_posture="operator_borne",
        created_at="2026-09-07T00:00:00+00:00",
    )
    binding = CandidateBinding(
        facts=RouteRowFacts(
            target_id="us-census",
            engine=route.engine,
            operator=route.operator,
            egress_class=route.egress_class,
            region=None,
            credential_source=route.credential_source,
            cost_posture=route.cost_posture,
        ),
        connection=ConnectionConfig(),
        target=ExecutionTarget(
            id="us-census", operator=route.operator, egress_class=route.egress_class
        ),
    )
    return {
        ATTEMPT_EXTRA: AttemptCommitment(
            attempt_id="attempt_CENSUS",
            run_id=1,
            seq=0,
            identity="identity",
            scope=row_ids,
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
        )
    }


def _capability(handler, row_ids=(1, 2, 3), *, cancelled=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = OpContext(
        http=client,
        extras={
            **_routed_extras(row_ids),
            **({"cancelled": cancelled} if cancelled else {}),
        },
        credential_use_context=CredentialUseContext.open(),
    )
    return AdmittedCensusDemographics(ctx).bind_rows(row_ids)


def _lookup(capability, points, *, geography="tract", include_moe=False):
    return asyncio.run(
        capability.lookup_many(points, geography=geography, include_moe=include_moe)
    )


def test_whole_scope_dedupes_six_decimals_and_groups_acs(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-census-key")
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            body = request.content.decode()
            assert "ACS2024_Current" in body
            assert "\n1,-77.03,38.9\n" in body
            assert "\n2," not in body
            assert "\n3,-77.04,38.91\n" in body
            return httpx.Response(
                200, text=_fixture_text("coordinatesbatch_dedupe.csv")
            )
        assert request.url.params["key"] == "test-census-key"
        assert request.url.params["for"] == "tract:*"
        assert request.url.params["in"] == "state:11 county:001"
        return httpx.Response(200, json=_fixture_json("acs_2024_dc_tracts.json"))

    capability = _capability(handler)
    result = _lookup(
        capability,
        {
            1: GeoPoint(lat=38.9, lon=-77.03),
            2: GeoPoint(lat=38.9000001, lon=-77.0300001),
            3: GeoPoint(lat=38.91, lon=-77.04),
        },
    )
    assert [r.demo_population for r in result.values()] == [1843, 1843, 2479]
    assert [r.demo_poverty_rate for r in result.values()] == [8.46, 8.46, 14.96]
    assert [r.demo_bachelors_plus_rate for r in result.values()] == [
        90.52,
        90.52,
        92.41,
    ]
    assert capability.request_count == 2
    assert capability.provider_details == {
        "dataset": "acs/acs5",
        "vintage": "2024",
        "geography": "tract",
    }
    assert len(calls) == 2
    assert set(capability.accounting_by_row) == {1, 2, 3}
    for accounting in capability.accounting_by_row.values():
        fact = accounting["model_calls"][0]
        assert fact["provider_cost_usd"] == 0
        assert fact["cost_source"] == "free_public_api"
        assert "test-census-key" not in str(accounting)


def test_missing_geography_and_block_are_independent_row_errors(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")

    def handler(request):
        if request.method == "POST":
            assert "Current_Current" in request.content.decode()
            return httpx.Response(
                200,
                text=(
                    _fixture_text("coordinatesbatch_current_current.csv")
                    + '"2","-77.04","38.91","Match","11","001","002001","","999"\n'
                ),
            )
        return httpx.Response(
            200, json=_fixture_json("acs_2024_dc_block_groups_980000.json")
        )

    capability = _capability(handler)
    result = _lookup(
        capability,
        {
            1: GeoPoint(lat=38.8977, lon=-77.0365),
            2: GeoPoint(lat=38.91, lon=-77.04),
            3: GeoPoint(lat=0, lon=0),
        },
        geography="block_group",
    )
    assert result[1].demo_population == 17
    assert result[2].code == "census_block_missing"
    assert result[3].code == "census_geography_missing"
    assert set(capability.accounting_by_row) == {1}


def test_no_acs_record_preserves_geography_with_null_metrics(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")

    def handler(request):
        return (
            httpx.Response(
                200, text=_fixture_text("coordinatesbatch_current_current.csv")
            )
            if request.method == "POST"
            else httpx.Response(200, json=[])
        )

    result = _lookup(_capability(handler), {1: GeoPoint(lat=38.8977, lon=-77.0365)})[1]
    assert result.demo_area_id == "11001980000"
    assert result.demo_dataset == "acs/acs5"
    assert result.demo_population is None
    assert result.demo_poverty_rate is None


def test_scope_and_second_call_refuse_before_more_egress(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="")

    capability = _capability(handler, row_ids=(1,))
    with pytest.raises(RecipeInvocationHalt, match="unadmitted"):
        _lookup(capability, {2: GeoPoint(lat=0, lon=0)})
    assert not calls
    result = _lookup(capability, {1: GeoPoint(lat=0, lon=0)})
    assert isinstance(result[1], RowError)
    with pytest.raises(RecipeInvocationHalt, match="one whole-scope"):
        _lookup(capability, {1: GeoPoint(lat=0, lon=0)})
    assert len(calls) == 1
    with pytest.raises(RecipeInvocationHalt, match="already bound"):
        capability.bind_rows((2,))


def test_missing_key_and_precancel_do_not_call_provider(monkeypatch):
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)

    def handler(_request):
        pytest.fail("no request is allowed")

    with pytest.raises(RecipeInvocationHalt, match="Set CENSUS_API_KEY"):
        _lookup(_capability(handler), {1: GeoPoint(lat=0, lon=0)})
    result = _lookup(
        _capability(handler, cancelled=lambda: True), {1: GeoPoint(lat=0, lon=0)}
    )
    assert result[1].code == "cancelled"


def test_cancellation_between_requests_skips_acs(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        return httpx.Response(
            200, text=_fixture_text("coordinatesbatch_current_current.csv")
        )

    capability = _capability(handler, cancelled=lambda: bool(calls))
    result = _lookup(capability, {1: GeoPoint(lat=38.8977, lon=-77.0365)})
    assert result[1].code == "cancelled"
    assert capability.request_count == 1


def test_caches_are_local_and_bounded_and_preserve_grouping(monkeypatch):
    from frisket.engine.executor import census_capability as module

    monkeypatch.setattr(module, "CENSUS_GEOLOOKUP_BATCH_SIZE", 2)
    monkeypatch.setattr(module, "MAX_GEO_CACHE_ENTRIES", 2)
    monkeypatch.setattr(module, "MAX_ACS_CACHE_ENTRIES", 1)
    calls = []

    class HTTP:
        async def post(self, _url, *, data, files):
            lines = files["coordinatesFile"][1].splitlines()
            calls.append(("POST", len(lines)))
            return httpx.Response(
                200,
                text="\n".join(
                    f"{line.split(',')[0]},0,0,Match,11,001,980000,1034"
                    for line in lines
                ),
            )

        async def get(self, _url, *, params):
            calls.append(("GET", params["in"]))
            return httpx.Response(200, json=_fixture_json("acs_2024_dc_tracts.json"))

    ctx = OpContext(http=HTTP())
    capability = AdmittedCensusDemographics(ctx)
    points = {i: (float(i), 0.0) for i in range(1, 6)}
    geos = asyncio.run(capability._lookup_geographies(points, "tract", ctx))
    assert len(geos) == 5
    assert calls == [("POST", 2), ("POST", 2), ("POST", 1)]
    assert len(capability._geo_cache) == 2
    asyncio.run(
        capability._lookup_geographies({4: points[4], 5: points[5]}, "tract", ctx)
    )
    assert len(calls) == 3
    asyncio.run(capability._fetch_acs(geos.values(), "tract", "2024", False, ctx))
    asyncio.run(capability._fetch_acs(geos.values(), "tract", "2024", False, ctx))
    assert calls[-1] == ("GET", "state:11 county:001")
    assert len(calls) == 4
    assert not AdmittedCensusDemographics(ctx)._geo_cache
    assert not AdmittedCensusDemographics(ctx)._acs_cache


def test_block_group_acs_groups_by_tract_and_cache_is_bounded(monkeypatch):
    from frisket.engine.executor import census_capability as module

    monkeypatch.setattr(module, "MAX_ACS_CACHE_ENTRIES", 1)
    parents = []

    class HTTP:
        async def get(self, _url, *, params):
            parents.append(params["in"])
            assert params["for"] == "block group:*"
            assert "B01003_001M" in params["get"]
            return httpx.Response(200, json=[])

    ctx = OpContext(http=HTTP())
    capability = AdmittedCensusDemographics(ctx)
    geos = [
        CensusGeo("11", "001", "980000", "1000"),
        CensusGeo("11", "001", "980000", "2000"),
        CensusGeo("11", "001", "000001", "1000"),
    ]
    asyncio.run(capability._fetch_acs(geos, "block_group", "2024", True, ctx))
    assert parents == [
        "state:11 county:001 tract:980000",
        "state:11 county:001 tract:000001",
    ]
    assert len(capability._acs_cache) == 1


def test_changed_credential_class_refuses_before_egress(monkeypatch):
    from frisket.execution.credential_use import CredentialOwner

    monkeypatch.setenv("CENSUS_API_KEY", "test-local-key")

    def handler(_request):
        pytest.fail("credential mismatch must refuse before HTTP")

    capability = _capability(handler)
    owner = CredentialOwner.organization("test-org")
    capability.ctx.credential_use_context = CredentialUseContext(
        cost_posture="org_key", consented_owner=owner, selected_owner=owner
    )
    with pytest.raises(RecipeInvocationHalt, match="credential"):
        _lookup(capability, {1: GeoPoint(lat=0, lon=0)})
    assert capability.request_count == 0


def test_project_secret_reaches_acs_and_records_only_its_provenance(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, text=_fixture_text("coordinatesbatch_current_current.csv")
            )
        assert request.url.params["key"] == "test-project-secret"
        return httpx.Response(200, json=[])

    capability = _capability(handler)
    capability.ctx.project = SimpleNamespace(
        secret_plaintext=lambda name: (
            "test-project-secret" if name == "CENSUS_API_KEY" else None
        )
    )
    result = _lookup(capability, {1: GeoPoint(lat=0, lon=0)})
    assert isinstance(result[1], CensusRecord)
    fact = capability.accounting_by_row[1]["model_calls"][0]
    assert fact["credential_source"] == "project_key"
    assert "test-project-secret" not in str(capability.accounting_by_row)
    assert "test-project-secret" not in repr(capability)
    assert len(calls) == 2


def test_geo_lookup_does_not_retry_rate_limit(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, text="rate limited")

    capability = _capability(handler)
    with pytest.raises(RuntimeError, match="429"):
        _lookup(capability, {1: GeoPoint(lat=0, lon=0)})
    assert len(calls) == 1
    assert capability.request_count == 1


def test_unrouted_lookup_refuses_before_http(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")
    capability = AdmittedCensusDemographics(OpContext(http=object())).bind_rows((1,))
    with pytest.raises(RecipeInvocationHalt, match="admitted provider route"):
        _lookup(capability, {1: GeoPoint(lat=0, lon=0)})
    assert capability.request_count == 0


@pytest.mark.parametrize("part", ["engine", "capability", "transport"])
def test_mismatched_route_refuses_before_http(part, monkeypatch):
    from dataclasses import replace
    from frisket.execution.attempt import ATTEMPT_EXTRA

    monkeypatch.setenv("CENSUS_API_KEY", "test-key")

    def handler(_request):
        pytest.fail("mismatched route must not dispatch")

    capability = _capability(handler)
    attempt = capability.ctx.extras[ATTEMPT_EXTRA]
    route = attempt.admission.route
    route = (
        replace(route, engine="nominatim")
        if part == "engine"
        else replace(route, target_snapshot={**route.target_snapshot, part: "other"})
    )
    capability.ctx.extras[ATTEMPT_EXTRA] = replace(
        attempt, admission=replace(attempt.admission, route=route)
    )
    with pytest.raises(RecipeInvocationHalt, match="admitted provider route"):
        _lookup(capability, {1: GeoPoint(lat=0, lon=0)})
    assert capability.request_count == 0


@pytest.mark.parametrize("error_kind", ["status", "json", "transport"])
def test_acs_errors_do_not_leak_credential(error_kind):
    from frisket.credentials import ResolvedCredential

    secret = "sensitive-test-key"

    class HTTP:
        async def get(self, _url, *, params):
            if error_kind == "transport":
                raise httpx.ConnectError(f"request URL contains key={secret}")
            return httpx.Response(
                503 if error_kind == "status" else 200, text=f"unexpected {secret}"
            )

    ctx = OpContext(http=HTTP())
    capability = AdmittedCensusDemographics(ctx)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            capability._fetch_acs_group(
                ("11", "001"),
                "tract",
                "2024",
                False,
                ctx,
                credential=ResolvedCredential(secret, "local"),
            )
        )
    assert secret not in str(caught.value)


def test_moe_and_unknown_denominators():
    capability = AdmittedCensusDemographics(OpContext())
    result = capability._format_row(
        CensusGeo("11", "001", "980000", "1034"),
        {
            "B01003_001M": "5",
            "B01002_001M": "-666666666",
            "B19013_001M": "8",
            "B17001_001E": "0",
            "B17001_002E": "12",
        },
        geography="tract",
        vintage="2024",
        include_moe=True,
    )
    assert isinstance(result, CensusRecord)
    assert len(CensusRecord.model_fields) == 22
    assert result.demo_population_moe == 5
    assert result.demo_median_age_moe is None
    assert result.demo_median_household_income_moe == 8
    assert result.demo_poverty_rate is None


def test_credential_redaction_precedes_error_truncation():
    from frisket.engine.executor.census_capability import _response_detail

    response = httpx.Response(503, text="x" * 195 + "sensitive-test-key")
    detail = _response_detail(response, "sensitive-test-key")
    assert "sensi" not in detail


def test_census_geolookup_parser_accepts_real_headerless_csv():
    parsed = AdmittedCensusDemographics._parse_geo_lookup_csv(
        _fixture_text("coordinatesbatch_current_current.csv")
        + '"2","-77.04","38.91","Match","11","001","002001","","999"\n'
    )

    assert parsed["1"] == CensusGeo("11", "001", "980000", "1034")
    assert parsed["2"] == CensusGeo("11", "001", "002001", None)


def test_census_suppressed_degree_counts_stay_unknown():
    recipe = AdmittedCensusDemographics(OpContext())
    record = {
        var: "100"
        for var in [
            "B01003_001E",
            "B01002_001E",
            "B19013_001E",
            "B17001_001E",
            "B17001_002E",
            "B15003_001E",
            "B03002_001E",
            "B03002_003E",
            "B03002_004E",
            "B03002_006E",
            "B03002_012E",
            "B25003_001E",
            "B25003_002E",
        ]
    }
    record.update(
        {
            "B15003_022E": "-666666666",
            "B15003_023E": "-666666666",
            "B15003_024E": "-666666666",
            "B15003_025E": "-666666666",
        }
    )

    data = recipe._format_row(
        CensusGeo("11", "001", "002001", "1000"),
        record,
        geography="tract",
        vintage="2024",
        include_moe=False,
    )

    assert data.demo_bachelors_plus_rate is None


def test_census_acs_non_json_key_error_is_readable():
    class Response:
        status_code = 200
        text = "<html><head><title>Invalid Key</title></head></html>"
        headers = {}

        def json(self):
            raise json.JSONDecodeError("bad", self.text, 0)

    class HTTP:
        async def get(self, _url, params):
            return Response()

    recipe = AdmittedCensusDemographics(OpContext())
    with pytest.raises(RuntimeError, match="Invalid Census API key"):
        asyncio.run(
            recipe._fetch_acs_group(
                ("11", "001"),
                "tract",
                "2024",
                False,
                OpContext(http=HTTP()),
            )
        )


def test_census_acs_non_json_missing_key_error_is_readable():
    class Response:
        status_code = 200
        text = "<html><a href='/data/key_signup.html?url=missing_key'>key</a></html>"
        headers = {}

        def json(self):
            raise json.JSONDecodeError("bad", self.text, 0)

    class HTTP:
        async def get(self, _url, params):
            return Response()

    recipe = AdmittedCensusDemographics(OpContext())
    with pytest.raises(RuntimeError, match="set CENSUS_API_KEY"):
        asyncio.run(
            recipe._fetch_acs_group(
                ("11", "001"),
                "tract",
                "2024",
                False,
                OpContext(http=HTTP()),
            )
        )


def test_census_acs_block_group_fixture_parses():
    class HTTP:
        async def get(self, _url, params):
            assert params["for"] == "block group:*"
            assert params["in"] == "state:11 county:001 tract:980000"
            return httpx.Response(
                200, json=_fixture_json("acs_2024_dc_block_groups_980000.json")
            )

    recipe = AdmittedCensusDemographics(OpContext())
    records = asyncio.run(
        recipe._fetch_acs_group(
            ("11", "001", "980000"),
            "block_group",
            "2024",
            False,
            OpContext(http=HTTP()),
        )
    )

    assert set(records) == {"110019800001"}
    formatted = recipe._format_row(
        CensusGeo("11", "001", "980000", "1034"),
        records["110019800001"],
        geography="block_group",
        vintage="2024",
        include_moe=False,
    )
    assert formatted.demo_area_id == "110019800001"
    assert formatted.demo_population == 17
    assert formatted.demo_bachelors_plus_rate == 100

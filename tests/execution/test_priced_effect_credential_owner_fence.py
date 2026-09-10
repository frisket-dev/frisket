"""Cross-seam regressions for W4's priced-effect credential owner fence.

Each capability is driven through its real recipe or admitted typed adapter. The provider
function one level below the adapter records whether egress would occur:

* a deployment environment credential is normalized to
  ``platform_key`` + deployment owner before the fence and executes;
* a project secret under platform consent refuses before egress;
* an org-BYOK credential whose live owner equals the quote owner executes;
* S!=F (the selected/storage-side owner differs from the funding owner pinned
  by the quote) refuses before egress.

The fake sits below credential selection, normalization, and the fence.  A
consumer reading the old source-only shape or dropping either owner makes
these tests red.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from frisket.credentials import ResolvedCredential
from frisket.actions.types import GeoPoint, Row
from frisket.actions.translate_types import TranslationOptions
from frisket.engine.executor.census_capability import (
    AdmittedCensusDemographics,
    CensusGeo,
)
from frisket.engine.executor.geocode_capability import AdmittedGeocoder
from frisket.engine.store.execution_routes import RouteRow
from frisket.execution.attempt import ATTEMPT_EXTRA, AttemptCommitment, RoutedAdmission
from frisket.execution.promise_compiler import OperatorBorneZeroCost
from frisket.execution.credential_use import (
    CredentialOwner,
    CredentialUseContext,
)
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.ops.ocr_engines_hosted import (
    _datalab_credential,
    _datalab_credential_parts,
)
from frisket.engine.executor.translate_read import AdmittedTranslator


DEPLOYMENT = CredentialOwner.deployment("frisket-hosted")
FUNDER = CredentialOwner.organization(31)
STORAGE = CredentialOwner.organization(44)


class _Project:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = dict(secrets or {})

    def secret_plaintext(self, name: str) -> str | None:
        return self._secrets.get(name)


@dataclass(frozen=True)
class _Resolver:
    credential: ResolvedCredential

    def resolve_action_credential(
        self, _project: Any, _name: str
    ) -> ResolvedCredential:
        return self.credential


def _case(
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    scenario: str,
) -> tuple[_Project, CredentialUseContext, bool, str]:
    monkeypatch.delenv(name, raising=False)
    if scenario == "deployment":
        monkeypatch.setenv(name, "deployment-secret")
        return (
            _Project(),
            CredentialUseContext(
                cost_posture="platform_metered",
                consented_owner=DEPLOYMENT,
                selected_owner=DEPLOYMENT,
                deployment_owner=DEPLOYMENT,
            ),
            True,
            "platform_key",
        )
    if scenario == "project_under_platform":
        return (
            _Project({name: "project-secret"}),
            CredentialUseContext(
                cost_posture="platform_metered",
                consented_owner=DEPLOYMENT,
                selected_owner=DEPLOYMENT,
                deployment_owner=DEPLOYMENT,
            ),
            False,
            "project_key",
        )
    selected = FUNDER if scenario == "byok_same_account" else STORAGE
    credential = ResolvedCredential(
        "org-secret",
        "org_byok",
        owner=selected,
    )
    return (
        _Project(),
        CredentialUseContext(
            cost_posture="org_key",
            consented_owner=FUNDER,
            # The trusted job/funding identity stays F. In the S!=F case the
            # buggy credential resolver reaches across to storage S; the
            # fence must compare rather than overwrite either fact.
            selected_owner=FUNDER,
            deployment_owner=DEPLOYMENT,
            credential_resolver=_Resolver(credential),
        ),
        scenario == "byok_same_account",
        "org_byok",
    )


_SCENARIOS = (
    "deployment",
    "project_under_platform",
    "byok_same_account",
    "byok_storage_differs_from_funder",
)


def _admitted_context(project, context, *, capability, engine, transport, source):
    """Pin the route; only the real live credential resolver may select the key."""
    route = RouteRow(
        id="route_FENCE",
        subject_kind="run",
        subject_id="1",
        seq=1,
        predecessor_id=None,
        promise_set_id="pset_FENCE",
        engine=engine,
        options={"language": []} if capability == "translate" else {},
        target_snapshot={
            "target_id": "us-census"
            if capability == "census_demographics"
            else "deepl"
            if capability == "translate"
            else "opencage",
            "capability": capability,
            "transport": transport,
            "run_scoped": False,
        },
        route_fact_hash="fence-hash",
        operator=engine,
        egress_class="third_party_api",
        region=None,
        credential_source=source,
        cost_posture=context.cost_posture,
        created_at="2026-09-07T00:00:00+00:00",
    )
    admission = RoutedAdmission(
        head_route_id=route.id,
        head_promise_set_id=route.promise_set_id,
        route=route,
        promise_set=None,
        binding=None,
        evaluation=None,
        admitted_by_consent_id=None,
    )
    return OpContext(
        project=project,
        http=object(),
        credential_use_context=context,
        extras={
            "row_id": 1,
            ATTEMPT_EXTRA: AttemptCommitment(
                attempt_id="attempt_FENCE",
                run_id=1,
                seq=0,
                identity="fence",
                scope=(1,),
                admission=admission,
                cost_basis=OperatorBorneZeroCost(),
                price_card_version=None,
            ),
        },
    )


def test_existing_datalab_ocr_fence_reads_the_normalized_owner_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-existing OCR fence must not bypass W4's common normalizer."""
    monkeypatch.setenv("DATALAB_API_KEY", "deployment-secret")
    context = CredentialUseContext(
        cost_posture="platform_metered",
        consented_owner=DEPLOYMENT,
        selected_owner=DEPLOYMENT,
        deployment_owner=DEPLOYMENT,
    )

    value, source, owner = _datalab_credential_parts(
        _datalab_credential(
            OpContext(project=_Project(), credential_use_context=context)
        )
    )

    assert value == "deployment-secret"
    assert source == "platform_key"
    assert owner == DEPLOYMENT


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _SCENARIOS)
async def test_translate_fences_class_and_owner_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    project, context, allowed, expected_source = _case(
        monkeypatch,
        name="DEEPL_API_KEY",
        scenario=scenario,
    )
    effects: list[str] = []

    async def fake_deepl(
        _http,
        _key,
        _texts,
        _target,
        _source,
        *,
        credential_source,
    ):
        effects.append(credential_source)
        return [
            {
                "text": "Hola",
                "detected_source_language": "EN",
                "billed_characters": 5,
            }
        ]

    monkeypatch.setattr(
        "frisket.ops.integrations.deepl.deepl_translate",
        fake_deepl,
    )
    ctx = _admitted_context(
        project,
        context,
        capability="translate",
        engine="deepl",
        transport="deepl.v2",
        source=expected_source,
    )
    options = TranslationOptions(target_language="Spanish")
    owner = AdmittedTranslator(ctx, engine="deepl", options=options.model_dump())
    row = Row({"statement": "Hello"})
    invoke = owner.bind_row(row, sheet_id=1, row_id=1, sources={}, ctx=ctx).translate(
        row, "Hello", options=options
    )
    try:
        if allowed:
            data = await invoke
            assert data.translation == "Hola"
            assert effects == [expected_source]
            assert (
                owner.accounting_by_row[1]["model_calls"][0]["credential_source"]
                == expected_source
            )
        else:
            with pytest.raises(RecipeInvocationHalt, match="refusing before the call"):
                await invoke
            assert effects == []
    finally:
        await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _SCENARIOS)
async def test_geocode_fences_class_and_owner_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    project, context, allowed, expected_source = _case(
        monkeypatch,
        name="OPENCAGE_API_KEY",
        scenario=scenario,
    )
    effects: list[str] = []

    async def fake_opencage(
        _self,
        _address,
        _ctx,
        _api_key,
        *,
        credential_source,
        pricing_key,
    ):
        effects.append(credential_source)
        return (
            {"lat": 38.9, "lon": -77.0, "formatted": "Washington, DC"},
            {"cost": 0.01, "pricing_key": pricing_key, "model_calls": []},
        )

    monkeypatch.setattr(AdmittedGeocoder, "_opencage", fake_opencage)
    ctx = _admitted_context(
        project,
        context,
        capability="geocode",
        engine="opencage",
        transport="opencage.v1",
        source=expected_source,
    )
    geocoder = AdmittedGeocoder(ctx)
    invoke = geocoder.bind_row(ctx).lookup("1600 Pennsylvania Ave")
    if allowed:
        result = await invoke
        assert result.geo_point.value == GeoPoint(lat=38.9, lon=-77.0)
        assert effects == [expected_source]
        assert geocoder.accounting_by_row[1]["cost"] == 0.01
    else:
        with pytest.raises(RecipeInvocationHalt, match="refusing before the call"):
            await invoke
        assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _SCENARIOS)
async def test_census_fences_class_and_owner_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    project, context, allowed, expected_source = _case(
        monkeypatch,
        name="CENSUS_API_KEY",
        scenario=scenario,
    )
    effects: list[str] = []

    async def fake_lookup(_self, points, _spec, _ctx):
        effects.append("geolookup")
        return {row_id: CensusGeo("11", "001", "980000") for row_id in points}

    async def fake_fetch(
        _self,
        _geos,
        _geography,
        _vintage,
        _include_moe,
        _ctx,
        *,
        credential,
    ):
        assert credential is not None
        effects.append(credential.source)
        return {}

    monkeypatch.setattr(AdmittedCensusDemographics, "_lookup_geographies", fake_lookup)
    monkeypatch.setattr(AdmittedCensusDemographics, "_fetch_acs", fake_fetch)
    ctx = _admitted_context(
        project,
        context,
        capability="census_demographics",
        engine="us_census_acs",
        transport="census.acs5",
        source=expected_source,
    )
    census = AdmittedCensusDemographics(ctx).bind_rows((1,))
    invoke = census.lookup_many(
        {1: GeoPoint(lat=38.9, lon=-77.0)},
        geography="tract",
        include_moe=False,
    )
    if allowed:
        out = await invoke
        assert effects == ["geolookup", expected_source]
        assert out[1].us_census_tract_geoid == "11001980000"
        meta = census.accounting_by_row[1]
        assert meta["model_calls"][0]["credential_source"] == expected_source
    else:
        with pytest.raises(RecipeInvocationHalt, match="refusing before the call"):
            await invoke
        assert effects == []

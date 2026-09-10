"""Behavioral closure for every priced capability's payer fence."""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from frisket.credentials import ResolvedCredential
from frisket.execution.attempt import (
    ATTEMPT_EXTRA,
    AttemptCommitment,
    RoutedAdmission,
)
from frisket.execution.credential_use import (
    CredentialOwner,
    CredentialUseContext,
    require_consented_credential,
)
from frisket.execution.promise_compiler import OperatorBorneZeroCost
from frisket.execution.provider import ConnectionConfig
from frisket.execution.targets import (
    CAPABILITY_CENSUS,
    CAPABILITY_GEOCODE,
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE,
    CAPABILITY_TRANSLATE,
    EXECUTION_CAPABILITIES,
)
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.actions.types import GeoPoint, Row
from frisket.actions.translate_types import TranslationOptions
from frisket.engine.executor.census_capability import AdmittedCensusDemographics
from frisket.engine.executor.geocode_capability import AdmittedGeocoder
from frisket.ops.ocr_engines import OcrEngines
from tests.document_conversion_helpers import bound_document_converter
from frisket.engine.executor.translate_read import AdmittedTranslator


FUNDER = CredentialOwner.organization(31)
STORAGE = CredentialOwner.organization(44)
DEPLOYMENT = CredentialOwner.deployment("frisket-hosted")


@dataclass(frozen=True)
class _Resolver:
    def resolve_action_credential(
        self, _project: Any, _name: str
    ) -> ResolvedCredential:
        return ResolvedCredential("org-secret", "org_byok", owner=STORAGE)


def _owner_mismatch_context() -> CredentialUseContext:
    return CredentialUseContext(
        cost_posture="org_key",
        consented_owner=FUNDER,
        selected_owner=STORAGE,
        deployment_owner=DEPLOYMENT,
        credential_resolver=_Resolver(),
    )


def _routed_extras(
    *, engine="openai/whisper-1", capability="transcribe", transport="openai.audio"
) -> dict[str, Any]:
    route = SimpleNamespace(
        engine=engine,
        cost_posture="org_key",
        credential_source="org_byok",
        options={"language": []} if capability == CAPABILITY_TRANSLATE else {},
        target_snapshot={"capability": capability, "transport": transport},
    )
    admission = RoutedAdmission(
        head_route_id="route_TEST",
        head_promise_set_id="pset_TEST",
        route=route,
        promise_set=None,
        binding=SimpleNamespace(
            connection=ConnectionConfig(
                timeout_seconds=30,
                extra={
                    "app_name": "test-app",
                    "function_name": "test-function",
                    "gpu": "T4",
                },
            )
        ),
        evaluation=None,
        admitted_by_consent_id=None,
    )
    return {
        ATTEMPT_EXTRA: AttemptCommitment(
            attempt_id="attempt_TEST",
            run_id=1,
            seq=0,
            identity="test-identity",
            scope=(1,),
            admission=admission,
            cost_basis=OperatorBorneZeroCost(),
            price_card_version=None,
        )
    }


def _effect_reached(effects: list[str], effect: str) -> NoReturn:
    effects.append(effect)
    raise AssertionError(f"{effect} crossed the credential fence")


async def _transcribe_probe(
    monkeypatch: pytest.MonkeyPatch,
    _tmp_path: Path,
    context: CredentialUseContext,
    effects: list[str],
) -> None:
    class _Router:
        def adapter_for(self, _provider):
            return SimpleNamespace(base_url="https://example.test", api_key="secret")

        def credential_source_for(self, _provider):
            return "org_byok"

    extras = _routed_extras() | {"router": _Router()}
    await transcribe_engines.OpenAITranscriptionAdapter().transcribe(
        "openai/whisper-1",
        "unread-before-fence.wav",
        {},
        OpContext(
            http=SimpleNamespace(
                post=lambda *_args, **_kwargs: _effect_reached(effects, "remote")
            ),
            credential_use_context=context,
            extras=extras,
        ),
        None,
    )


async def _ocr_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    context: CredentialUseContext,
    effects: list[str],
) -> None:
    async def fake_datalab_ocr(*_args, **_kwargs):
        _effect_reached(effects, "datalab-ocr")

    monkeypatch.setattr(
        "frisket.ops.integrations.datalab.datalab_ocr",
        fake_datalab_ocr,
    )
    monkeypatch.setattr(
        "frisket.ops.media_metadata.image_dimensions",
        lambda _data: (1, 1),
    )
    source = tmp_path / "unread-before-fence.png"
    source.write_bytes(b"not-read-until-after-the-fence")
    await OcrEngines()._ocr_datalab(
        [source],
        OpContext(
            credential_use_context=context,
            extras=_routed_extras(),
        ),
        {"cost": 0.0, "calls": 0},
    )


async def _to_markdown_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    context: CredentialUseContext,
    effects: list[str],
) -> None:
    async def fake_datalab_convert(*_args, **_kwargs):
        _effect_reached(effects, "datalab-convert")

    monkeypatch.setattr(
        "frisket.ops.integrations.datalab.datalab_convert",
        fake_datalab_convert,
    )
    source = tmp_path / "credential-fence.pdf"
    source.write_bytes(b"%PDF-1.4")
    converter = bound_document_converter(
        OpContext(
            credential_use_context=context,
            extras=_routed_extras(),
        ),
        engine="datalab",
    )
    await converter._convert_datalab(source)


async def _translate_probe(
    monkeypatch: pytest.MonkeyPatch,
    _tmp_path: Path,
    context: CredentialUseContext,
    effects: list[str],
) -> None:
    async def fake_deepl(*_args, **_kwargs):
        _effect_reached(effects, "deepl")

    monkeypatch.setattr(
        "frisket.ops.integrations.deepl.deepl_translate",
        fake_deepl,
    )
    ctx = OpContext(
        project=object(),
        http=object(),
        credential_use_context=context,
        extras=_routed_extras(
            engine="deepl", capability=CAPABILITY_TRANSLATE, transport="deepl.v2"
        ),
    )
    options = TranslationOptions(target_language="Spanish")
    owner = AdmittedTranslator(ctx, engine="deepl", options=options.model_dump())
    row = Row({"statement": "Hello"})
    try:
        await owner.bind_row(row, sheet_id=1, row_id=1, sources={}, ctx=ctx).translate(
            row, "Hello", options=options
        )
    finally:
        await owner.aclose()


async def _geocode_probe(
    monkeypatch: pytest.MonkeyPatch,
    _tmp_path: Path,
    context: CredentialUseContext,
    effects: list[str],
) -> None:
    async def fake_opencage(*_args, **_kwargs):
        _effect_reached(effects, "opencage")

    monkeypatch.setattr(AdmittedGeocoder, "_opencage", fake_opencage)
    ctx = OpContext(
        project=object(),
        http=object(),
        credential_use_context=context,
        extras={
            **_routed_extras(
                engine="opencage",
                capability=CAPABILITY_GEOCODE,
                transport="opencage.v1",
            ),
            "row_id": 1,
        },
    )
    await AdmittedGeocoder(ctx).bind_row(ctx).lookup("1600 Pennsylvania Ave")


async def _census_probe(
    monkeypatch: pytest.MonkeyPatch,
    _tmp_path: Path,
    context: CredentialUseContext,
    effects: list[str],
) -> None:
    async def fake_lookup(_self, points, _spec, _ctx):
        del points
        _effect_reached(effects, "census-geolookup")

    async def fake_fetch(*_args, **_kwargs):
        effects.append("census-acs")
        return {}

    monkeypatch.setattr(AdmittedCensusDemographics, "_lookup_geographies", fake_lookup)
    monkeypatch.setattr(AdmittedCensusDemographics, "_fetch_acs", fake_fetch)
    census = AdmittedCensusDemographics(
        OpContext(
            project=object(),
            http=object(),
            credential_use_context=context,
            extras=_routed_extras(
                engine="us_census_acs",
                capability=CAPABILITY_CENSUS,
                transport="census.acs5",
            ),
        ),
    ).bind_rows((1,))
    await census.lookup_many(
        {1: GeoPoint(lat=38.9, lon=-77.0)}, geography="tract", include_moe=False
    )


_Probe = Callable[
    [pytest.MonkeyPatch, Path, CredentialUseContext, list[str]],
    Awaitable[None],
]
_FENCE_PROBES: dict[str, _Probe] = {
    CAPABILITY_TRANSCRIBE: _transcribe_probe,
    CAPABILITY_OCR: _ocr_probe,
    CAPABILITY_TRANSLATE: _translate_probe,
    CAPABILITY_TO_MARKDOWN: _to_markdown_probe,
    CAPABILITY_GEOCODE: _geocode_probe,
    CAPABILITY_CENSUS: _census_probe,
}


def test_payer_fence_requires_the_trusted_credential_context() -> None:
    with pytest.raises(TypeError, match="required keyword-only argument: 'context'"):
        require_consented_credential(
            cost_posture="org_key",
            selected_source="org_byok",
            effect="the provider call",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", sorted(_FENCE_PROBES))
async def test_every_priced_capability_refuses_owner_mismatch_before_effect(
    capability: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert set(_FENCE_PROBES) == set(EXECUTION_CAPABILITIES)
    effects: list[str] = []

    with pytest.raises(
        RecipeInvocationHalt,
        match=r"organization '44'.*organization '31'.*refusing before the call",
    ):
        await _FENCE_PROBES[capability](
            monkeypatch,
            tmp_path,
            _owner_mismatch_context(),
            effects,
        )

    assert effects == []

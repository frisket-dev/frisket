"""Per-model route choice constrains the shared runtime resolver."""

from __future__ import annotations

from dataclasses import replace

from frisket.cloud_execution import CloudPayerSubject
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.provider import (
    CompositionFacts,
    ConnectionConfig,
    ExactExecutionMatch,
    ExecutionComposition,
)
from frisket.execution.resolver import (
    Refusal,
    Resolution,
    ResolutionRequest,
    canonical_model_route_id,
    preferred_route_candidate,
    preferred_static_choice,
    resolve,
)
from frisket.execution.targets import (
    CAPABILITY_LLM,
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE,
    CAPABILITY_TRANSLATE,
    ExecutionTarget,
    LLMOptionSupport,
    OcrOptionSupport,
    TargetEngineSupport,
)

MODEL = "openai/gpt-5-mini"


def test_exact_restriction_cannot_select_another_target_for_same_engine():
    composition = ExecutionComposition(
        facts=CompositionFacts(),
        provider=StaticExecutionTargetProvider(),
        credential_use_context=CredentialUseContext.open(),
    ).restricted_to(
        ExactExecutionMatch(
            target_id="local-onnx",
            capability=CAPABILITY_TRANSCRIBE,
            engine="parakeet-tdt",
        )
    )

    assert [target.id for target in composition.resolution_targets()] == ["local-onnx"]
    outcome = resolve(
        composition.resolution_request(
            "parakeet-tdt", capability=CAPABILITY_TRANSCRIBE, options={}
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )
    assert getattr(outcome, "target_id", None) == "local-onnx" or (
        isinstance(outcome, Resolution) and outcome.target.id == "local-onnx"
    )


def test_exact_restriction_materializes_matching_wildcard_support():
    provider = _RoutesProvider()
    provider.own = replace(
        provider.own,
        engines=(replace(provider.own.engines[0], engine="openai/*"),),
    )
    provider._targets = (provider.own, provider.cloud)
    composition = _composition(provider).restricted_to(
        ExactExecutionMatch(
            target_id="own",
            capability=CAPABILITY_LLM,
            engine=MODEL,
        )
    )

    targets = composition.resolution_targets()
    assert [(target.id, target.engines[0].engine) for target in targets] == [
        ("own", MODEL)
    ]
    outcome = resolve(
        composition.resolution_request(MODEL, capability=CAPABILITY_LLM, options={}),
        composition.provider_for_resolution(),
        composition.facts,
    )
    assert isinstance(outcome, Resolution)
    assert outcome.target.id == "own"


def test_exact_restriction_with_missing_target_refuses_without_fallback():
    provider = _RoutesProvider()
    composition = _composition(provider).restricted_to(
        ExactExecutionMatch(
            target_id="missing",
            capability=CAPABILITY_LLM,
            engine=MODEL,
        )
    )

    assert composition.resolution_targets() == ()
    outcome = resolve(
        composition.resolution_request(MODEL, capability=CAPABILITY_LLM, options={}),
        composition.provider_for_resolution(),
        composition.facts,
    )
    assert isinstance(outcome, Refusal)
    assert outcome.family == "no_capable_target"
    assert provider.connection_calls == []


def _llm_target(target_id: str, *, cloud: bool) -> ExecutionTarget:
    return ExecutionTarget(
        id=target_id,
        operator="frisket" if cloud else "openai",
        egress_class="frisket_shared" if cloud else "third_party_api",
        engines=(
            TargetEngineSupport(
                engine=MODEL,
                capability=CAPABILITY_LLM,
                transport="frisket.cloud.llm.v1" if cloud else "remote",
                options=LLMOptionSupport(tools=True),
            ),
        ),
    )


class _RoutesProvider:
    def __init__(
        self,
        *,
        include_cloud: bool = True,
        configured: frozenset[str] = frozenset({"own", "cloud"}),
        live: frozenset[str] = frozenset({"own", "cloud"}),
    ) -> None:
        self.own = _llm_target("own", cloud=False)
        self.cloud = _llm_target("cloud", cloud=True)
        self._targets = (self.own, self.cloud) if include_cloud else (self.own,)
        self._configured = configured
        self._live = live
        self.connection_calls: list[str] = []

    def targets(self):
        return self._targets

    def configuration(self, target_id: str, capability: str, engine: str):
        del capability, engine
        return "configured" if target_id in self._configured else "not_configured"

    def selection_rank(self, target_id: str, capability: str, engine: str) -> int:
        del capability, engine
        return 0 if target_id == "own" else 10

    def connection(self, target_id: str):
        self.connection_calls.append(target_id)
        return ConnectionConfig() if target_id in self._live else None

    def cloud_payer(self, target_id: str, capability: str, engine: str):
        del capability
        if target_id != "cloud":
            return None
        return CloudPayerSubject(
            user_id=7,
            account_id=41,
            lane="frisket_credits",
            provider="openai",
            engine=engine,
            route_revision="route-1",
        )


def _composition(provider: _RoutesProvider, **kwargs) -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(),
        provider=provider,
        credential_use_context=CredentialUseContext.open(),
        **kwargs,
    )


def test_explicit_route_filters_before_configured_first_and_probes_only_that_route():
    provider = _RoutesProvider(
        configured=frozenset({"cloud"}),
        live=frozenset({"cloud"}),
    )

    choice = preferred_static_choice(
        MODEL,
        {},
        provider,
        capability=CAPABILITY_LLM,
        route="own",
    )
    assert not isinstance(choice, Refusal)
    assert choice is not None and choice[0].id == "own"

    outcome = resolve(
        ResolutionRequest(
            engine=MODEL,
            capability=CAPABILITY_LLM,
            route="own",
        ),
        provider,
        CompositionFacts(),
    )

    assert isinstance(outcome, Refusal)
    assert outcome.family == "no_live_target"
    assert outcome.target_id == "own"
    assert provider.connection_calls == ["own"]


def test_explicit_missing_cloud_route_refuses_without_own_fallback_or_probe():
    provider = _RoutesProvider(include_cloud=False)

    outcome = resolve(
        ResolutionRequest(
            engine=MODEL,
            capability=CAPABILITY_LLM,
            route="cloud",
        ),
        provider,
        CompositionFacts(),
    )

    assert isinstance(outcome, Refusal)
    assert outcome.family == "no_live_target"
    assert outcome.target_id is None
    assert "Reconnect Frisket Cloud" in outcome.remedy
    assert provider.connection_calls == []


def test_known_cloud_inventory_explains_missing_selected_route_without_probe():
    provider = _RoutesProvider(include_cloud=False)
    composition = _composition(
        provider,
        model_routes={MODEL: "cloud"},
        cloud_inventory_known=True,
    )
    request = composition.resolution_request(
        MODEL,
        capability=CAPABILITY_LLM,
        options={},
    )

    outcome = resolve(
        request,
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert isinstance(outcome, Refusal)
    assert outcome.family == "no_live_target"
    assert outcome.target_id is None
    assert outcome.remedy == (
        "This model is not currently available through Frisket Cloud."
    )
    assert provider.connection_calls == []


def test_composition_route_map_and_projection_override_share_one_request_path():
    provider = _RoutesProvider()
    composition = _composition(
        provider,
        model_routes={MODEL: "cloud"},
        model_route_default=None,
    )

    saved = composition.resolution_request(
        MODEL,
        capability=CAPABILITY_LLM,
        options={"tools": True},
    )
    projected = composition.resolution_request(
        MODEL,
        capability=CAPABILITY_LLM,
        options={"tools": True},
        route_override="own",
    )

    assert saved.route == "cloud"
    assert projected.route == "own"
    outcome = resolve(saved, composition.provider_for_resolution(), composition.facts)
    assert isinstance(outcome, Resolution)
    assert outcome.target.id == "cloud"
    assert outcome.facts.cloud_payer is not None


def test_route_default_skips_local_only_models():
    class LocalProvider:
        target = ExecutionTarget(
            id="local",
            operator="self",
            egress_class="none",
            engines=(
                TargetEngineSupport(
                    engine="rapidocr",
                    capability=CAPABILITY_OCR,
                    transport="local",
                    options=OcrOptionSupport(language=True, geometry=True),
                ),
            ),
        )

        def targets(self):
            return (self.target,)

        def configuration(self, target_id, capability, engine):
            del target_id, capability, engine
            return "configured"

        def selection_rank(self, target_id, capability, engine):
            del target_id, capability, engine
            return 0

        def connection(self, target_id):
            del target_id
            return ConnectionConfig()

    composition = ExecutionComposition(
        facts=CompositionFacts(),
        provider=LocalProvider(),
        credential_use_context=CredentialUseContext.open(),
        model_routes={},
        model_route_default="cloud",
    )

    request = composition.resolution_request(
        "rapidocr", capability=CAPABILITY_OCR, options={}
    )

    assert request.route == "own"
    assert isinstance(
        resolve(request, composition.provider_for_resolution(), composition.facts),
        Resolution,
    )


def test_unset_eligible_model_uses_cloud_setup_when_neither_route_is_configured():
    provider = _RoutesProvider(
        include_cloud=False,
        configured=frozenset(),
        live=frozenset(),
    )
    composition = _composition(provider, model_routes={})

    request = composition.resolution_request(
        MODEL, capability=CAPABILITY_LLM, options={}
    )
    outcome = resolve(request, composition.provider_for_resolution(), composition.facts)

    assert request.route == "cloud"
    assert isinstance(outcome, Refusal)
    assert outcome.target_id is None
    assert "Reconnect Frisket Cloud" in outcome.remedy


def test_unconfigured_managed_local_candidate_does_not_hide_ready_cloud_route():
    provider = _RoutesProvider(
        configured=frozenset({"cloud"}),
        live=frozenset({"cloud"}),
    )
    provider.own = replace(provider.own, egress_class="none")
    provider._targets = (provider.own, provider.cloud)
    composition = _composition(provider, model_routes={})

    request = composition.resolution_request(
        MODEL, capability=CAPABILITY_LLM, options={}
    )
    outcome = resolve(request, composition.provider_for_resolution(), composition.facts)

    assert request.route == "cloud"
    assert isinstance(outcome, Resolution)
    assert outcome.target.id == "cloud"


def test_cloud_default_applies_to_openrouter_catalog_model_before_connecting():
    model = "openrouter/minimax/minimax-m3"

    class OwnProvider:
        target = ExecutionTarget(
            id="remote-api:openrouter",
            operator="openrouter",
            egress_class="third_party_api",
            engines=(
                TargetEngineSupport(
                    engine="openrouter/*",
                    capability=CAPABILITY_LLM,
                    transport="remote",
                    options=LLMOptionSupport(tools=True),
                ),
            ),
        )

        def targets(self):
            return (self.target,)

        def configuration(self, target_id, capability, engine):
            del target_id, capability, engine
            return "not_configured"

        def selection_rank(self, target_id, capability, engine):
            del target_id, capability, engine
            return 0

        def connection(self, target_id):
            del target_id
            return None

    composition = ExecutionComposition(
        facts=CompositionFacts(),
        provider=OwnProvider(),
        credential_use_context=CredentialUseContext.open(),
        model_routes={},
        model_route_default="cloud",
    )

    request = composition.resolution_request(
        model, capability=CAPABILITY_LLM, options={}
    )

    assert request.route == "cloud"


def test_canonical_model_route_id_is_shared_across_named_ocr_capabilities():
    assert canonical_model_route_id("datalab", CAPABILITY_OCR) == "datalab"
    assert canonical_model_route_id("datalab", CAPABILITY_TO_MARKDOWN) == "datalab"


def test_option_free_route_candidate_can_pin_an_option_required_engine():
    provider = StaticExecutionTargetProvider()

    candidate = preferred_route_candidate(
        "opus_mt", provider, capability=CAPABILITY_TRANSLATE
    )
    option_checked = preferred_static_choice(
        "opus_mt", {}, provider, capability=CAPABILITY_TRANSLATE
    )

    assert not isinstance(candidate, Refusal)
    assert candidate is not None and candidate[1].engine == "opus_mt"
    assert isinstance(option_checked, Refusal)
    assert option_checked.family == "no_capable_target"


def test_exact_transcription_restriction_keeps_model_specific_support(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    composition = ExecutionComposition(
        facts=CompositionFacts(),
        provider=StaticExecutionTargetProvider(),
        credential_use_context=CredentialUseContext.open(),
    ).restricted_to(
        ExactExecutionMatch(
            target_id="remote-api:openrouter",
            capability=CAPABILITY_TRANSCRIBE,
            engine="openrouter/microsoft/mai-transcribe-2",
        )
    )
    target = composition.resolution_targets()[0]
    assert len(target.engines) == 1
    assert target.engines[0].options.clean is True
    result = resolve(
        composition.resolution_request(
            "openrouter/microsoft/mai-transcribe-2",
            capability=CAPABILITY_TRANSCRIBE,
            options={"clean": True},
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )
    assert isinstance(result, Resolution)
    assert result.support.options.clean is True


def test_metered_projection_preserves_exact_transcription_support():
    from frisket.execution.credential_use import CredentialOwner
    from frisket.execution.price_book import PlatformMetered

    match = ExactExecutionMatch(
        target_id="remote-api:openrouter",
        capability=CAPABILITY_TRANSCRIBE,
        engine="openrouter/microsoft/mai-transcribe-2",
    )
    owner = CredentialOwner.deployment("test")
    composition = ExecutionComposition(
        facts=CompositionFacts(edition="hosted", funding=PlatformMetered()),
        provider=StaticExecutionTargetProvider(),
        credential_use_context=CredentialUseContext(
            cost_posture="platform_metered",
            consented_owner=owner,
            selected_owner=owner,
            deployment_owner=owner,
        ),
        included=(match,),
    )
    target = next(
        t for t in composition.resolution_targets() if t.id == match.target_id
    )
    supports = [s for s in target.engines if s.capability == CAPABILITY_TRANSCRIBE]
    assert len(supports) == 1
    assert supports[0].engine == match.engine and supports[0].options.clean is True

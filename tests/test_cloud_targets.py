from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from frisket.ai.llm import ModelRouter
from frisket.contracts.action import Receipt
from frisket.cloud_execution import CloudExecutionAttempt
from frisket.cloud_connection import CloudConnectionStore
from frisket.cloud_targets import (
    cloud_execution_composition,
    compose_cloud_targets,
    legacy_cloud_execution_preference,
)
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudDocumentConvertOptionSupport,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudLLMOptionSupport,
    CloudLLMResult,
    CloudLLMUsage,
    CloudOCROptionSupport,
    CloudSessionResponse,
    CloudTokenPrice,
    CloudUnitPrice,
    CloudUser,
)
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.promise_compiler import PricedCostBasis
from frisket.execution.price_book import OperatorBorne
from frisket.execution.provider import (
    CompositionFacts,
    ConnectionConfig,
    ExecutionComposition,
    TargetConfigurationError,
)
from frisket.execution.resolver import Refusal, ResolutionRequest, resolve
from frisket.execution.resolve_for_action import BoundedScratchInput, resolve_for_action
from frisket.execution.targets import (
    CAPABILITY_LLM,
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSLATE,
    ExecutionTarget,
    LLMOptionSupport,
    LOCAL_MODELS_TARGET_ID,
    OcrOptionSupport,
    TargetEngineSupport,
    ToMarkdownOptionSupport,
    TranslateOptionSupport,
)
from frisket.engine.store import Project
from frisket.engine.runner import MapRunner
from frisket.engine.runner.validation import ClaimsGate
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.project_settings import ProjectSettings, write_project_settings

CLOUD_OCR_ENGINE = "paddleocr-vl"


class _LocalProvider:
    def __init__(self) -> None:
        self.target = ExecutionTarget(
            id="local",
            operator="self",
            egress_class="none",
            engines=(
                TargetEngineSupport(
                    engine="rapidocr",
                    transport="local",
                    capability=CAPABILITY_OCR,
                    options=OcrOptionSupport(language=True, geometry=True),
                ),
            ),
        )

    def targets(self):
        return (self.target,)

    def connection(self, target_id):
        return ConnectionConfig() if target_id == "local" else None

    def configuration(self, target_id, capability, engine):
        del capability, engine
        return "configured" if target_id == "local" else "not_configured"

    def selection_rank(self, target_id, capability, engine):
        del target_id, capability, engine
        return 0


class _EmptyProvider:
    def targets(self):
        return ()

    def connection(self, target_id):
        del target_id
        return None

    def configuration(self, target_id, capability, engine):
        del target_id, capability, engine
        return "not_configured"

    def selection_rank(self, target_id, capability, engine):
        del target_id, capability, engine
        return 0


class _OpenAIProvider:
    def __init__(self, *, configured: bool = True) -> None:
        self.configured = configured
        self.target = ExecutionTarget(
            id="remote-api:openai",
            operator="openai",
            egress_class="third_party_api",
            engines=(
                TargetEngineSupport(
                    engine="openai/*",
                    transport="remote",
                    capability=CAPABILITY_LLM,
                    options=LLMOptionSupport(
                        structured_output=True,
                        tools=True,
                        vision=True,
                    ),
                ),
                TargetEngineSupport(
                    engine="datalab",
                    transport="remote",
                    capability=CAPABILITY_OCR,
                    options=OcrOptionSupport(language=False, geometry=True),
                ),
            ),
        )

    def targets(self):
        return (self.target,)

    def connection(self, target_id):
        return (
            ConnectionConfig(token="configured-media-key")
            if target_id == "remote-api:openai" and self.configured
            else None
        )

    def configuration(self, target_id, capability, engine):
        del capability, engine
        return (
            "configured"
            if target_id == "remote-api:openai" and self.configured
            else "not_configured"
        )

    def selection_rank(self, target_id, capability, engine):
        del target_id, capability, engine
        return 0


class _PaddleOCRLocalProvider:
    def __init__(self, *, configured: bool) -> None:
        self.configured = configured
        self.target = ExecutionTarget(
            id=LOCAL_MODELS_TARGET_ID,
            operator="self",
            egress_class="none",
            engines=(
                TargetEngineSupport(
                    engine=CLOUD_OCR_ENGINE,
                    transport="sidecar.ocr",
                    capability=CAPABILITY_OCR,
                    options=OcrOptionSupport(language=True, geometry=True),
                ),
            ),
        )

    def targets(self):
        return (self.target,)

    def connection(self, target_id):
        return (
            ConnectionConfig()
            if target_id == LOCAL_MODELS_TARGET_ID and self.configured
            else None
        )

    def configuration(self, target_id, capability, engine):
        del capability, engine
        return (
            "configured"
            if target_id == LOCAL_MODELS_TARGET_ID and self.configured
            else "not_configured"
        )

    def selection_rank(self, target_id, capability, engine):
        del target_id, capability, engine
        return 0


def _base() -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=_LocalProvider(),
        credential_use_context=CredentialUseContext.open(),
    )


def _base_without_local_route() -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=_EmptyProvider(),
        credential_use_context=CredentialUseContext.open(),
    )


def _base_with_openai_route(*, configured: bool = True) -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=_OpenAIProvider(configured=configured),
        credential_use_context=CredentialUseContext.open(),
    )


def _base_with_paddleocr_local_route(*, configured: bool) -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=_PaddleOCRLocalProvider(configured=configured),
        credential_use_context=CredentialUseContext.open(),
    )


def _account() -> CloudAccount:
    return CloudAccount(
        id=41,
        name="Personal",
        kind="personal",
        balance_micro=1_000_000,
        available_micro=1_000_000,
        manage_billing=True,
    )


def _capabilities(*, ready: bool = True) -> CloudCapabilitiesResponse:
    return CloudCapabilitiesResponse(
        account_id=41,
        routes=[
            CloudCapabilityRoute(
                capability="ocr",
                engine=CLOUD_OCR_ENGINE,
                provider="anthropic",
                lane="frisket_credits",
                route_revision="route-1",
                ready=ready,
                blocker=None if ready else "Add credits to this account",
                price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=2500),
                options=CloudOCROptionSupport(
                    kind="ocr",
                    language=True,
                    geometry=True,
                    searchable_pdf=False,
                ),
            )
        ],
    )


def _llm_capabilities(*, ready: bool = True) -> CloudCapabilitiesResponse:
    return CloudCapabilitiesResponse(
        account_id=41,
        routes=[
            CloudCapabilityRoute(
                capability="llm.complete",
                engine="openai/gpt-5-mini",
                provider="openai",
                lane="frisket_credits",
                route_revision="llm-route-1",
                ready=ready,
                blocker=None if ready else "Add credits to this account",
                price=CloudTokenPrice(
                    kind="tokens",
                    input_micro_per_million_tokens=250_000,
                    output_micro_per_million_tokens=2_000_000,
                ),
                options=CloudLLMOptionSupport(
                    kind="llm",
                    structured_output=True,
                    tools=True,
                    vision=True,
                    max_output_tokens=65_536,
                ),
            )
        ],
    )


def _vision_ocr_capabilities(*, ready: bool = True) -> CloudCapabilitiesResponse:
    llm = _llm_capabilities(ready=ready).routes[0]
    return CloudCapabilitiesResponse(
        account_id=41,
        routes=[
            llm.model_copy(
                update={
                    "capability": "ocr",
                    "route_revision": "ocr-openai-route-1",
                    "options": CloudOCROptionSupport(
                        kind="ocr",
                        language=True,
                        geometry=True,
                        searchable_pdf=False,
                    ),
                }
            )
        ],
    )


@pytest.mark.parametrize(
    ("blocker", "expected_configuration"),
    [
        ("account_key_required", "not_configured"),
        ("insufficient_credits", "configured"),
        ("account_suspended", "configured"),
    ],
)
def test_cloud_configuration_distinguishes_missing_key_from_route_health(
    blocker, expected_configuration
):
    route = (
        _llm_capabilities()
        .routes[0]
        .model_copy(
            update={
                "lane": "account_key",
                "ready": False,
                "blocker": blocker,
            }
        )
    )
    composition = compose_cloud_targets(
        _base_without_local_route(),
        user_id=7,
        account=_account(),
        capabilities=CloudCapabilitiesResponse(account_id=41, routes=[route]),
        preference="local_first",
    )
    target_id = "frisket-cloud:41:account_key:openai"
    provider = composition.provider_for_resolution()

    assert (
        provider.configuration(target_id, CAPABILITY_LLM, "openai/gpt-5-mini")
        == expected_configuration
    )
    outcome = resolve(
        ResolutionRequest(
            engine="openai/gpt-5-mini",
            capability=CAPABILITY_LLM,
            options={},
            route="cloud",
        ),
        provider,
        composition.facts,
    )
    assert isinstance(outcome, Refusal)
    assert outcome.target_id == target_id
    assert blocker in outcome.remedy


def test_cloud_first_resolves_target_specific_payer_and_offering():
    composition = compose_cloud_targets(
        _base_without_local_route(),
        user_id=7,
        account=_account(),
        capabilities=_capabilities(),
        preference="cloud_first",
    )

    outcome = resolve(
        ResolutionRequest(
            engine=CLOUD_OCR_ENGINE, capability=CAPABILITY_OCR, options={}
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "frisket-cloud:41:frisket_credits:anthropic"
    assert outcome.support.transport == "frisket.cloud.media.v1"
    assert outcome.facts.cost_posture == "platform_metered"
    assert outcome.facts.credential_source == "platform_key"
    assert outcome.facts.cloud_payer is not None
    assert outcome.facts.cloud_payer.user_id == 7
    offering = composition.offering_for(
        target_id=outcome.target.id,
        capability=CAPABILITY_OCR,
        engine=CLOUD_OCR_ENGINE,
    )
    assert offering is not None
    assert offering.quote.unit_rate == "0.0025"
    assert offering.settlement.row_settlement_mode == "quoted_successful_rows"


def test_local_first_preserves_existing_local_route():
    composition = compose_cloud_targets(
        _base(),
        user_id=7,
        account=_account(),
        capabilities=_capabilities(),
        preference="local_first",
    )

    outcome = resolve(
        ResolutionRequest(engine="rapidocr", capability=CAPABILITY_OCR, options={}),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "local"
    assert outcome.facts.cost_posture == "operator_borne"
    assert outcome.facts.cloud_payer is None


def test_cloud_first_keeps_an_inherent_local_engine_on_this_computer():
    composition = compose_cloud_targets(
        _base(),
        user_id=7,
        account=_account(),
        capabilities=_capabilities(),
        preference="cloud_first",
    )

    outcome = resolve(
        ResolutionRequest(engine="rapidocr", capability=CAPABILITY_OCR, options={}),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "local"
    assert outcome.facts.cloud_payer is None


@pytest.mark.parametrize("preference", ["local_first", "cloud_first"])
def test_installed_managed_local_route_precedes_other_own_venues(preference):
    class Provider:
        targets_by_priority = (
            ExecutionTarget(
                id="models-gateway",
                operator="self",
                egress_class="operator_lan",
                engines=(
                    TargetEngineSupport(
                        engine="docling",
                        transport="sidecar.convert",
                        capability=CAPABILITY_TO_MARKDOWN,
                        options=ToMarkdownOptionSupport(),
                    ),
                ),
            ),
            ExecutionTarget(
                id=LOCAL_MODELS_TARGET_ID,
                operator="self",
                egress_class="none",
                engines=(
                    TargetEngineSupport(
                        engine="docling",
                        transport="sidecar.convert",
                        capability=CAPABILITY_TO_MARKDOWN,
                        options=ToMarkdownOptionSupport(),
                    ),
                ),
            ),
        )

        def targets(self):
            return self.targets_by_priority

        def connection(self, target_id):
            return (
                ConnectionConfig()
                if target_id
                in {
                    "models-gateway",
                    LOCAL_MODELS_TARGET_ID,
                }
                else None
            )

        def configuration(self, target_id, capability, engine):
            del capability, engine
            return (
                "configured"
                if target_id in {"models-gateway", LOCAL_MODELS_TARGET_ID}
                else "not_configured"
            )

        def selection_rank(self, target_id, capability, engine):
            del target_id, capability, engine
            return 0

    base = ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=Provider(),
        credential_use_context=CredentialUseContext.open(),
    )
    capabilities = CloudCapabilitiesResponse(
        account_id=41,
        routes=[
            CloudCapabilityRoute(
                capability="document.convert",
                engine="docling",
                provider="docling",
                lane="frisket_credits",
                route_revision="docling-route-1",
                ready=True,
                blocker=None,
                price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=2_500),
                options=CloudDocumentConvertOptionSupport(kind="document.convert"),
            )
        ],
    )
    composition = compose_cloud_targets(
        base,
        user_id=7,
        account=_account(),
        capabilities=capabilities,
        preference=preference,
    )

    outcome = resolve(
        ResolutionRequest(
            engine="docling", capability=CAPABILITY_TO_MARKDOWN, options={}
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == LOCAL_MODELS_TARGET_ID


def test_cloud_first_disconnection_preserves_non_cloud_third_party_api(tmp_path):
    class Provider:
        target = ExecutionTarget(
            id="deepl",
            operator="deepl",
            egress_class="third_party_api",
            engines=(
                TargetEngineSupport(
                    engine="deepl",
                    transport="deepl.v2",
                    capability=CAPABILITY_TRANSLATE,
                    options=TranslateOptionSupport(
                        source_language=True,
                        auto_detect_source=True,
                    ),
                ),
            ),
        )

        def targets(self):
            return (self.target,)

        def connection(self, target_id):
            return ConnectionConfig(token="deepl-key") if target_id == "deepl" else None

        def configuration(self, target_id, capability, engine):
            del capability, engine
            return "configured" if target_id == "deepl" else "not_configured"

        def selection_rank(self, target_id, capability, engine):
            del target_id, capability, engine
            return 0

    project = Project.create(tmp_path / "translate.frisket", name="Translate")
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    base = ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=Provider(),
        credential_use_context=CredentialUseContext.open(),
    )
    try:
        composition = cloud_execution_composition(
            base,
            project,
            store=CloudConnectionStore(tmp_path / "profile"),
            client=object(),
        )
        outcome = resolve(
            ResolutionRequest(
                engine="deepl",
                capability=CAPABILITY_TRANSLATE,
                options={},
            ),
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "deepl"


@pytest.mark.parametrize(
    ("target_id", "egress_class", "blocked"),
    [
        ("models-gateway", "operator_lan", True),
        (LOCAL_MODELS_TARGET_ID, "none", False),
    ],
)
def test_signed_out_cloud_first_blocks_nonlocal_routes_but_preserves_local(
    tmp_path, target_id, egress_class, blocked
):
    class Provider:
        target = ExecutionTarget(
            id=target_id,
            operator="self",
            egress_class=egress_class,
            engines=(
                TargetEngineSupport(
                    engine="docling",
                    transport="sidecar.convert",
                    capability=CAPABILITY_TO_MARKDOWN,
                    options=ToMarkdownOptionSupport(),
                ),
            ),
        )

        def targets(self):
            return (self.target,)

        def connection(self, candidate_id):
            return ConnectionConfig() if candidate_id == target_id else None

        def configuration(self, candidate_id, capability, engine):
            del capability, engine
            return "configured" if candidate_id == target_id else "not_configured"

        def selection_rank(self, candidate_id, capability, engine):
            del candidate_id, capability, engine
            return 0

    project = Project.create(tmp_path / f"{target_id}.frisket", name="Document")
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    base = ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=Provider(),
        credential_use_context=CredentialUseContext.open(),
    )
    try:
        composition = cloud_execution_composition(
            base,
            project,
            store=CloudConnectionStore(tmp_path / "profile"),
            client=object(),
        )
        outcome = resolve(
            ResolutionRequest(
                engine="docling", capability=CAPABILITY_TO_MARKDOWN, options={}
            ),
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    if blocked:
        assert isinstance(outcome, Refusal)
        assert outcome.target_id is None
        assert "Reconnect Frisket Cloud" in outcome.remedy
    else:
        assert not isinstance(outcome, Refusal)
        assert outcome.target.id == target_id


def test_unreadable_inherited_cloud_first_profile_does_not_use_own_key(tmp_path):
    class Provider:
        target = ExecutionTarget(
            id="remote-api:openai",
            operator="openai",
            egress_class="third_party_api",
            engines=(
                TargetEngineSupport(
                    engine="openai/gpt-5-mini",
                    transport="remote",
                    capability=CAPABILITY_LLM,
                    options=LLMOptionSupport(
                        structured_output=True,
                        tools=True,
                        vision=True,
                        max_output_tokens=16_384,
                    ),
                ),
            ),
        )

        def targets(self):
            return (self.target,)

        def connection(self, target_id):
            return (
                ConnectionConfig(token="openai-key")
                if target_id == "remote-api:openai"
                else None
            )

        def configuration(self, target_id, capability, engine):
            del capability, engine
            return (
                "configured" if target_id == "remote-api:openai" else "not_configured"
            )

        def selection_rank(self, target_id, capability, engine):
            del target_id, capability, engine
            return 0

    project = Project.create(tmp_path / "llm.frisket", name="LLM")
    write_project_settings(
        project,
        ProjectSettings(cloud_user_id=7, cloud_account_id=41),
    )
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection(
        "cloud-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.set_preference("cloud_first")
    store.key_path.unlink()
    base = ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=Provider(),
        credential_use_context=CredentialUseContext.open(),
    )
    try:
        composition = cloud_execution_composition(
            base,
            project,
            store=store,
            client=object(),
        )
        outcome = resolve(
            ResolutionRequest(
                engine="openai/gpt-5-mini",
                capability=CAPABILITY_LLM,
                options={},
            ),
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert isinstance(outcome, Refusal)
    assert outcome.target_id is None
    assert "Reconnect Frisket Cloud" in outcome.remedy
    assert "does not support Reconnect" not in outcome.remedy


@pytest.mark.parametrize("unreadable_profile", [False, True])
def test_unbound_legacy_project_does_not_inherit_install_cloud_first(
    tmp_path, unreadable_profile
):
    project = Project.create(tmp_path / "unbound.frisket", name="Unbound")
    store = CloudConnectionStore(tmp_path / "unbound-profile")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.set_preference("cloud_first")
    if unreadable_profile:
        store.key_path.unlink()
    try:
        assert legacy_cloud_execution_preference(project, store) == "local_first"
        composition = cloud_execution_composition(
            _base_with_openai_route(),
            project,
            store=store,
            client=object(),
        )
        request = composition.resolution_request(
            "openai/gpt-5-mini", capability=CAPABILITY_LLM, options={}
        )
        outcome = resolve(
            request,
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert composition.model_route_default is None
    assert request.route == "own"
    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "remote-api:openai"


def test_cloud_llm_route_carries_typed_price_and_payer():
    composition = compose_cloud_targets(
        _base(),
        user_id=7,
        account=_account(),
        capabilities=_llm_capabilities(),
        preference="cloud_first",
    )

    outcome = resolve(
        ResolutionRequest(
            engine="openai/gpt-5-mini",
            capability=CAPABILITY_LLM,
            options={"structured_output": True, "max_output_tokens": 4096},
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert not isinstance(outcome, Refusal)
    assert outcome.support.transport == "frisket.cloud.llm.v1"
    assert outcome.facts.cloud_payer is not None
    assert outcome.facts.cloud_payer.account_id == 41
    price = composition.provider.cloud_token_price(
        outcome.target.id, CAPABILITY_LLM, "openai/gpt-5-mini"
    )
    assert price.output_micro_per_million_tokens == 2_000_000


@pytest.mark.parametrize("ocr_first", [False, True])
def test_cloud_token_prices_are_bound_to_capability_regardless_of_route_order(
    ocr_first,
):
    llm_route = _llm_capabilities().routes[0]
    ocr_route = llm_route.model_copy(
        update={
            "capability": "ocr",
            "route_revision": "ocr-openai-route-different-price",
            "price": CloudTokenPrice(
                kind="tokens",
                input_micro_per_million_tokens=300_000,
                output_micro_per_million_tokens=2_500_000,
            ),
            "options": CloudOCROptionSupport(
                kind="ocr", language=True, geometry=True, searchable_pdf=False
            ),
        }
    )
    routes = [ocr_route, llm_route] if ocr_first else [llm_route, ocr_route]
    composition = compose_cloud_targets(
        _base_without_local_route(),
        user_id=7,
        account=_account(),
        capabilities=CloudCapabilitiesResponse(account_id=41, routes=routes),
        preference="cloud_first",
    )
    target_id = "frisket-cloud:41:frisket_credits:openai"

    llm_price = composition.provider.cloud_token_price(
        target_id, CAPABILITY_LLM, "openai/gpt-5-mini"
    )
    ocr_price = composition.provider.cloud_token_price(
        target_id, CAPABILITY_OCR, "openai/gpt-5-mini"
    )

    assert isinstance(llm_price, CloudTokenPrice)
    assert isinstance(ocr_price, CloudTokenPrice)
    assert llm_price.input_micro_per_million_tokens == 250_000
    assert llm_price.output_micro_per_million_tokens == 2_000_000
    assert ocr_price.input_micro_per_million_tokens == 300_000
    assert ocr_price.output_micro_per_million_tokens == 2_500_000


def test_cloud_vision_ocr_uses_model_transport_and_unbounded_receipt_pricing():
    composition = compose_cloud_targets(
        _base_without_local_route(),
        user_id=7,
        account=_account(),
        capabilities=_vision_ocr_capabilities(),
        preference="cloud_first",
    )
    resolved = resolve_for_action(
        object(),
        {"engine": "openai/gpt-5-mini"},
        SimpleNamespace(
            name="ocr.compare",
            consumes_resolution=True,
            execution_capability=CAPABILITY_OCR,
        ),
        composition=composition,
        bounded_scratch_input=BoundedScratchInput(
            quantity=1,
            work_scope={"identity": "scratch-vision-ocr"},
        ),
    )

    assert resolved is not None and not isinstance(resolved, Refusal)
    assert resolved.resolution.support.transport == "frisket.cloud.llm.v1"
    assert isinstance(resolved.cost_basis, PricedCostBasis)
    assert resolved.cost_basis.estimated_quantity is None
    assert resolved.cost_basis.row_quote_quantities is None
    assert resolved.cost_basis.quantity_unit == "usd"
    assert resolved.cost_basis.meter_key == "cloud_effective_cost_micro"
    assert resolved.cost_basis.pricing_key.endswith(":openai:ocr:openai/gpt-5-mini")
    [cost_claim] = [
        claim for claim in resolved.promise_set.promises if claim.field == "cost"
    ]
    assert cost_claim.op == "unbounded"


def test_legacy_cloud_first_project_keeps_cloud_when_own_key_is_configured(tmp_path):
    project = Project.create(tmp_path / "legacy.frisket", name="Legacy")
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.save_capabilities(_llm_capabilities(), expected_user_id=7)
    try:
        composition = cloud_execution_composition(
            _base_with_openai_route(), project, store=store, client=object()
        )
        request = composition.resolution_request(
            "openai/gpt-5-mini", capability=CAPABILITY_LLM, options={}
        )
        outcome = resolve(
            request,
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert request.route == "cloud"
    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "frisket-cloud:41:frisket_credits:openai"


@pytest.mark.parametrize(
    ("local_configured", "expected_route", "expected_target"),
    [
        (True, "own", LOCAL_MODELS_TARGET_ID),
        (False, "cloud", "frisket-cloud:41:frisket_credits:anthropic"),
    ],
    ids=("installed-local", "uninstalled-local"),
)
def test_legacy_cloud_first_uses_configured_local_else_actual_cloud_candidate(
    tmp_path, local_configured, expected_route, expected_target
):
    project = Project.create(tmp_path / "legacy-local.frisket", name="Legacy local")
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    store = CloudConnectionStore(tmp_path / "profile-local")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.save_capabilities(_capabilities(), expected_user_id=7)
    try:
        composition = cloud_execution_composition(
            _base_with_paddleocr_local_route(configured=local_configured),
            project,
            store=store,
            client=object(),
        )
        request = composition.resolution_request(
            CLOUD_OCR_ENGINE, capability=CAPABILITY_OCR, options={}
        )
        outcome = resolve(
            request,
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert request.route == expected_route
    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == expected_target


def test_legacy_cloud_first_connected_empty_catalog_keeps_configured_own_route(
    tmp_path,
):
    project = Project.create(tmp_path / "legacy-own.frisket", name="Legacy own")
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    store = CloudConnectionStore(tmp_path / "profile-own")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.save_capabilities(
        CloudCapabilitiesResponse(account_id=41, routes=[]), expected_user_id=7
    )
    try:
        composition = cloud_execution_composition(
            _base_with_openai_route(), project, store=store, client=object()
        )
        request = composition.resolution_request(
            "openai/gpt-5-mini", capability=CAPABILITY_LLM, options={}
        )
        outcome = resolve(
            request,
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert request.route == "own"
    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "remote-api:openai"


def test_materialized_cloud_default_connected_empty_catalog_keeps_configured_own(
    tmp_path,
):
    project = Project.create(tmp_path / "materialized-own.frisket", name="Own")
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            model_execution_routes={},
            model_execution_default="cloud",
        ),
    )
    store = CloudConnectionStore(tmp_path / "profile-materialized-own")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.save_capabilities(
        CloudCapabilitiesResponse(account_id=41, routes=[]), expected_user_id=7
    )
    try:
        composition = cloud_execution_composition(
            _base_with_openai_route(), project, store=store, client=object()
        )
        request = composition.resolution_request(
            "openai/gpt-5-mini", capability=CAPABILITY_LLM, options={}
        )
        outcome = resolve(
            request,
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert request.route == "own"
    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "remote-api:openai"


def test_local_first_omits_cloud_llm_when_exact_local_adapter_exists():
    class Router:
        def adapter_for_model(self, model):
            return object() if model == "openai/gpt-5-mini" else None

    composition = compose_cloud_targets(
        _base_with_openai_route(),
        user_id=7,
        account=_account(),
        capabilities=_llm_capabilities(ready=False),
        preference="local_first",
        router=Router(),
    )

    outcome = resolve(
        ResolutionRequest(
            engine="openai/gpt-5-mini",
            capability=CAPABILITY_LLM,
            options={},
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "remote-api:openai"
    assert outcome.facts.cloud_payer is None
    assert {target.id for target in composition.provider.targets()} >= {
        "remote-api:openai",
        "frisket-cloud:41:frisket_credits:openai",
    }


def test_local_first_uses_cloud_llm_when_local_adapter_is_missing():
    class Router:
        def adapter_for_model(self, model):
            del model
            return None

    composition = compose_cloud_targets(
        _base_with_openai_route(),
        user_id=7,
        account=_account(),
        capabilities=_llm_capabilities(),
        preference="local_first",
        router=Router(),
    )

    outcome = resolve(
        ResolutionRequest(
            engine="openai/gpt-5-mini",
            capability=CAPABILITY_LLM,
            options={},
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "remote-api:openai"
    assert outcome.facts.cloud_payer is None


def test_local_first_uses_configured_cloud_route_when_own_key_is_missing():
    composition = compose_cloud_targets(
        _base_with_openai_route(configured=False),
        user_id=7,
        account=_account(),
        capabilities=_llm_capabilities(),
        preference="local_first",
    )

    outcome = resolve(
        ResolutionRequest(
            engine="openai/gpt-5-mini",
            capability=CAPABILITY_LLM,
            options={},
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "frisket-cloud:41:frisket_credits:openai"
    assert outcome.facts.cloud_payer is not None


def test_local_first_broken_own_key_refuses_without_switching_to_cloud():
    class Provider(_OpenAIProvider):
        def connection(self, target_id):
            raise TargetConfigurationError(
                target_id,
                "Re-enter the configured OpenAI key; it could not be decrypted.",
            )

    base = ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=Provider(),
        credential_use_context=CredentialUseContext.open(),
    )
    composition = compose_cloud_targets(
        base,
        user_id=7,
        account=_account(),
        capabilities=_llm_capabilities(),
        preference="local_first",
    )

    outcome = resolve(
        ResolutionRequest(
            engine="openai/gpt-5-mini",
            capability=CAPABILITY_LLM,
            options={},
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert isinstance(outcome, Refusal)
    assert outcome.target_id == "remote-api:openai"
    assert "could not be decrypted" in outcome.remedy


def test_local_first_preserves_media_route_when_cloud_target_also_has_llm():
    class Router:
        def adapter_for_model(self, model):
            del model
            return None

    capabilities = CloudCapabilitiesResponse(
        account_id=41,
        routes=[
            *_llm_capabilities().routes,
            CloudCapabilityRoute(
                capability="ocr",
                engine="datalab",
                provider="openai",
                lane="frisket_credits",
                route_revision="ocr-route-1",
                ready=True,
                blocker=None,
                price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=2_500),
                options=CloudOCROptionSupport(
                    kind="ocr",
                    language=False,
                    geometry=True,
                    searchable_pdf=False,
                ),
            ),
        ],
    )
    composition = compose_cloud_targets(
        _base_with_openai_route(),
        user_id=7,
        account=_account(),
        capabilities=capabilities,
        preference="local_first",
        router=Router(),
    )

    llm = resolve(
        ResolutionRequest(
            engine="openai/gpt-5-mini",
            capability=CAPABILITY_LLM,
            options={},
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )
    media = resolve(
        ResolutionRequest(engine="datalab", capability=CAPABILITY_OCR, options={}),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert not isinstance(llm, Refusal)
    assert llm.target.id == "remote-api:openai"
    assert not isinstance(media, Refusal)
    assert media.target.id == "remote-api:openai"
    assert media.facts.cloud_payer is None


def test_structured_cloud_llm_action_requires_explicit_unknown_quantity_consent(
    tmp_path,
):
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import (
        _typed_map_rows_plan,
        bound_typed_program_request_from_runner_spec,
    )

    project = Project.create(tmp_path / "llm.frisket", name="LLM")
    sheet_id = project.add_sheet("S")
    source_id = project.add_column(sheet_id, "source", type="text")
    [row_id] = project.add_rows(
        sheet_id, [{"source": "cloud quoted input"}], {"source": source_id}
    )
    plan = _typed_map_rows_plan(
        typed_action_for_request(
            {
                "action_id": "map.extract",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": ["source"],
                    "model": "openai/gpt-5-mini",
                    "instruction": "Extract the value",
                    "fields": [{"name": "value", "type": "text"}],
                    "grounding": {"enabled": False},
                },
                "idempotency_key": "cloud-llm-route-test",
            }
        )
    )
    spec = plan.spec_dict()
    bound = bound_typed_program_request_from_runner_spec(spec)
    assert bound is not None
    recipe = _typed_map_rows_plan(bound).program
    composition = compose_cloud_targets(
        _base(),
        user_id=7,
        account=_account(),
        capabilities=_llm_capabilities(),
        preference="cloud_first",
    )
    try:
        resolved = resolve_for_action(
            project,
            spec,
            recipe,
            composition=composition,
            scope_row_ids=[row_id],
        )
        own_key = resolve_for_action(
            project,
            spec,
            recipe,
            composition=compose_cloud_targets(
                _base_with_openai_route(),
                user_id=7,
                account=_account(),
                capabilities=_llm_capabilities(),
                preference="local_first",
            ),
            scope_row_ids=[row_id],
        )
        blocked_cloud = resolve_for_action(
            project,
            spec,
            recipe,
            composition=compose_cloud_targets(
                _base_with_openai_route(),
                user_id=7,
                account=_account(),
                capabilities=_llm_capabilities(ready=False),
                preference="cloud_first",
            ),
            scope_row_ids=[row_id],
        )
    finally:
        project.close()

    assert resolved is not None and not isinstance(resolved, Refusal)
    assert own_key is None
    assert isinstance(blocked_cloud, Refusal)
    assert blocked_cloud.target_id == "frisket-cloud:41:frisket_credits:openai"
    assert "Add credits" in blocked_cloud.remedy
    assert isinstance(resolved.cost_basis, PricedCostBasis)
    assert resolved.cost_basis.meter_key == "cloud_effective_cost_micro"
    assert resolved.cost_basis.row_quote_quantities is None
    assert resolved.cost_basis.bound is None
    [cost_claim] = [
        claim for claim in resolved.promise_set.promises if claim.field == "cost"
    ]
    assert cost_claim.op == "unbounded"
    assert cost_claim.basis["pricing_key"] == resolved.cost_basis.pricing_key


def test_cloud_llm_map_run_uses_routed_transport_and_retires_child_checkpoint(
    tmp_path,
):
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import (
        _typed_map_rows_plan,
        bound_typed_program_request_from_runner_spec,
    )
    from frisket.execution.attempt_authority import AttemptAuthority

    class Transport:
        calls: list[tuple[CloudExecutionAttempt, object]]

        def __init__(self) -> None:
            self.calls = []

        async def execute_llm(self, attempt, request):
            self.calls.append((attempt, request))
            return CloudExecutionResponse(
                execution_id="cloud-map-execution-1",
                status="completed",
                route=_llm_capabilities().routes[0],
                result=CloudLLMResult(
                    content='{"value":"Cloud result"}',
                    data={"value": "Cloud result"},
                    tokens_in=12,
                    tokens_out=4,
                    cost=0.00003,
                    model="openai/gpt-5-mini",
                    cost_source="cloud_receipt",
                    cached=False,
                    provider="openai",
                    credential_source="platform_key",
                    raw={},
                    output_limited=False,
                ),
                receipt=CloudExecutionReceipt(
                    user_id=7,
                    account_id=41,
                    lane="frisket_credits",
                    provider="openai",
                    engine="openai/gpt-5-mini",
                    charge_micro=30,
                    provider_cost_micro=20,
                    usage=CloudLLMUsage(
                        kind="llm.complete", tokens_in=12, tokens_out=4
                    ),
                ),
            )

    project = Project.create(tmp_path / "llm-run.frisket", name="LLM")
    sheet_id = project.add_sheet("S")
    source_id = project.add_column(sheet_id, "source", type="text")
    project.add_rows(sheet_id, [{"source": "route this row"}], {"source": source_id})
    plan = _typed_map_rows_plan(
        typed_action_for_request(
            {
                "action_id": "map.extract",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": ["source"],
                    "model": "openai/gpt-5-mini",
                    "instruction": "Extract the value",
                    "fields": [{"name": "value", "type": "text"}],
                    "grounding": {"enabled": False},
                },
                "idempotency_key": "cloud-llm-map-run",
            }
        )
    )
    spec = plan.spec_dict()
    bound = bound_typed_program_request_from_runner_spec(spec)
    assert bound is not None
    recipe = _typed_map_rows_plan(bound).program
    transport = Transport()
    composition = compose_cloud_targets(
        _base(),
        user_id=7,
        account=_account(),
        capabilities=_llm_capabilities(),
        preference="cloud_first",
        execution_transport=transport,
    )
    claim_token = "output-claim:cloud-llm-map-run"
    ReceiptStore(project).insert_running(
        Receipt(
            receipt_id="cloud-llm-map-run",
            project_id="project",
            action_id="cloud-llm-map-run",
            action_kind="map.extract",
            status="running",
        )
    )
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["value"],
        action_kind="map.extract",
        receipt_id="cloud-llm-map-run",
        claim_token=claim_token,
    )
    assert conflict is None and len(claims) == 1

    def bind_claim(progress):
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=claim_token,
            run_id=progress.run_id,
            expected_output_names=["value"],
        )

    runner = MapRunner(
        project,
        ModelRouter(use_env_keys=False, cache_mode="off", max_retries=0),
        concurrency=1,
        authority=AttemptAuthority(project, composition=composition),
        on_progress=bind_claim,
        allow_action_lifecycle_only_recipes=True,
        execution_composition=composition,
    )
    try:
        with pytest.raises(ClaimsGate) as confirmation:
            asyncio.run(runner.run(spec, program=recipe, claim_token=claim_token))
        confirmed = {
            **spec,
            "consented_promise_set_hash": confirmation.value.promise_set_hash,
        }
        progress = asyncio.run(
            runner.run(
                confirmed,
                program=recipe,
                confirmed=True,
                claim_token=claim_token,
            )
        )

        assert progress.done is True
        assert progress.halted_code is None, progress.halted_reason
        assert len(transport.calls) == 1
        [fact] = project.db.execute(
            "SELECT id,provider,credential_source,units,epoch_id FROM model_calls"
        ).fetchall()
        assert fact["id"] == "cloud-map-execution-1"
        assert fact["provider"] == "openai"
        assert fact["credential_source"] == "platform_key"
        assert fact["epoch_id"] is not None
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints WHERE family='model_call'"
            ).fetchone()[0]
            == 0
        )
    finally:
        asyncio.run(runner.router.aclose())
        project.close()


def test_blocked_cloud_first_route_does_not_fall_back_to_local():
    composition = compose_cloud_targets(
        _base_without_local_route(),
        user_id=7,
        account=_account(),
        capabilities=_capabilities(ready=False),
        preference="cloud_first",
    )

    outcome = resolve(
        ResolutionRequest(
            engine=CLOUD_OCR_ENGINE, capability=CAPABILITY_OCR, options={}
        ),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert isinstance(outcome, Refusal)
    assert outcome.target_id == "frisket-cloud:41:frisket_credits:anthropic"
    assert "Add credits" in outcome.remedy


def test_project_composition_reads_selected_account_capabilities_without_network(
    tmp_path,
):
    project = Project.create(tmp_path / "project.frisket", name="Project")
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.save_capabilities(_capabilities(), expected_user_id=7)

    class Client:
        calls = 0

        def capabilities(self, token, account_id):
            self.calls += 1
            raise AssertionError((token, account_id))

    client = Client()
    try:
        composition = cloud_execution_composition(
            _base_without_local_route(), project, store=store, client=client
        )
    finally:
        project.close()

    assert client.calls == 0
    assert store.load().capabilities[41].routes == _capabilities().routes
    assert (
        composition.cloud_payer_for(
            target_id="frisket-cloud:41:frisket_credits:anthropic",
            capability="ocr",
            engine=CLOUD_OCR_ENGINE,
        ).user_id
        == 7
    )


def test_project_composition_uses_last_known_capabilities_without_refetch(tmp_path):
    project = Project.create(tmp_path / "project.frisket", name="Project")
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.save_capabilities(_capabilities(), expected_user_id=7)

    class Client:
        def capabilities(self, _token, _account_id):
            raise AssertionError("a stored route snapshot must avoid discovery")

    try:
        composition = cloud_execution_composition(
            _base_without_local_route(), project, store=store, client=Client()
        )
        outcome = resolve(
            ResolutionRequest(
                engine=CLOUD_OCR_ENGINE, capability=CAPABILITY_OCR, options={}
            ),
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "frisket-cloud:41:frisket_credits:anthropic"


def test_expired_connection_blocks_snapshot_without_fetch_or_local_fallback(tmp_path):
    project = Project.create(tmp_path / "project.frisket", name="Project")
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection(
        "expired-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2020-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.save_capabilities(_capabilities(), expected_user_id=7)

    class Client:
        def capabilities(self, _token, _account_id):
            raise AssertionError("expired connections must not make a discovery call")

    try:
        composition = cloud_execution_composition(
            _base_without_local_route(), project, store=store, client=Client()
        )
        outcome = resolve(
            ResolutionRequest(
                engine=CLOUD_OCR_ENGINE, capability=CAPABILITY_OCR, options={}
            ),
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert isinstance(outcome, Refusal)
    assert outcome.target_id == "frisket-cloud:41:frisket_credits:anthropic"
    assert "Reconnect" in outcome.remedy

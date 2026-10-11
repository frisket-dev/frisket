from __future__ import annotations

from frisket.cloud_connection import CloudConnectionStore
from frisket.cloud_targets import cloud_execution_composition, compose_cloud_targets
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudLLMOptionSupport,
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
)
from frisket.execution.resolver import Refusal, ResolutionRequest, resolve
from frisket.execution.resolve_for_action import resolve_for_action
from frisket.execution.targets import (
    CAPABILITY_LLM,
    CAPABILITY_OCR,
    ExecutionTarget,
    OcrOptionSupport,
    TargetEngineSupport,
)
from frisket.engine.store import Project
from frisket.project_settings import ProjectSettings, write_project_settings


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


def _base() -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=_LocalProvider(),
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
                engine="rapidocr",
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


def test_cloud_first_resolves_target_specific_payer_and_offering():
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
    assert outcome.target.id == "frisket-cloud:41:frisket_credits:anthropic"
    assert outcome.support.transport == "frisket.cloud.media.v1"
    assert outcome.facts.cost_posture == "platform_metered"
    assert outcome.facts.credential_source == "platform_key"
    assert outcome.facts.cloud_payer is not None
    assert outcome.facts.cloud_payer.user_id == 7
    offering = composition.offering_for(
        target_id=outcome.target.id,
        capability=CAPABILITY_OCR,
        engine="rapidocr",
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
    price = composition.provider.cloud_llm_price(outcome.target.id, "openai/gpt-5-mini")
    assert price.output_micro_per_million_tokens == 2_000_000


def test_local_first_omits_cloud_llm_when_exact_local_adapter_exists():
    class Router:
        def adapter_for_model(self, model):
            return object() if model == "openai/gpt-5-mini" else None

    composition = compose_cloud_targets(
        _base(),
        user_id=7,
        account=_account(),
        capabilities=_llm_capabilities(),
        preference="local_first",
        router=Router(),
    )

    assert all(
        support.capability != CAPABILITY_LLM
        for target in composition.provider.targets()
        for support in target.engines
    )


def test_cloud_llm_action_quote_is_allocated_to_exact_rows(tmp_path):
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
    finally:
        project.close()

    assert resolved is not None and not isinstance(resolved, Refusal)
    assert isinstance(resolved.cost_basis, PricedCostBasis)
    assert resolved.cost_basis.meter_key == "cloud_effective_cost_micro"
    assert resolved.cost_basis.row_quote_quantities[0][0] == row_id
    assert resolved.cost_basis.bound is not None
    assert resolved.cost_basis.bound > 0


def test_blocked_cloud_first_route_does_not_fall_back_to_local():
    composition = compose_cloud_targets(
        _base(),
        user_id=7,
        account=_account(),
        capabilities=_capabilities(ready=False),
        preference="cloud_first",
    )

    outcome = resolve(
        ResolutionRequest(engine="rapidocr", capability=CAPABILITY_OCR, options={}),
        composition.provider_for_resolution(),
        composition.facts,
    )

    assert isinstance(outcome, Refusal)
    assert outcome.target_id == "frisket-cloud:41:frisket_credits:anthropic"
    assert "Add credits" in outcome.remedy


def test_project_composition_fetches_selected_account_capabilities_once(tmp_path):
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

    class Client:
        calls = 0

        def capabilities(self, token, account_id):
            assert token == "installation-token"
            assert account_id == 41
            self.calls += 1
            return _capabilities()

    client = Client()
    try:
        composition = cloud_execution_composition(
            _base(), project, store=store, client=client
        )
    finally:
        project.close()

    assert client.calls == 1
    assert (
        composition.cloud_payer_for(
            target_id="frisket-cloud:41:frisket_credits:anthropic",
            capability="ocr",
            engine="rapidocr",
        ).user_id
        == 7
    )

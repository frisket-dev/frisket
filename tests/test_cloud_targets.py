from __future__ import annotations

import asyncio

import pytest

from frisket.ai.llm import ModelRouter
from frisket.contracts.action import Receipt
from frisket.cloud_execution import CloudExecutionAttempt
from frisket.cloud_connection import CloudClientError, CloudConnectionStore
from frisket.cloud_targets import cloud_execution_composition, compose_cloud_targets
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
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
from frisket.engine.runner import MapRunner
from frisket.engine.runner.validation import ClaimsGate
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
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
    assert store.load().capabilities[41].routes == _capabilities().routes
    assert (
        composition.cloud_payer_for(
            target_id="frisket-cloud:41:frisket_credits:anthropic",
            capability="ocr",
            engine="rapidocr",
        ).user_id
        == 7
    )


def test_capability_fetch_failure_blocks_last_known_cloud_routes(tmp_path):
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
            raise CloudClientError("cloud_unreachable", retryable=True)

    try:
        composition = cloud_execution_composition(
            _base(), project, store=store, client=Client()
        )
        outcome = resolve(
            ResolutionRequest(engine="rapidocr", capability=CAPABILITY_OCR, options={}),
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert isinstance(outcome, Refusal)
    assert outcome.target_id == "frisket-cloud:41:frisket_credits:anthropic"
    assert "temporarily unavailable" in outcome.remedy


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
            _base(), project, store=store, client=Client()
        )
        outcome = resolve(
            ResolutionRequest(engine="rapidocr", capability=CAPABILITY_OCR, options={}),
            composition.provider_for_resolution(),
            composition.facts,
        )
    finally:
        project.close()

    assert isinstance(outcome, Refusal)
    assert outcome.target_id == "frisket-cloud:41:frisket_credits:anthropic"
    assert "Reconnect" in outcome.remedy

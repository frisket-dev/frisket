from __future__ import annotations

from frisket.cloud_connection import CloudConnectionStore
from frisket.cloud_targets import cloud_execution_composition, compose_cloud_targets
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudOCROptionSupport,
    CloudSessionResponse,
    CloudUnitPrice,
    CloudUser,
)
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.price_book import OperatorBorne
from frisket.execution.provider import (
    CompositionFacts,
    ConnectionConfig,
    ExecutionComposition,
)
from frisket.execution.resolver import Refusal, ResolutionRequest, resolve
from frisket.execution.targets import (
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

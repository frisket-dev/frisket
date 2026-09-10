from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from frisket.execution.commercial import (
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialQuoteRounding,
    CommercialQuoteTerms,
    CommercialSettlementTerms,
)
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.definitions import (
    LOCAL_ONNX_TARGET_ID,
    MODELS_GATEWAY_TARGET_ID,
    StaticExecutionTargetProvider,
    build_static_targets,
)
from frisket.execution.price_book import ByokZero, PlatformMetered
from frisket.execution.provider import (
    CompositionFacts,
    ConnectionConfig,
    ExactExecutionMatch,
    ExecutionComposition,
    ExecutionCompositionContext,
)
from frisket.execution.targets import CAPABILITY_TO_MARKDOWN
from frisket.server.action_catalog_hints import (
    project_action_catalog_launcher_hints,
)


class _SyntheticProvider:
    def __init__(
        self,
        *,
        live: bool = True,
        source_target_id: str = LOCAL_ONNX_TARGET_ID,
        target_id: str = "synthetic-shared",
    ) -> None:
        local = next(
            target for target in build_static_targets() if target.id == source_target_id
        )
        self.target = replace(
            local,
            id=target_id,
            operator="synthetic-operator",
            egress_class="frisket_shared",
        )
        self.live = live

    def targets(self):
        return (self.target,)

    def connection(self, target_id: str):
        if target_id != self.target.id or not self.live:
            return None
        return ConnectionConfig()


def _offering(
    *,
    target_id: str = "synthetic-shared",
    engine: str = "parakeet-tdt",
    capability: str = "transcribe",
) -> CommercialOffering:
    document = capability == CAPABILITY_TO_MARKDOWN
    return CommercialOffering(
        match=CommercialOfferingMatch(
            target_id=target_id,
            capability=capability,
            engine=engine,
        ),
        quote=CommercialQuoteTerms(
            pricing_key="synthetic.audio-minute",
            terms_version="synthetic.terms.v7",
            unit_rate="0.031",
            quantity_unit="page" if document else "audio_minute",
            rounding=CommercialQuoteRounding(
                mode="half_even",
                decimal_places=6,
            ),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="pages" if document else "audio_seconds",
            meter_units_per_quantity_unit="1" if document else "60",
            ceiling_mode="consented_quantity",
            row_settlement_mode=(
                "all_metered" if document else "quoted_successful_rows"
            ),
        ),
        charge_authority="synthetic-charge-authority",
        presentation=CommercialPresentation(
            venue_label="Synthetic shared transcription",
            billing_label="Billed by the synthetic charge authority",
        ),
    )


def _platform_composition(*, with_offer: bool) -> ExecutionComposition:
    provider = _SyntheticProvider()
    return ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-catalog",
            funding=PlatformMetered(),
        ),
        provider=provider,
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
        offerings=(_offering(),) if with_offer else (),
    )


def _included_docling_composition(
    *, live: bool = True, commercially_offered: bool = False
) -> ExecutionComposition:
    provider = _SyntheticProvider(
        live=live,
        source_target_id=MODELS_GATEWAY_TARGET_ID,
        target_id=MODELS_GATEWAY_TARGET_ID,
    )
    return ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-catalog",
            funding=PlatformMetered(),
        ),
        provider=provider,
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
        included=()
        if commercially_offered
        else (
            ExactExecutionMatch(
                target_id=MODELS_GATEWAY_TARGET_ID,
                capability=CAPABILITY_TO_MARKDOWN,
                engine="docling",
            ),
        ),
        offerings=(
            _offering(
                target_id=MODELS_GATEWAY_TARGET_ID,
                capability=CAPABILITY_TO_MARKDOWN,
                engine="docling",
            ),
        )
        if commercially_offered
        else (),
    )


def test_platform_funding_without_offer_adds_no_catalog_option() -> None:
    hints = project_action_catalog_launcher_hints(
        {}, execution_composition=_platform_composition(with_offer=False)
    )

    assert "engines" not in hints["media.transcribe"]


def test_transient_probe_failure_keeps_included_gateway_engine_selectable() -> None:
    hints = project_action_catalog_launcher_hints(
        {
            "configured": True,
            "available": False,
            "engines": [],
            "error": "ReadTimeout: worker is cold",
            "transient_failure": True,
        },
        execution_composition=_included_docling_composition(),
    )

    [docling] = hints["media.to_markdown"]["engines"]
    assert docling["id"] == "docling"
    assert docling["available"] is True
    assert docling["target_id"] == MODELS_GATEWAY_TARGET_ID
    assert "error" not in docling
    assert docling["targets"] == [
        {
            "target": MODELS_GATEWAY_TARGET_ID,
            "target_id": MODELS_GATEWAY_TARGET_ID,
            "available": True,
        }
    ]


def test_cloud_composition_marker_keeps_gateway_engine_selectable() -> None:
    hints = project_action_catalog_launcher_hints(
        {"catalog_from_composition": True},
        execution_composition=_included_docling_composition(),
    )

    [docling] = hints["media.to_markdown"]["engines"]
    assert docling["id"] == "docling"
    assert docling["available"] is True
    assert docling["target_id"] == MODELS_GATEWAY_TARGET_ID


def test_transient_probe_failure_does_not_hide_dead_composition_target() -> None:
    hints = project_action_catalog_launcher_hints(
        {
            "configured": True,
            "available": False,
            "engines": [],
            "error": "ReadTimeout: worker is cold",
            "transient_failure": True,
        },
        execution_composition=_included_docling_composition(live=False),
    )

    [docling] = hints["media.to_markdown"]["engines"]
    assert docling["available"] is False
    assert "not currently available" in docling["error"]


def test_explicit_engine_failure_wins_over_live_composition_target() -> None:
    hints = project_action_catalog_launcher_hints(
        {
            "configured": True,
            "available": True,
            "engines": [
                {
                    "name": "docling",
                    "route": "/to-markdown",
                    "available": False,
                    "error": "docling worker failed to load",
                }
            ],
            "error": None,
        },
        execution_composition=_included_docling_composition(commercially_offered=True),
    )

    [docling] = hints["media.to_markdown"]["engines"]
    assert docling["available"] is False
    assert docling["error"] == "docling worker failed to load"
    assert docling["targets"][0]["available"] is False
    assert docling["targets"][0]["error"] == "docling worker failed to load"


def test_free_public_api_catalog_hints_are_classification_not_prices(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    hints = project_action_catalog_launcher_hints({})

    geocode = hints["enrich.geocode"]
    assert "pricing" not in geocode
    assert set(geocode["pricing_options"]) == {"opencage"}
    assert geocode["cost_source"] == "free_public_api"
    assert geocode["cost_source_options"] == {"nominatim": "free_public_api"}

    census = hints["enrich.census_demographics"]
    assert "pricing" not in census
    assert "pricing_options" not in census
    assert census["cost_source"] == "free_public_api"


def test_platform_composition_keeps_only_free_public_api_catalog_options(
    monkeypatch,
) -> None:
    # Exercise the dangerous shape: an OpenCage key makes the uncomposed
    # recipe catalog carry a real provider tariff, but funding without an
    # injected offer must not let that tariff or engine cross this request.
    monkeypatch.setenv("OPENCAGE_API_KEY", "configured-own-key")
    composition = ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-free-public",
            funding=PlatformMetered(),
        ),
        provider=StaticExecutionTargetProvider(),
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
    )

    hints = project_action_catalog_launcher_hints({}, execution_composition=composition)
    geocode = hints["enrich.geocode"]
    assert [engine["id"] for engine in geocode["engines"]] == ["nominatim"]
    assert "pricing" not in geocode
    assert "pricing_options" not in geocode
    assert geocode["cost_source_options"] == {"nominatim": "free_public_api"}

    census = hints["enrich.census_demographics"]
    assert [engine["id"] for engine in census["engines"]] == ["us_census_acs"]
    assert "pricing" not in census
    assert "pricing_options" not in census
    assert census["cost_source"] == "free_public_api"


def test_exact_injected_offer_is_the_only_platform_catalog_option() -> None:
    hints = project_action_catalog_launcher_hints(
        {}, execution_composition=_platform_composition(with_offer=True)
    )

    [engine] = hints["media.transcribe"]["engines"]
    assert engine["id"] == "parakeet-tdt"
    assert engine["target_id"] == "synthetic-shared"
    assert engine["available"] is True
    assert engine["billable"] is True
    pricing = engine["pricing"]
    assert pricing == {
        "key": "synthetic.audio-minute",
        "label": "Billed by the synthetic charge authority",
        "provider": "synthetic-operator",
        "unit": "audio_minute",
        "unit_price_usd": 0.031,
        "unit_price_usd_string": "0.031",
        "env_var": None,
        "billable": True,
        "external_api": True,
        "cost_source": "pricing_data",
        "description": "Synthetic shared transcription",
        "terms_version": "synthetic.terms.v7",
        "charge_authority": "synthetic-charge-authority",
        "venue_label": "Synthetic shared transcription",
        "billing_label": "Billed by the synthetic charge authority",
    }
    assert engine["targets"] == [
        {
            "target": "synthetic-shared",
            "target_id": "synthetic-shared",
            "available": True,
            "billable": True,
            "pricing": pricing,
        }
    ]


def test_edition_target_can_offer_whisper_turbo_without_a_local_gateway() -> None:
    provider = _SyntheticProvider(
        source_target_id=MODELS_GATEWAY_TARGET_ID,
        target_id="modal:edition-models",
    )
    composition = ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-catalog",
            funding=PlatformMetered(),
        ),
        provider=provider,
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
        offerings=(
            _offering(
                target_id="modal:edition-models",
                engine="whisper-turbo",
            ),
        ),
    )

    hints = project_action_catalog_launcher_hints({}, execution_composition=composition)

    [engine] = hints["media.transcribe"]["engines"]
    assert engine["id"] == "whisper-turbo"
    assert engine["available"] is True
    assert engine["target_id"] == "modal:edition-models"
    assert engine["targets"] == [
        {
            "target": "modal:edition-models",
            "target_id": "modal:edition-models",
            "available": True,
            "billable": True,
            "pricing": engine["pricing"],
        }
    ]


def test_wildcard_target_materializes_only_the_exact_offered_engine() -> None:
    """A provider wildcard is capability, not a commercial catalog."""
    provider = _SyntheticProvider(
        source_target_id="remote-api:openai",
        target_id="synthetic-openai",
    )
    composition = ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-catalog",
            funding=PlatformMetered(),
        ),
        provider=provider,
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
        offerings=(
            _offering(
                target_id="synthetic-openai",
                engine="openai/whisper-1",
            ),
        ),
    )

    hints = project_action_catalog_launcher_hints({}, execution_composition=composition)

    [engine] = hints["media.transcribe"]["engines"]
    assert engine["id"] == "openai/whisper-1"
    assert engine["target_id"] == "synthetic-openai"
    assert engine["pricing"]["key"] == "synthetic.audio-minute"


def test_own_key_composition_keeps_provider_direct_options() -> None:
    composition = ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-byok",
            funding=ByokZero(),
        ),
        provider=StaticExecutionTargetProvider(),
        credential_use_context=CredentialUseContext(cost_posture="org_key"),
    )
    hints = project_action_catalog_launcher_hints({}, execution_composition=composition)

    engines = {engine["id"]: engine for engine in hints["media.transcribe"]["engines"]}
    assert "openai/whisper-1" in engines
    gateway = next(
        row
        for row in engines["parakeet-tdt"]["targets"]
        if row["target"] == MODELS_GATEWAY_TARGET_ID
    )
    assert "pricing" not in gateway


def test_project_route_builds_composition_from_the_same_effective_router(
    tmp_path: Path,
) -> None:
    from frisket.ai.llm import ModelRouter
    from frisket.engine.store import Project
    from frisket.server.routes.actions import register_action_catalog_routes

    project = Project.create(tmp_path / "catalog.frisket", name="catalog")
    router = ModelRouter(use_env_keys=False, local_endpoints=())
    composition = _platform_composition(with_offer=False)
    project_id = project.path.stem

    class _Workspace:
        executor_deps_factory = None

        @staticmethod
        def edition_execution_composition_context_for(_request):
            return ExecutionCompositionContext.direct()

        def get(self, pid: str):
            assert pid == project_id
            return project

        def router_for(self, value):
            assert value is project
            return router

        def execution_composition_for(self, value, effective_router, _context):
            assert value is project
            assert effective_router is router
            return composition

        def org_provider_keys(self):
            return {}

    app = FastAPI()
    register_action_catalog_routes(
        app,
        workspace=_Workspace(),  # type: ignore[arg-type]
        sidecar_capabilities=lambda: {},
    )
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/projects/{pid}/actions/v1/catalog"
    )
    request = SimpleNamespace(state=SimpleNamespace())
    payload = route.endpoint(request, project_id).model_dump()

    transcribe = next(
        entry for entry in payload["actions"] if entry["kind"] == "media.transcribe"
    )
    assert "engines" not in transcribe["ui_hints"]
    project.close()


def test_project_route_catalog_uses_injected_router_credentials(tmp_path: Path) -> None:
    """An injected base router is an execution credential source too."""
    from frisket.ai.llm import ModelRouter
    from frisket.server.routes.actions import register_action_catalog_routes
    from frisket.server.workspace import Workspace

    router = ModelRouter(
        keys={"openai": "injected-openai-key"},
        use_env_keys=False,
        local_endpoints=(),
    )
    workspace = Workspace(tmp_path / "workspace", router=router)
    created = workspace.create("Catalog", project_id="catalog")

    app = FastAPI()
    register_action_catalog_routes(
        app,
        workspace=workspace,
        sidecar_capabilities=lambda: {},
    )
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/projects/{pid}/actions/v1/catalog"
    )
    request = SimpleNamespace(state=SimpleNamespace())
    payload = route.endpoint(request, created["id"]).model_dump()
    by_kind = {entry["kind"]: entry for entry in payload["actions"]}

    transcribe = {
        engine["id"]: engine
        for engine in by_kind["media.transcribe"]["ui_hints"]["engines"]
    }
    ner = {engine["id"]: engine for engine in by_kind["map.ner"]["ui_hints"]["engines"]}
    assert "openai" in workspace.router_for(workspace.get(created["id"])).providers()
    assert transcribe["openai/whisper-1"]["available"] is True
    assert ner["llm"]["available"] is True
    workspace.get(created["id"]).close()

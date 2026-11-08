"""Selector reads are part of the actual app, including incomplete action drafts."""

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_application_serves_project_selector_for_an_incomplete_draft(tmp_path):
    app = create_app(tmp_path / "workspace", serve_spa=False)
    project = app.state.workspace.create("Choices", project_id="choices")
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project['id']}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "map.translate",
                    "field": "engine",
                    "params": {},
                },
            },
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "frisket.selector_choices.v3"
    assert payload["project_id"] == project["id"]
    assert payload["groups"]


def test_saved_gateway_reaches_selector_and_direct_execution(tmp_path, monkeypatch):
    from frisket.server.services import models_gateway

    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    monkeypatch.setattr(
        models_gateway,
        "probe_models_gateway",
        lambda *_args, **_kwargs: {
            "ok": True,
            "reachable": True,
            "status": 200,
            "detail": None,
            "service": "frisket-models",
            "version": "1",
            "engines": [],
        },
    )
    app = create_app(tmp_path / "workspace", serve_spa=False)
    workspace = app.state.workspace
    workspace.create("Choices", project_id="choices")
    candidate = {"origin": "https://models.example.test", "token": "gateway-test-key"}
    with TestClient(app) as client:
        validated = client.post("/api/models-gateway/validate", json=candidate)
        assert validated.status_code == 200, validated.text
        saved = client.put(
            "/api/models-gateway",
            json={
                **candidate,
                "validation_token": validated.json()["validation_token"],
            },
        )
        assert saved.status_code == 200, saved.text
        response = client.post(
            "/api/projects/choices/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "media.transcribe",
                    "field": "engine",
                    "params": {},
                },
            },
        )
    assert response.status_code == 200, response.text
    gateway_choices = [
        choice
        for group in response.json()["groups"]
        for choice in group["choices"]
        if choice["resolved_target"]
        and choice["resolved_target"]["target_id"] == "models-gateway"
    ]
    assert gateway_choices
    assert any(choice["can_run"] for choice in gateway_choices)
    project = workspace.get("choices")
    composition = workspace.execution_composition_for(
        project,
        workspace.action_execution_router_for(project),
        workspace.execution_composition_context_for(),
    )
    connection = composition.provider.connection("models-gateway")
    assert connection is not None
    assert connection.base_url == candidate["origin"]
    assert connection.token == candidate["token"]


def test_native_hosted_selector_distinguishes_platform_and_byok_credentials(tmp_path):
    from frisket.ai.llm.router import ModelRouter
    from frisket.execution.commercial import (
        CommercialOffering,
        CommercialOfferingMatch,
        CommercialPresentation,
        CommercialQuoteRounding,
        CommercialQuoteTerms,
        CommercialSettlementTerms,
    )
    from frisket.execution.credential_use import CredentialUseContext
    from frisket.execution.definitions import StaticExecutionTargetProvider
    from frisket.execution.price_book import ByokZero, PlatformMetered, cost_posture_for
    from frisket.execution.provider import CompositionFacts, ExecutionComposition

    class HostedComposition(ExecutionComposition):
        # External hosts admit their LLM roster separately from fleet offerings.
        def resolution_targets(self):
            return self.provider.targets()

    def composition(project, router, _context):
        credential_context = CredentialUseContext(
            cost_posture=cost_posture_for(funding)
        )
        return HostedComposition(
            facts=CompositionFacts(
                edition="hosted", org_id="org-choices", funding=funding
            ),
            provider=StaticExecutionTargetProvider(
                env={},
                secrets=project,
                router=router,
                credential_use_context=credential_context,
            ),
            credential_use_context=credential_context,
            offerings=(
                CommercialOffering(
                    match=CommercialOfferingMatch(
                        target_id="remote-api:openai",
                        capability="classify",
                        engine="openai-decisions",
                    ),
                    quote=CommercialQuoteTerms(
                        pricing_key="decisions",
                        terms_version="v1",
                        unit_rate="0.000001",
                        quantity_unit="input_token",
                        rounding=CommercialQuoteRounding(),
                    ),
                    settlement=CommercialSettlementTerms(
                        meter_key="input_tokens",
                        meter_units_per_quantity_unit="1",
                        ceiling_mode="consented_quantity",
                        row_settlement_mode="all_metered",
                    ),
                    charge_authority="hosted",
                    presentation=CommercialPresentation(
                        venue_label="OpenAI Decisions",
                        billing_label="Cloud credits per input token",
                    ),
                ),
            ),
        )

    for source, funding in (
        ("platform_key", PlatformMetered()),
        ("org_byok", PlatformMetered()),
        ("org_byok", ByokZero()),
    ):
        # The other provider is always platform-funded: a global funding marker
        # alone cannot describe every entry in a mixed-provider selector.
        app = create_app(
            tmp_path / f"{source}-{type(funding).__name__}",
            serve_spa=False,
            router=ModelRouter(
                keys={"openai": "test-openai", "anthropic": "test-anthropic"},
                key_sources={"openai": source, "anthropic": "platform_key"},
                use_env_keys=False,
            ),
            execution_composition_factory=composition,
        )
        app.state.workspace.create("Choices", project_id="choices")
        with TestClient(app) as client:
            choices = {}
            for action, field in (("map.ask", "model"), ("map.classify", "engine")):
                response = client.post(
                    "/api/projects/choices/selector-choices",
                    json={
                        "schema_version": "frisket.selector_choices_query.v1",
                        "subject": {
                            "kind": "action",
                            "action_id": action,
                            "field": field,
                            "params": {},
                        },
                    },
                )
                assert response.status_code == 200, response.text
                choices[field] = {
                    choice["authored_selection"][field]: choice
                    for group in response.json()["groups"]
                    for choice in group["choices"]
                }
        models = choices["model"]
        openai = models["openai/gpt-5-mini"]["execution_route"]
        decisions = choices["engine"]["openai-decisions"]["execution_route"]
        if source == "platform_key":
            assert openai["kind"] == decisions["kind"] == "cloud_credits"
            assert openai["label"] == "Frisket Cloud"
            assert openai["billed_to"] == "Frisket Cloud credits"
            assert decisions["label"] == "OpenAI Decisions"
            assert decisions["billed_to"] == "Cloud credits per input token"
        else:
            assert openai["kind"] == decisions["kind"] == "local_key"
            assert openai["label"] == decisions["label"] == "Your OpenAI API key"
            assert openai["credential_label"] == "Organization key"
        assert openai["account_id"] is None
        assert openai["sends_data_to"] == "OpenAI"
        anthropic = next(
            choice for model, choice in models.items() if model.startswith("anthropic/")
        )
        assert anthropic["execution_route"]["kind"] == "cloud_credits"
        local = choices["engine"]["local_semantic"]["execution_route"]
        assert local["kind"] == "local_engine"
        assert local["billed_to"] is None

"""Classification routes carry egress, credential and token tariff facts."""

from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace

import pytest

from frisket.ai.external_pricing import CLOUDFLARE_CLEF_INPUT_TOKEN
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.price_book import (
    OperatorBorne,
    live_cost_fact,
    quote_classify,
    settle,
)
from frisket.execution.promise_compiler import OperatorBorneZeroCost, PricedCostBasis
from frisket.execution.provider import CompositionFacts
from frisket.execution.resolver import Refusal, ResolutionRequest, resolve
from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY, bind_fact_to_route
from frisket.execution.targets import CAPABILITY_CLASSIFY, CLOUDFLARE_CLEF_TARGET_ID


ACCOUNT_ID = "a" * 32


def _resolve(engine, **env):
    return resolve(
        ResolutionRequest(engine=engine, options={}, capability=CAPABILITY_CLASSIFY),
        StaticExecutionTargetProvider(env=env),
        CompositionFacts(),
    )


@pytest.mark.parametrize("engine", ["local_semantic", "gliclass", "jeff"])
def test_existing_classifiers_stay_local_and_free(engine):
    outcome = _resolve(engine)
    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "local"
    assert outcome.facts.egress_class == "none"
    assert isinstance(
        quote_classify(
            target_id="local",
            engine=engine,
            funding=OperatorBorne(),
            offering=None,
            input_tokens=None,
        ),
        OperatorBorneZeroCost,
    )


def test_flash_requires_its_gateway_and_never_falls_back_to_cloudflare():
    refused = _resolve(
        "clef-flash", CLOUDFLARE_ACCOUNT_ID=ACCOUNT_ID, CLOUDFLARE_API_TOKEN="secret"
    )
    assert isinstance(refused, Refusal)
    assert refused.family == "no_live_target"
    assert refused.target_id == "models-gateway"
    outcome = _resolve(
        "clef-flash",
        FRISKET_MODELS_URL="http://127.0.0.1:8091",
        FRISKET_MODELS_TOKEN="gateway-secret",
    )
    assert not isinstance(outcome, Refusal)
    assert outcome.target.id == "models-gateway"
    assert outcome.support.transport == "sidecar.classify"
    assert outcome.facts.egress_class == "operator_lan"


@pytest.mark.parametrize(
    "env", [{}, {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID}, {"CLOUDFLARE_API_TOKEN": "s"}]
)
def test_cloudflare_needs_both_account_and_token(env):
    outcome = _resolve("clef", **env)
    assert isinstance(outcome, Refusal)
    assert outcome.family == "no_live_target"
    assert "CLOUDFLARE_ACCOUNT_ID" in outcome.remedy
    assert "CLOUDFLARE_API_TOKEN" in outcome.remedy


def test_cloudflare_connection_preserves_project_credential_source():
    project = SimpleNamespace(secret_plaintext=lambda name: "project-secret")
    provider = StaticExecutionTargetProvider(
        env={"CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID}, secrets=project
    )
    connection = provider.connection(CLOUDFLARE_CLEF_TARGET_ID)
    assert connection is not None
    assert connection.base_url == (
        "https://api.cloudflare.com/client/v4/accounts/"
        + ACCOUNT_ID
        + "/ai/run/@cf/cloudflare/clef"
    )
    assert connection.token == "project-secret"
    assert connection.extra["credential_source"] == "project_key"
    assert "project-secret" not in repr(connection)


@pytest.mark.parametrize("account", ["../other", "user@evil.test", "x" * 32])
def test_cloudflare_account_cannot_change_endpoint(account):
    provider = StaticExecutionTargetProvider(
        env={"CLOUDFLARE_ACCOUNT_ID": account, "CLOUDFLARE_API_TOKEN": "secret"}
    )
    with pytest.raises(ValueError, match="CLOUDFLARE_ACCOUNT_ID"):
        provider.connection(CLOUDFLARE_CLEF_TARGET_ID)


def test_cloudflare_quote_live_fence_and_settlement_use_same_token_tariff():
    args = dict(
        target_id=CLOUDFLARE_CLEF_TARGET_ID,
        engine="clef",
        funding=OperatorBorne(),
        offering=None,
    )
    basis = quote_classify(**args, input_tokens=1_000_000)
    assert isinstance(basis, PricedCostBasis)
    assert basis.pricing_key == CLOUDFLARE_CLEF_INPUT_TOKEN
    assert basis.quantity_unit == "input_token"
    assert basis.meter_key == "input_tokens"
    assert basis.bound == Decimal("0.24")
    fact = live_cost_fact(**args, capability=CAPABILITY_CLASSIFY, hardware_class=None)
    assert fact["unit_rate"] == basis.unit_rate == "0.00000024"
    settled = settle(
        cost_basis={"kind": "priced", **asdict(basis)},
        metered_units=[{"input_tokens": 2_000_000}],
        price_card_version=None,
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert settled["charge_usd"] == "0.48"


@pytest.mark.parametrize(
    "engine,target,transport,operator,kind,cost_source",
    [
        (
            "clef",
            "cloudflare-clef",
            "cloudflare.clef",
            "cloudflare",
            "platform_api",
            "estimated",
        ),
        (
            "clef-flash",
            "models-gateway",
            "sidecar.classify",
            "self",
            "local_http",
            "free_local",
        ),
    ],
)
def test_classify_model_call_receipt_binds_to_route(
    engine, target, transport, operator, kind, cost_source
):
    route = SimpleNamespace(
        id="route-classify",
        engine=engine,
        operator=operator,
        credential_source="local",
        cost_posture="operator_borne",
        target_snapshot={
            "target_id": target,
            "capability": "classify",
            "transport": transport,
            "run_scoped": False,
        },
    )
    fact = bind_fact_to_route(
        route,
        {
            "engine": engine,
            "credential_source": "project_key",
            "cost_source": cost_source,
            "provider_cost_usd": 0.01,
        },
    )
    assert fact["provider_kind"] == kind
    assert fact["cost_source"] == cost_source
    assert (
        fact[ROUTE_OBSERVATION_KEY]["observed"]["provenance"]
        == "frisket.classify_binding.v1"
    )
    if engine == "clef":
        assert fact["provider"] == "cloudflare"
        assert fact["credential_source"] == "project_key"


def test_classify_quote_measures_selected_text_and_questions(tmp_path, monkeypatch):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.ai.llm import ModelRouter
    from frisket.contracts.clef import clef_questions, estimate_clef_input_tokens
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
    from frisket.engine.store import Project
    from frisket.execution.provider import (
        ExecutionCompositionContext,
        open_execution_composition,
    )
    from frisket.execution.resolve_for_action import resolve_for_action

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")
    project = Project.create(tmp_path / "classify.frisket", name="classify")
    try:
        sheet = project.add_sheet("Text")
        column = project.add_column(sheet, "body", type="text")
        row_ids = project.add_rows(
            sheet,
            [{"body": "hello"}, {"body": "  "}, {"body": "excluded " * 1000}],
            {"body": column},
        )
        fields = [{"name": "topic", "type": "category", "labels": ["a", "b"]}]
        plan = build_typed_map_rows_plan(
            project,
            BoundTypedActionRequest.bind(
                ACTION_REGISTRY.get("map.classify"),
                ActionRequest(
                    action_id="map.classify",
                    scope=SheetRows(sheet_id=sheet, row_ids=row_ids[:2]),
                    params={
                        "source": ["body"],
                        "engine": "clef",
                        "fields": fields,
                        "context": "News archive",
                    },
                    idempotency_key="classify-quote",
                ),
            ),
        )
        resolved = resolve_for_action(
            project,
            plan.spec_dict(),
            plan.program,
            composition=open_execution_composition(
                project, ModelRouter(), ExecutionCompositionContext.direct()
            ),
        )
        assert resolved is not None and not isinstance(resolved, Refusal)
        assert resolved.resolution.target.id == "cloudflare-clef"
        assert resolved.resolution.facts.egress_class == "third_party_api"
        basis = resolved.cost_basis
        assert isinstance(basis, PricedCostBasis)
        expected = estimate_clef_input_tokens(
            "hello", clef_questions(fields, "News archive")
        )
        assert int(basis.estimated_quantity) == expected
        claims = {
            p.field for p in resolved.promise_set.promises if p.audience == "user_claim"
        }
        assert claims >= {"cost", "egress_class"}
    finally:
        project.close()

"""Decisions uses existing classification contracts, admission and paid usage."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from frisket.actions.classify import ClassifyParams, classify_row
from frisket.actions.extract.llm import ExtractParams, extract_row
from frisket.actions.types import Row, RowError
from frisket.contracts.decisions import (
    decisions_questions,
    decisions_values,
    decisions_usage,
    estimate_decisions_tokens,
)
from frisket.engine.executor.decision_read import AdmittedDecisionClassifier
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.price_book import OperatorBorne, quote_classify
from frisket.execution.provider import CompositionFacts
from frisket.execution.resolver import ResolutionRequest, resolve
from frisket.ops.base import RecipeInvocationHalt

FIELDS = [
    {"name": "类型 / kind", "type": "category", "labels": ["邮件", "Court filing"]},
    {"name": "Relevant?", "type": "boolean"},
    {"name": "Priority", "type": "score"},
]


def params(**kwargs):
    return ClassifyParams(
        source=["body"], engine="openai-decisions", fields=FIELDS, **kwargs
    )


def reply(**kwargs):
    return {
        "model": "gpt-6-luna",
        "usage": {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300},
            "output_tokens": 100,
        },
        "answers": [
            {
                "name": "q0",
                "type": "choice",
                "choice": "o1",
                "confidence": 0.9,
                "probabilities": [
                    {"value": "o0", "probability": 0.4},
                    {"value": "o1", "probability": 0.6},
                ],
            },
            {"name": "q1", "type": "predicate", "probability": 0.2},
            {"name": "q2", "type": "choice", "choice": "o7", "confidence": 0.8},
        ],
        **kwargs,
    }


def resolved():
    return resolve(
        ResolutionRequest(engine="openai-decisions", capability="classify"),
        StaticExecutionTargetProvider(env={"OPENAI_API_KEY": "test-secret"}),
        CompositionFacts(),
    )


async def invoke(monkeypatch, handler, *, resolution=None, extract=False):
    monkeypatch.setattr(
        "frisket.engine.executor.decision_read.preview_resolution_in_scope",
        lambda _: resolution or resolved(),
    )
    owner = AdmittedDecisionClassifier(
        engine="openai-decisions", context="Investigation"
    )
    row = Row({"body": "Example court filing"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ctx = SimpleNamespace(
            http=client, extras={}, credential_use_context=CredentialUseContext.open()
        )
        bound = owner.bind_row(row, row_id=1, ctx=ctx)
        if extract:
            p = ExtractParams(
                source=["body"],
                engine="openai-decisions",
                fields=FIELDS,
                grounding={"enabled": False},
            )
            result = await extract_row(p, row, bound)
        else:
            result = await classify_row(params(include_confidence=True), row, bound)
    return result, owner.accounting_by_row[1]


def test_existing_fields_project_to_named_decisions_without_losing_labels():
    p = params()
    questions = decisions_questions([f.model_dump() for f in p.fields], "Context")
    assert [q["name"] for q in questions] == ["q0", "q1", "q2"]
    assert questions[0]["choices"][0] == {"value": "o0", "description": "邮件"}
    assert questions[1]["type"] == "predicate"
    assert questions[2]["type"] == "choice"
    assert len(questions[2]["choices"]) == 11
    assert "Context" in questions[0]["instructions"]
    assert decisions_values(reply()["answers"], FIELDS) == {
        "类型 / kind": ("Court filing", 0.9),
        "Relevant?": (False, 0.8),
        "Priority": (7, 0.8),
    }
    assert estimate_decisions_tokens("x", questions) > estimate_decisions_tokens(
        "x", []
    )


def test_field_limits_and_unsupported_features_are_validated_before_dispatch():
    for kind in ["text", "integer", "number"]:
        with pytest.raises(ValidationError):
            ClassifyParams(
                source=["body"],
                engine="openai-decisions",
                fields=[{"name": "x", "type": kind}],
            )
    with pytest.raises(ValidationError):
        params(include_justification=True)
    with pytest.raises(ValidationError, match="citations"):
        ExtractParams(source=["body"], engine="openai-decisions", fields=FIELDS)
    ClassifyParams(
        source=["body"],
        engine="openai-decisions",
        fields=[
            {"name": "x", "type": "category", "labels": [str(i) for i in range(255)]}
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extract", [False, True])
async def test_transport_field_refusal_and_actual_paid_usage(monkeypatch, extract):
    def respond(request):
        assert str(request.url) == "https://api.openai.com/v1/decisions"
        assert request.headers["authorization"] == "Bearer test-secret"
        wire = json.loads(request.content)
        assert wire["model"] == "gpt-6-luna"
        assert wire["input"] == "Example court filing"
        assert wire["questions"][1]["type"] == "predicate"
        body = reply()
        body["answers"][1] = {"name": "q1", "type": "refusal"}
        return httpx.Response(200, json=body)

    result, accounting = await invoke(monkeypatch, respond, extract=extract)
    assert result.output.root["类型 / kind"].value == "Court filing"
    assert result.output.root["Relevant?"].code == "classify_refused"
    assert result.output.root["Priority"].value == 7
    assert accounting["cost"] == pytest.approx(0.0000895)
    fact = accounting["model_calls"][0]
    assert fact["provider"] == "openai"
    assert fact["units"] == {
        "requests": 1,
        "input_tokens": 1000,
        "cached_tokens": 200,
        "cache_write_tokens": 300,
    }
    assert fact["model_ids"] == ["openai/gpt-6-luna"]


@pytest.mark.parametrize("tokens,multiplier", [(272000, 1), (272001, 2)])
def test_long_context_and_cache_cost_not_double_counted(tokens, multiplier):
    units, cost = decisions_usage(
        {
            "input_tokens": tokens,
            "input_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 1000},
        },
        0.0000001,
    )
    assert units["input_tokens"] == tokens
    assert cost == pytest.approx((tokens - 2000 + 100 + 1250) * 0.0000001 * multiplier)
    assert decisions_usage(
        {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 8},
        },
        0.0000001,
    ) == ({}, None)


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a.pop(),
        lambda a: a[0].update(name="q1"),
        lambda a: a[0].update(choice="unrequested"),
        lambda a: a[0].update(confidence=float("nan")),
        lambda a: a[1].update(probability=True),
        lambda a: a[2].update(type="score", score=7.5),
    ],
)
def test_malformed_provider_answers_are_not_published(change):
    answers = reply()["answers"]
    change(answers)
    with pytest.raises(ValueError):
        decisions_values(answers, FIELDS)


@pytest.mark.asyncio
async def test_unknown_usage_stays_unknown_and_errors_do_not_leak(monkeypatch):
    _, accounting = await invoke(
        monkeypatch, lambda _: httpx.Response(200, json=reply(usage={}))
    )
    assert accounting["cost"] is None
    with pytest.raises(RowError) as raised:
        await invoke(
            monkeypatch,
            lambda _: httpx.Response(401, text="test-secret private document"),
        )
    assert "401" in str(raised.value)
    assert "test-secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_mismatched_route_refuses_before_network(monkeypatch):
    resolution = resolved()
    resolution = replace(
        resolution, support=replace(resolution.support, transport="cloudflare.clef")
    )
    with pytest.raises(RecipeInvocationHalt, match="admitted engine"):
        await invoke(
            monkeypatch, lambda _: pytest.fail("must not call"), resolution=resolution
        )


def test_quote_uses_provider_usage_not_flat_token_settlement():
    quote = quote_classify(
        target_id="remote-api:openai",
        engine="openai-decisions",
        funding=OperatorBorne(),
        offering=None,
        input_tokens=1250,
    )
    assert quote.pricing_key == "classify.openai.decisions.input_token"
    assert quote.unit_rate == "0.0000001"
    assert quote.charge_authority == "provider_usage"


@pytest.mark.parametrize("action_id", ["map.classify", "map.extract"])
def test_action_confirms_executes_records_usage_and_replays(
    tmp_path, monkeypatch, action_id
):
    import asyncio
    from frisket.ai.llm import ModelRouter
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store import Project
    from frisket.engine.store.runs import RunResultStore

    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=reply())

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(ModelRouter, "client", property(lambda _: client))
    project = Project.create(tmp_path / "decisions.frisket")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body", type="text")
        row_id = project.add_rows(sheet, [{"body": "Court filing"}], {"body": column})[
            0
        ]
        parameters = params(include_confidence=True).model_dump(mode="json")
        if action_id == "map.extract":
            parameters = ExtractParams(
                source=["body"],
                engine="openai-decisions",
                fields=FIELDS,
                grounding=None,
                instruction="Only from provided content.",
                include_confidence=True,
            ).model_dump(mode="json")
        request = {
            "action_id": action_id,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": parameters,
            "idempotency_key": "decisions-action",
        }
        result = run_action_spec(project, request, project_id="decisions")
        assert result.status == "needs_confirmation", result.errors
        assert calls == []
        request["confirmation"] = result.errors[0].details["promise_set_hash"]
        result = run_action_spec(project, request, project_id="decisions")
        assert result.status == "completed", result.errors
        assert len(calls) == 1
        if action_id == "map.extract":
            assert (
                "Only from provided content."
                in calls[0]["questions"][0]["instructions"]
            )
        columns = {c["name"]: c["id"] for c in project.columns(sheet)}
        assert (
            project.get_values(sheet, columns["Priority"], row_ids=[row_id])[row_id]
            == 7
        )
        facts = RunResultStore(project).model_calls(result.run_id)
        assert facts[0]["provider_cost_usd"] == pytest.approx(0.0000895)
        assert facts[0]["engine"] == "openai-decisions"
        assert (
            run_action_spec(project, request, project_id="decisions").status
            == "completed"
        )
        assert len(calls) == 1
    finally:
        project.close()
        asyncio.run(client.aclose())


@pytest.mark.parametrize("action_id", ["map.classify", "map.extract"])
def test_catalog_uses_existing_openai_setup_and_field_controls(monkeypatch, action_id):
    from frisket.server.action_catalog_hints import _recipe_engines

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def engine():
        return next(
            e for e in _recipe_engines(action_id, {}) if e["id"] == "openai-decisions"
        )

    assert engine()["available"] is False
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    row = engine()
    assert row["available"] is True
    assert row["billable"] is True
    assert row["classification_options"]["max_labels"] == 255
    assert row["classification_options"]["include_confidence"] is True
    assert row["classification_options"]["include_justification"] is False
    assert "Text only" in row["description"]


@pytest.mark.asyncio
async def test_unconsented_credential_refuses_before_openai_http(monkeypatch):
    resolution = resolved()
    resolution = replace(
        resolution, facts=replace(resolution.facts, cost_posture="org_key")
    )
    with pytest.raises(RecipeInvocationHalt, match="credential"):
        await invoke(
            monkeypatch,
            lambda _: pytest.fail("must not call with operator key"),
            resolution=resolution,
        )


def test_remote_provider_connection_keeps_selected_key_ownership():
    provider = StaticExecutionTargetProvider(
        env={"OPENAI_API_KEY": "env-test-secret"},
        router=SimpleNamespace(
            configured_keys=lambda: {"openai": "org-test-secret"},
            credential_source_for=lambda _: "org_byok",
        ),
    )
    connection = provider.connection("remote-api:openai")
    assert connection.token == "org-test-secret"
    assert connection.extra["credential_source"] == "org_byok"

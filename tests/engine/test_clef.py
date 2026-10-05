"""Clef field projection, strict decision decoding and admitted HTTP boundary."""

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from frisket.actions.classify import ClassifyParams, classify_row
from frisket.actions.types import Row, RowError
from frisket.contracts.clef import (
    clef_questions,
    clef_values,
    estimate_clef_input_tokens,
)
from frisket.engine.executor.clef_read import AdmittedClefClassifier
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.provider import CompositionFacts
from frisket.execution.resolver import ResolutionRequest, resolve
from frisket.ops.base import RecipeInvocationHalt


FIELDS = [
    {
        "name": "Document kind",
        "type": "category",
        "labels": ["Email / correspondence", "Court filing"],
    },
    {"name": "Relevant", "type": "boolean"},
    {"name": "Priority", "type": "score"},
]


def params(engine="clef", **kwargs):
    return ClassifyParams(source=["body"], engine=engine, fields=FIELDS, **kwargs)


def reply(engine="clef", **kwargs):
    return {
        "model": engine,
        "usage": {"input_tokens": 1000, "output_tokens": 0},
        "answers": {
            "q0": {
                "type": "choice",
                "choice": "o1",
                "confidence": 0.8,
                "probabilities": {"o0": 0.2, "o1": 0.8},
            },
            "q1": {"type": "noul", "noul": 0.9},
            "q2": {
                "type": "choice",
                "choice": "o7",
                "confidence": 1.0,
                "probabilities": {f"o{i}": float(i == 7) for i in range(11)},
            },
        },
        **kwargs,
    }


def test_projection_keeps_user_names_out_of_provider_ids():
    p = params(context="Investigative archive", include_confidence=True)
    questions = clef_questions([f.model_dump() for f in p.fields], p.context)
    assert list(questions) == ["q0", "q1", "q2"]
    assert questions["q0"]["criteria"]["o0"] == "Email / correspondence"
    assert "Investigative archive" in questions["q0"]["instructions"]
    assert questions["q1"]["type"] == "noul"
    assert questions["q2"]["criteria"]["o10"] == "10"
    assert estimate_clef_input_tokens("body", questions) > estimate_clef_input_tokens(
        "body", {}
    )
    assert clef_values(reply()["answers"], FIELDS) == {
        "Document kind": ("Court filing", 0.8),
        "Relevant": (True, 0.9),
        "Priority": (7, 1),
    }


@pytest.mark.parametrize("engine", ["clef", "clef-flash"])
def test_unsupported_fields_and_explanations_refuse_before_execution(engine):
    with pytest.raises(ValidationError, match="justifications"):
        params(engine, include_justification=True)
    for kind in ("text", "number", "integer"):
        with pytest.raises(ValidationError, match="category, boolean, and score"):
            ClassifyParams(
                source=["body"], engine=engine, fields=[{"name": "x", "type": kind}]
            )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 1.01, True, "0.9"])
def test_boolean_rejects_invalid_probabilities(bad):
    answers = reply()["answers"]
    answers["q1"]["noul"] = bad
    with pytest.raises(ValueError, match="probability"):
        clef_values(answers, FIELDS)


@pytest.mark.parametrize(
    "edit",
    [
        lambda a: a.pop("q1"),
        lambda a: a["q0"].update(choice="not-an-option"),
        lambda a: a["q0"].update(choice="o0"),
        lambda a: a["q0"].update(probabilities={"o0": 0.5}),
        lambda a: a["q0"].update(probabilities={"o0": 0.1, "o1": 0.1}),
    ],
)
def test_unusable_responses_are_not_published(edit):
    answers = reply()["answers"]
    edit(answers)
    with pytest.raises(ValueError):
        clef_values(answers, FIELDS)


def test_probability_rounding_for_many_labels_is_supported():
    fields = [
        {"name": "kind", "type": "category", "labels": list(map(str, range(254)))}
    ]
    answers = {
        "q0": {
            "type": "choice",
            "choice": "o0",
            "probabilities": {f"o{i}": 0.0039 for i in range(254)},
        }
    }
    assert clef_values(answers, fields)["kind"] == ("0", 0.0039)


def resolved(engine):
    return resolve(
        ResolutionRequest(engine=engine, capability="classify"),
        StaticExecutionTargetProvider(
            env={
                "CLOUDFLARE_ACCOUNT_ID": "a" * 32,
                "CLOUDFLARE_API_TOKEN": "test-secret",
                "FRISKET_MODELS_URL": "http://models.test",
                "FRISKET_MODELS_TOKEN": "sidecar-secret",
            }
        ),
        CompositionFacts(),
    )


async def invoke(monkeypatch, handler, *, engine="clef", resolution=None):
    # Supply a resolved preview boundary directly to isolate the HTTP adapter.
    # End-to-end admission/confirmation is covered by the executor test below.
    resolution = resolution or resolved(engine)
    monkeypatch.setattr(
        "frisket.engine.executor.clef_read.preview_resolution_in_scope",
        lambda extras: resolution,
    )
    owner = AdmittedClefClassifier(engine=engine)
    row = Row({"body": "Example court filing"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ctx = SimpleNamespace(
            http=client, extras={}, credential_use_context=CredentialUseContext.open()
        )
        bound = owner.bind_row(row, row_id=1, ctx=ctx)
        result = await classify_row(params(engine, include_confidence=True), row, bound)
    return result, owner.accounting_by_row[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["clef", "clef-flash"])
async def test_api_and_sidecar_use_same_decisions_and_correct_accounting(
    monkeypatch, engine
):
    import json

    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["questions"]["q1"]["type"] == "noul"
        assert request.headers["Authorization"] == (
            "Bearer test-secret" if engine == "clef" else "Bearer sidecar-secret"
        )
        assert body["state" if engine == "clef" else "text"] == "Example court filing"
        return httpx.Response(
            200,
            json={"success": True, "result": reply()}
            if engine == "clef"
            else reply(engine),
        )

    result, accounting = await invoke(monkeypatch, respond, engine=engine)
    assert len(requests) == 1
    assert result.output.root["Priority"].value == 7
    assert accounting["cost"] == pytest.approx(0.00024 if engine == "clef" else 0)
    fact = accounting["model_calls"][0]
    assert fact["units"]["input_tokens"] == 1000
    assert fact["capability"] == "classify"


@pytest.mark.asyncio
async def test_missing_usage_does_not_fabricate_zero_cost(monkeypatch):
    _, accounting = await invoke(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"success": True, "result": reply(usage={})}
        ),
    )
    assert accounting["cost"] is None
    assert accounting["cost_source"] == "unknown"


@pytest.mark.asyncio
async def test_error_body_never_leaks_provider_secrets(monkeypatch):
    with pytest.raises(RowError) as caught:
        await invoke(
            monkeypatch,
            lambda request: httpx.Response(401, text="test-secret private document"),
        )
    assert "401" in caught.value.message
    assert "test-secret" not in caught.value.message


@pytest.mark.asyncio
async def test_wrong_credential_posture_refuses_before_http(monkeypatch):
    resolution = resolved("clef")
    resolution = replace(
        resolution, facts=replace(resolution.facts, cost_posture="org_key")
    )

    def unexpected(request):
        pytest.fail("must not send data under unconsented credential")

    with pytest.raises(RecipeInvocationHalt, match="credential"):
        await invoke(monkeypatch, unexpected, resolution=resolution)


@pytest.mark.parametrize("engine", ["clef", "clef-flash"])
def test_executor_confirms_executes_and_replays_without_another_call(
    tmp_path, monkeypatch, engine
):
    import asyncio

    from frisket.ai.llm import ModelRouter
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store import Project
    from frisket.engine.store.runs import RunResultStore

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-secret")
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sidecar-secret")
    calls = []

    def respond(request):
        calls.append(request)
        body = reply(engine)
        return httpx.Response(
            200, json={"success": True, "result": body} if engine == "clef" else body
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(ModelRouter, "client", property(lambda self: client))
    project = Project.create(tmp_path / "clef.frisket", name="Clef")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body", type="text")
        project.add_rows(sheet, [{"body": "Example court filing"}], {"body": column})
        request = {
            "action_id": "map.classify",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": params(engine, include_confidence=True).model_dump(mode="json"),
            "idempotency_key": "clef-executor",
        }
        result = run_action_spec(project, request, project_id="clef")
        if engine == "clef":
            assert result.status == "needs_confirmation", result.errors
            assert calls == []
            request["confirmation"] = result.errors[0].details["promise_set_hash"]
            result = run_action_spec(project, request, project_id="clef")
        assert result.status == "completed", result.errors
        assert len(calls) == 1
        facts = RunResultStore(project).model_calls(result.run_id)
        assert len(facts) == 1
        assert facts[0]["capability"] == "classify"
        assert facts[0]["provider_cost_usd"] == pytest.approx(
            0.00024 if engine == "clef" else 0
        )
        replay = run_action_spec(project, request, project_id="clef")
        assert replay.status == "completed", replay.errors
        assert len(calls) == 1
    finally:
        project.close()
        asyncio.run(client.aclose())

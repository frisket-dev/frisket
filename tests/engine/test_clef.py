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


async def invoke(
    monkeypatch, handler, *, engine="clef", resolution=None, text="Example court filing"
):
    # Supply a resolved preview boundary directly to isolate the HTTP adapter.
    # End-to-end admission/confirmation is covered by the executor test below.
    resolution = resolution or resolved(engine)
    monkeypatch.setattr(
        "frisket.engine.executor.clef_read.preview_resolution_in_scope",
        lambda extras: resolution,
    )
    owner = AdmittedClefClassifier(engine=engine)
    row = Row({"body": text})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ctx = SimpleNamespace(
            http=client, extras={}, credential_use_context=CredentialUseContext.open()
        )
        bound = owner.bind_row(row, row_id=1, ctx=ctx)
        result = await classify_row(params(engine, include_confidence=True), row, bound)
    return result, owner.accounting_by_row[1]


@pytest.mark.asyncio
async def test_oversized_flash_input_refuses_before_http(monkeypatch):
    from frisket.contracts.clef import CLEF_FLASH_MAX_TEXT_CHARS

    with pytest.raises(RowError, match="input exceeds") as raised:
        await invoke(
            monkeypatch,
            lambda request: pytest.fail("must not send oversized input"),
            engine="clef-flash",
            text="x" * (CLEF_FLASH_MAX_TEXT_CHARS + 1),
        )
    assert raised.value.code == "classify_input_invalid"


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
@pytest.mark.parametrize("action_id", ["map.classify", "map.extract"])
def test_executor_confirms_executes_and_replays_without_another_call(
    tmp_path, monkeypatch, engine, action_id
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
        body["answers"]["q1"]["noul"] = 0.1
        body["answers"]["q2"].update(
            choice="o0", probabilities={f"o{i}": float(i == 0) for i in range(11)}
        )
        return httpx.Response(
            200, json={"success": True, "result": body} if engine == "clef" else body
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(ModelRouter, "client", property(lambda self: client))
    project = Project.create(tmp_path / "clef.frisket", name="Clef")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body", type="text")
        row = project.add_rows(
            sheet, [{"body": "Example court filing"}], {"body": column}
        )[0]
        parameters = params(engine, include_confidence=True).model_dump(mode="json")
        if action_id == "map.extract":
            from frisket.actions.extract import ExtractParams

            parameters = ExtractParams(
                source=["body"],
                engine=engine,
                fields=[{**f, "required": True} for f in FIELDS],
                grounding=None,
                include_confidence=True,
                instruction="Only use explicit document content.",
                context="Archive",
            ).model_dump(mode="json")
        request = {
            "action_id": action_id,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": parameters,
            "output_names": {"Document kind": "Kind"},
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
        if action_id == "map.extract":
            import json

            sent = json.loads(calls[0].content)
            assert (
                "Only use explicit document content."
                in sent["questions"]["q0"]["instructions"]
            )
            assert "Archive" in sent["questions"]["q0"]["instructions"]
        columns = {c["name"]: c["id"] for c in project.columns(sheet)}
        for name, expected in {
            "Kind": "Court filing",
            "Relevant": False,
            "Priority": 0,
            "Document kind_confidence": 0.8,
        }.items():
            assert (
                project.get_values(sheet, columns[name], row_ids=[row])[row] == expected
            )
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


@pytest.mark.parametrize("action_id", ["map.classify", "map.extract"])
@pytest.mark.parametrize("template", [False, True])
def test_decision_image_sources_reject_before_provider_call(
    tmp_path, monkeypatch, action_id, template
):
    from frisket.ai.llm import ModelRouter
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store import Project

    monkeypatch.setattr(
        ModelRouter,
        "client",
        property(lambda self: pytest.fail("must not call provider")),
    )
    project = Project.create(tmp_path / "images.frisket")
    try:
        sheet = project.add_sheet("Images")
        project.add_column(sheet, "image", type="image")
        parameters = {"engine": "clef", "source": ["image"], "fields": FIELDS}
        if template:
            parameters["source"] = {"text": "Read {{image}}"}
        if action_id == "map.extract":
            parameters["grounding"] = None
        result = run_action_spec(
            project,
            {
                "action_id": action_id,
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": parameters,
                "idempotency_key": "image-reject",
            },
            project_id="images",
        )
        assert result.status == "failed"
        assert any("Clef accepts text" in error.message for error in result.errors), (
            result.errors
        )
    finally:
        project.close()


def test_paid_extract_preview_uses_one_clef_call_without_publishing(
    tmp_path, monkeypatch
):
    import asyncio
    import json

    from frisket.ai.llm import ModelRouter
    from frisket.engine.executor import resolve_map_preview
    from frisket.engine.store import Project
    from tests.preview.test_accounted_action_preview import _runner, _admit, _outputs

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-secret")
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "result": reply()})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(ModelRouter, "client", property(lambda self: client))
    project = Project.create(tmp_path / "preview.frisket")
    try:
        sheet = project.add_sheet("Text")
        column = project.add_column(sheet, "body", type="text")
        project.add_rows(sheet, [{"body": "Archive evidence"}], {"body": column})
        request = {
            "action_id": "map.extract",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": ["body"],
                "engine": "clef",
                "fields": FIELDS,
                "grounding": None,
                "instruction": "Decide from archive.",
            },
            "idempotency_key": "clef-preview",
        }
        plan = resolve_map_preview(project, request)
        runner = _runner(project, None)
        before = _outputs(project)
        spec, attempt = _admit(runner, plan)
        result = asyncio.run(
            runner.preview(spec, program=plan.program, attempt=attempt)
        )
        assert len(calls) == 1
        assert "Decide from archive." in calls[0]["questions"]["q0"]["instructions"]
        assert result.values
        assert all(
            cells["Document kind"]["value"] == "Court filing"
            for cells in result.values.values()
        )
        assert _outputs(project) == before
        facts = runner.run_store.model_calls()
        assert len(facts) == 1 and facts[0]["run_id"] is None
        assert facts[0]["provider_cost_usd"] == pytest.approx(0.00024)
    finally:
        project.close()
        asyncio.run(client.aclose())

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, Field

from frisket.actions.core import (
    ActionCategory,
    ModelRows,
    OutputField,
    RegisteredAction,
    action,
    model_rows,
)
from frisket.actions.model_rows import RichSource
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicOutput,
    ModelPrompt,
    ModelRef,
    Outcome,
    Row,
    RowError,
    RowResult,
)
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner.row_inputs import row_values
from frisket.engine.store import Project
from frisket.ops.base import OpContext


class SelectedParams(ActionParams):
    document: RichSource
    chosen_model: ModelRef | None = None


class WireAnswer(BaseModel):
    count: int = Field(ge=0, le=3)


class PublishedAnswer(BaseModel):
    answer: Outcome[str]


def prompt(params: SelectedParams, row: Row) -> ModelPrompt[WireAnswer]:
    return ModelPrompt(
        messages=({"role": "user", "content": str(row.values)},), max_tokens=71
    )


def complete(
    params: SelectedParams, row: Row, response: WireAnswer
) -> RowResult[PublishedAnswer]:
    return RowResult(
        output=PublishedAnswer(
            answer=Outcome.ok(
                f"{row.values['input']}: {response.count}", confidence=0.8
            )
        )
    )


async def direct(params: SelectedParams, row: Row) -> RowResult[PublishedAnswer]:
    return RowResult(output=PublishedAnswer(answer=Outcome.ok(row.values["input"])))


def declaration(renderer=prompt, **kwargs):
    return RegisteredAction(
        "map.seam",
        action(
            name="seam",
            title="Seam",
            description="One row request.",
            category=ActionCategory.TEXT,
            run=model_rows(renderer, **kwargs),
        ),
    )


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "source.frisket")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "body", type="text")
    rows = project.add_rows(sheet, [{"body": "Ada"}], {"body": column})
    try:
        yield project, sheet, column, rows[0]
    finally:
        project.close()


def plan_for(source, registered, *, model=None):
    project, sheet, _, _ = source
    params = {"document": {"text": "Person: {{body}}"}, "chosen_model": model}
    request = ActionRequest(
        action_id="map.seam",
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        params=params,
        idempotency_key="seam@1",
    )
    bound = BoundTypedActionRequest.bind(registered, request)
    return build_typed_map_rows_plan(project, bound)


def test_model_selection_uses_typed_value_and_direct_keeps_template(source):
    registered = declaration(direct=direct, complete=complete)
    assert (
        registered.catalog_entry()["ui_hints"]["semantic_controls"]["chosen_model"]
        == "model"
    )
    direct_plan = plan_for(source, registered)
    model_plan = plan_for(source, registered, model="anthropic/claude-haiku-4-5")
    assert isinstance(registered.definition.run, ModelRows)
    assert not isinstance(direct_plan.action.definition.run, ModelRows)
    assert isinstance(model_plan.action.definition.run, ModelRows)
    assert "model" not in direct_plan.spec
    assert model_plan.spec["model"] == "anthropic/claude-haiku-4-5"
    assert direct_plan.spec["input_template"] == model_plan.spec["input_template"]
    project, _, column, row_id = source
    values = row_values(
        project, direct_plan.program, direct_plan.spec_dict(), {"body": column}, row_id
    )
    result = asyncio.run(
        direct_plan.program.execute(
            values, direct_plan.spec_dict(), OpContext(project=project)
        )
    )
    assert result.cells["answer"]["value"] == "Person: Ada"


def test_completion_gets_validated_response_and_admitted_row(source):
    plan = plan_for(
        source, declaration(complete=complete), model="anthropic/claude-haiku-4-5"
    )
    values = {"input": "Person: Ada"}
    request = plan.program.render(values, plan.spec_dict())
    assert request.max_tokens == 71
    assert request.schema["properties"]["count"]["maximum"] == 3
    publication = plan.program.normalize_model_output(
        {"count": 2}, plan.spec_dict(), row_values=values
    )
    assert publication.cells["answer"] == {"value": "Person: Ada: 2", "confidence": 0.8}
    with pytest.raises(ValueError):
        plan.program.normalize_model_output(
            {"count": 4}, plan.spec_dict(), row_values=values
        )


@pytest.mark.parametrize("engine", ["llm", "local_semantic"])
@pytest.mark.parametrize("template", [True, False])
def test_classification_consumes_only_the_host_selected_source(
    source, engine, template
):
    from frisket.actions.classify import CLASSIFY, ClassifyParams, classify_row

    project, sheet, column, row_id = source
    other = project.add_column(sheet, "unselected", type="text")
    row_id = project.add_rows(
        sheet,
        [{"body": "Ada", "unselected": "private, never send"}],
        {"body": column, "unselected": other},
    )[0]
    params = ClassifyParams(
        source={"text": "Person: {{body}}"} if template else ["body"],
        engine=engine,
        model="anthropic/claude-haiku-4-5" if engine == "llm" else None,
        fields=[{"name": "topic", "labels": ["person", "other"]}],
    )
    request = ActionRequest(
        action_id="map.classify",
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        params=params.model_dump(mode="json"),
        idempotency_key="classify-template",
    )
    plan = build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(
            RegisteredAction("map.classify", CLASSIFY), request
        ),
    )
    values = row_values(
        project,
        plan.program,
        plan.spec_dict(),
        {"body": column, "unselected": other},
        row_id,
    )
    assert values == ({"input": "Person: Ada"} if template else {"body": "Ada"})
    if engine == "llm":
        prompt = plan.program.render(values, plan.spec_dict())
        assert prompt.messages[1]["content"][0]["text"] == (
            "input: Person: Ada" if template else "body: Ada"
        )
    else:

        class Classifier:
            async def classify(self, row, text, fields):
                assert text == ("Person: Ada" if template else "Ada")
                return {"topic": Outcome.ok("person")}

        result = asyncio.run(classify_row(params, Row(values), Classifier()))
        assert result.output.root["topic"].value == "person"


def dynamic_prompt(params: SelectedParams, row: Row) -> ModelPrompt[DynamicOutput]:
    return ModelPrompt(
        messages=({"role": "user", "content": "Return a score."},),
        response_schema={
            "type": "object",
            "properties": {"score": {"type": "integer", "maximum": 3}},
            "required": ["score"],
            "additionalProperties": False,
        },
        max_tokens=31,
    )


def dynamic_complete(
    params: SelectedParams, row: Row, response: DynamicOutput
) -> RowResult[DynamicOutput]:
    return RowResult(
        output=DynamicOutput({"answer": Outcome.ok(response.root["score"])})
    )


def dynamic_fields(params: SelectedParams) -> dict[str, Any]:
    return {
        "answer": OutputField(
            "answer",
            "integer",
            {"type": "integer", "minimum": 1, "maximum": 3},
            Outcome[int | None],
        )
    }


def test_dynamic_response_schema_is_distinct_from_frozen_publication_schema(source):
    plan = plan_for(
        source,
        declaration(
            dynamic_prompt, complete=dynamic_complete, dynamic_outputs=dynamic_fields
        ),
        model="anthropic/claude-haiku-4-5",
    )
    request = plan.program.render({"input": "Ada"}, plan.spec_dict())
    assert set(request.schema["properties"]) == {"score"}
    assert request.max_tokens == 31
    assert [field["name"] for field in plan.output_fields] == ["answer"]
    publication = plan.program.normalize_model_output(
        {"score": 2}, plan.spec_dict(), row_values={"input": "Ada"}
    )
    assert publication.cells["answer"]["value"] == 2
    with pytest.raises(RowError):
        plan.program.normalize_model_output(
            {"score": 0}, plan.spec_dict(), row_values={"input": "Ada"}
        )


def test_model_rows_rejects_async_completion_and_missing_model():
    async def asynchronous(
        params: SelectedParams, row: Row, response: WireAnswer
    ) -> RowResult[PublishedAnswer]:
        return complete(params, row, response)

    with pytest.raises(TypeError, match="synchronous"):
        declaration(complete=asynchronous)
    registered = declaration(complete=complete)
    request = ActionRequest(
        action_id="map.seam",
        scope={"kind": "sheet_rows", "sheet_id": 1},
        params={"document": ["body"]},
        idempotency_key="seam@1",
    )
    with pytest.raises(ValueError, match="model must be selected"):
        BoundTypedActionRequest.bind(registered, request)


def test_dynamic_request_is_shared_by_estimate_cache_and_execution(source, tmp_path):
    from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter, ResponseCache
    from frisket.ai.llm.cache import request_key
    from frisket.engine.runner import MapRunner
    from frisket.engine.runner.row_execution import AdaptiveThrottle, llm_row
    from frisket.execution.attempt_authority import UnroutedOnlyAuthority

    project, _, column, row_id = source
    plan = plan_for(
        source,
        declaration(
            dynamic_prompt, complete=dynamic_complete, dynamic_outputs=dynamic_fields
        ),
        model="anthropic/claude-haiku-4-5",
    )
    spec = plan.spec_dict()
    values = row_values(project, plan.program, spec, {"body": column}, row_id)
    rendered = plan.program.render(values, spec)
    cache = ResponseCache(tmp_path / "responses.db")
    router = ModelRouter(cache=cache, cache_mode="replay_strict", use_env_keys=False)
    try:
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        assert isinstance(
            runner.estimate(spec, program=plan.program)["cost"], (int, float)
        )
        assert not runner.exact_replay_available(spec, program=plan.program)
        request = LLMRequest(
            model=spec["model"],
            messages=rendered.messages,
            schema=rendered.schema,
            max_tokens=rendered.max_tokens,
        )
        cache.put(
            request_key(request, plan.program.version),
            LLMResponse(
                content='{"score":2}',
                data={"score": 2},
                tokens_in=7,
                tokens_out=3,
                cost=0.0,
                model=spec["model"],
            ),
        )
        assert runner.exact_replay_available(spec, program=plan.program)
        data, facts = asyncio.run(
            llm_row(router, AdaptiveThrottle(), plan.program, values, spec)
        )
        assert data == {"score": 2}
        assert facts["cost"] == 0
        assert len(facts["model_calls"]) == 1
    finally:
        cache.close()


@pytest.mark.parametrize("key", ["$ref", "$dynamicRef"])
def test_prompt_schema_cannot_fetch_remote_references(key):
    with pytest.raises(ValueError, match="references must be local"):
        ModelPrompt(
            messages=({"role": "user", "content": "x"},),
            response_schema={
                "type": "object",
                "properties": {"data": {key: "https://example.invalid/schema"}},
            },
        )

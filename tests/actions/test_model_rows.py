from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    RootModel,
    ValidationError,
    computed_field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from frisket.actions.core import ActionCategory, RegisteredAction, action, model_rows
from frisket.actions.model_rows import (
    ASK,
    SUMMARIZE,
    AskParams,
    SummarizeParams,
    ask,
    summarize,
)
from frisket.actions.types import (
    GeoPoint,
    ModelPrompt,
    Outcome,
    Row,
    SheetRows,
)
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project


def test_model_backed_catalog_is_derived_from_model_rows_contract() -> None:
    for declaration, output in ((ASK, "answer"), (SUMMARIZE, "summary")):
        assert declaration.run.params_model.model_json_schema()["properties"]["source"][
            "anyOf"
        ]
        assert declaration.run.model_param == "model"
        assert [field.key for field in declaration.run.output_fields] == [output]


def test_model_params_require_one_unambiguous_unique_source() -> None:
    common = {"model": "anthropic/claude-haiku-4-5", "question": "What?"}
    with pytest.raises(ValidationError):
        AskParams.model_validate({**common, "source": []})
    with pytest.raises(ValidationError):
        AskParams.model_validate({**common, "source": ["body", "body"]})
    with pytest.raises(ValidationError):
        AskParams.model_validate({**common, "source": {"text": "literal only"}})
    assert [item.name for item in AskParams(**common, source=["body"]).source] == [
        "body"
    ]
    assert (
        AskParams(**common, source={"text": "{{ body }}"}).source.text == "{{ body }}"
    )


def test_ask_and_summarize_have_exact_prompt_semantics() -> None:
    row = Row({"body": "The filing names Ada and reports $4 million."})
    asked = ask(
        AskParams(
            source=["body"],
            model="anthropic/claude-haiku-4-5",
            question="Who is named?",
            context="Rows are SEC filings.",
        ),
        row,
    )
    assert asked.messages[0]["content"].endswith(
        "Dataset context: Rows are SEC filings."
    )
    assert asked.messages[1]["content"][-1]["text"] == "\nWho is named?"

    summarized = summarize(
        SummarizeParams(
            source=["body"],
            model="anthropic/claude-haiku-4-5",
            preset="quotes",
            instruction="Use exactly seven words.",
        ),
        row,
    )
    content = summarized.messages[1]["content"]
    assert content[-1]["text"] == "\nUse exactly seven words."
    assert "newsworthy" not in str(content)


class _StructuredOutput(BaseModel):
    point: GeoPoint
    tags: list[str]
    metadata: dict[str, int]


class _AliasedOutput(BaseModel):
    answer: str = Field(alias="renamed")


class _OutcomeOutput(BaseModel):
    answer: Outcome[str]


class _RootOutput(RootModel[str]):
    pass


class _ValidatedOutput(BaseModel):
    answer: str

    @field_validator("answer")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value:
            raise ValueError("blank")
        return value


class _ReservedOutput(BaseModel):
    error: str


class _ConfiguredOutput(BaseModel):
    model_config = ConfigDict(str_to_lower=True)

    answer: str


class _ModelValidatedOutput(BaseModel):
    answer: str

    @model_validator(mode="after")
    def _validate_model(self):
        return self


class _FieldSerializedOutput(BaseModel):
    answer: str

    @field_serializer("answer")
    def _serialize_answer(self, value: str) -> str:
        return value.lower()


class _ModelSerializedOutput(BaseModel):
    answer: str

    @model_serializer
    def _serialize_model(self) -> dict[str, str]:
        return {"answer": self.answer.lower()}


class _ComputedOutput(BaseModel):
    answer: str

    @computed_field
    @property
    def length(self) -> int:
        return len(self.answer)


class _AnnotatedSerializedOutput(BaseModel):
    answer: Annotated[str, PlainSerializer(str.lower)]


class _CustomInitOutput(BaseModel):
    answer: str

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class _PostInitOutput(BaseModel):
    answer: str

    def model_post_init(self, context: Any) -> None:
        del context


class _AsyncCallableRenderer:
    async def __call__(
        self, params: AskParams, row: Row
    ) -> ModelPrompt[_StructuredOutput]:
        del params, row
        return ModelPrompt(messages=({"role": "user"},))


def _renderer_for(output_model: type[BaseModel], *, async_renderer: bool = False):
    def renderer(params, row):
        del params, row
        return ModelPrompt(messages=({"role": "user"},))

    async def asynchronous(params, row):
        del params, row
        return ModelPrompt(messages=({"role": "user"},))

    chosen = asynchronous if async_renderer else renderer
    chosen.__annotations__ = {
        "params": AskParams,
        "row": Row,
        "return": ModelPrompt[output_model],
    }
    return chosen


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (_renderer_for(_AliasedOutput), "aliases"),
        (_renderer_for(_ValidatedOutput), "validators"),
        (_renderer_for(_ConfiguredOutput), "configuration"),
        (_renderer_for(_OutcomeOutput), "Outcome"),
        (_renderer_for(_RootOutput), "object model"),
        (_renderer_for(_StructuredOutput, async_renderer=True), "synchronous"),
        (_AsyncCallableRenderer(), "synchronous"),
        (_renderer_for(_ModelValidatedOutput), "validators"),
        (_renderer_for(_FieldSerializedOutput), "serializers"),
        (_renderer_for(_ModelSerializedOutput), "serializers"),
        (_renderer_for(_ComputedOutput), "serializers"),
        (_renderer_for(_AnnotatedSerializedOutput), "serializers"),
        (_renderer_for(_CustomInitOutput), "initialization hooks"),
        (_renderer_for(_PostInitOutput), "initialization hooks"),
    ],
)
def test_model_rows_rejects_unsupported_contracts(handler, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        model_rows(handler)


def test_model_rows_accepts_structured_output_fields() -> None:
    terminal = model_rows(_renderer_for(_StructuredOutput))
    fields = {field.key: field for field in terminal.output_fields}
    assert fields["point"].column_type == "geo_point"
    assert fields["point"].schema["type"] == "object"
    assert set(fields["point"].schema["properties"]) == {"lat", "lon"}
    assert fields["tags"].column_type == "json"
    assert fields["metadata"].column_type == "json"


def test_model_rows_accepts_control_like_logical_output_names() -> None:
    terminal = model_rows(_renderer_for(_ReservedOutput))
    assert [field.key for field in terminal.output_fields] == ["error"]


class _FamilyOutput(BaseModel):
    first: str
    second: str


@pytest.mark.parametrize(
    "output_names",
    [
        {"first": "error"},
        {"first": "first_confidence"},
        {"first": "value", "second": "value_justification"},
    ],
)
def test_model_binding_accepts_distinct_control_like_output_names(
    output_names: dict[str, str],
) -> None:
    registered = RegisteredAction(
        "map.family",
        action(
            name="family",
            title="Family",
            description="Test an output family.",
            category=ActionCategory.TEXT,
            run=model_rows(_renderer_for(_FamilyOutput)),
        ),
    )
    _, fields = registered.bind_values(
        scope=SheetRows(sheet_id=1),
        params={
            "source": ["body"],
            "model": "anthropic/claude-haiku-4-5",
            "question": "What?",
        },
        output_names=output_names,
    )
    assert fields is not None
    assert [field.materialized_name(output_names) for field in fields] == [
        output_names.get("first", "first"),
        output_names.get("second", "second"),
    ]
    with pytest.raises(ValueError, match="final output names must be unique"):
        registered.bind_values(
            scope=SheetRows(sheet_id=1),
            params={
                "source": ["body"],
                "model": "anthropic/claude-haiku-4-5",
                "question": "What?",
            },
            output_names={"first": "same", "second": "same"},
        )


class _AnswerAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        del client
        self.requests.append(request)
        data = {"answer": "A real answer"}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            model=request.model,
            tokens_in=10,
            tokens_out=3,
            cost=0.001,
        )


@pytest.mark.parametrize(
    "output_name",
    [
        "error",
        "error_code",
        "answer_confidence",
        "confidence",
        "outcome",
        "justification",
        "__field_errors__",
    ],
)
def test_control_like_physical_names_publish_and_replay_as_values(
    tmp_path: Path,
    output_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This test must observe the request-level consent challenge regardless of
    # the host's standing preapproval setting.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = Project.create(tmp_path / "control-names.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        source_id = project.add_column(sheet_id, "body")
        project.add_rows(sheet_id, [{"body": "A real answer"}], {"body": source_id})
        adapter = _AnswerAdapter()
        router = ModelRouter(
            keys={"anthropic": "fixture"},
            use_env_keys=False,
            cache=None,
            cache_mode="off",
            max_retries=0,
        )
        router._adapters["anthropic"] = adapter
        body = {
            "action_id": "map.ask",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["body"],
                "model": "anthropic/claude-haiku-4-5",
                "question": "What does the source say?",
            },
            "output_names": {"answer": output_name},
            "idempotency_key": "control-names",
        }
        quoted = run_action_spec(project, body, project_id="test", router=router)
        assert quoted.status == "needs_confirmation"
        assert adapter.requests == []
        body["confirmation"] = quoted.errors[0].details["promise_set_hash"]
        result = run_action_spec(project, body, project_id="test", router=router)
        assert result.status == "completed", result.errors
        column = next(c for c in project.columns(sheet_id) if c["name"] == output_name)
        assert list(project.get_values(sheet_id, column["id"]).values()) == [
            "A real answer"
        ]
        cell = project.db.execute(
            "SELECT error, error_code, confidence, justification, outcome FROM results WHERE column_id=?",
            (column["id"],),
        ).fetchone()
        assert tuple(cell) == (None, None, None, None, "ok")
        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?",
                (result.receipt_id,),
            ).fetchone()[0]
        )
        [output] = receipt["outputs"]
        assert output["name"] == output_name
        assert output["ref"]["role"] == "answer"
        assert set(adapter.requests[0].schema["properties"]) == {"answer"}
        replay = run_action_spec(project, body, project_id="test", router=router)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert len(adapter.requests) == 1
    finally:
        project.close()

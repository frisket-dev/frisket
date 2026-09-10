"""Offline tests for the typed classify domain and its admitted local engine."""

from __future__ import annotations

import asyncio
import builtins
import typing
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from frisket.actions.classify import (
    ClassifyField,
    ClassifyParams,
    classify_complete,
    classify_outputs,
    classify_prompt,
    classify_response_schema,
    classify_row,
)
from frisket.actions.types import DynamicOutput, Outcome, Row, RowError, RowResult
from frisket.engine.executor.classify_read import (
    LOCAL_SEMANTIC_CHUNK_CHARS,
    LOCAL_SEMANTIC_MAX_CHUNKS,
    AdmittedClassifier,
    local_semantic_chunks,
)


TOPIC_FIELD = {
    "name": "topic",
    "type": "category",
    "labels": ["accountability", "infrastructure", "other / unclear"],
    "label_descriptions": {
        "accountability": "Government contracts, procurement, and public accountability.",
        "infrastructure": "Roads and public infrastructure projects.",
        "other / unclear": "No clear topic match from the other descriptions.",
    },
}

LLM_FIELDS = [
    {
        "name": "beat",
        "type": "category",
        "labels": ["accountability", "infrastructure"],
        "description": "Primary reporting beat.",
    },
    {"name": "risk_score", "type": "score", "description": "Public-interest risk."},
]


def _local(**overrides: Any) -> ClassifyParams:
    payload: dict[str, Any] = {"source": ["story"], "fields": [TOPIC_FIELD]}
    payload.update(overrides)
    return ClassifyParams.model_validate(payload)


def _llm(**overrides: Any) -> ClassifyParams:
    payload: dict[str, Any] = {
        "source": ["story"],
        "engine": "llm",
        "model": "anthropic/claude-haiku-4-5",
        "context": "Classify city news stories for an accountability desk.",
        "fields": LLM_FIELDS,
        "include_justification": True,
        "include_confidence": True,
    }
    payload.update(overrides)
    return ClassifyParams.model_validate(payload)


# --- (a) Params validation -------------------------------------------------


def test_engine_is_symbolic_and_defaults_to_the_free_engine() -> None:
    assert _local().engine.root == "local_semantic"
    with pytest.raises(ValidationError, match="local_semantic or llm"):
        _local(engine="fastembed")


def test_model_is_required_exactly_when_the_engine_is_llm() -> None:
    with pytest.raises(ValidationError, match="requires a model"):
        _llm(model=None)
    with pytest.raises(ValidationError, match="does not use a model"):
        _local(model="anthropic/claude-haiku-4-5")
    with pytest.raises(ValidationError):
        _llm(model="not-a-provider-model")
    assert (
        _llm().model is not None and _llm().model.root == "anthropic/claude-haiku-4-5"
    )


def test_local_semantic_requires_exactly_one_category_field() -> None:
    with pytest.raises(ValidationError, match="exactly one category field"):
        _local(fields=[TOPIC_FIELD, {"name": "risk", "type": "score"}])
    with pytest.raises(ValidationError, match="exactly one category field"):
        _local(fields=[{"name": "risk", "type": "score"}])
    with pytest.raises(ValidationError, match="only the winning label"):
        _local(include_confidence=True)
    with pytest.raises(ValidationError, match="only the winning label"):
        _local(include_justification=True)
    assert len(_llm().fields) == 2


def test_category_labels_must_be_unique_non_empty_and_describe_labels() -> None:
    with pytest.raises(ValidationError, match="non-empty labels"):
        ClassifyField(name="topic")
    with pytest.raises(ValidationError, match="non-empty"):
        ClassifyField(name="topic", labels=["a", " "])
    with pytest.raises(ValidationError, match="unique"):
        ClassifyField(name="topic", labels=["a", "a"])
    with pytest.raises(ValidationError, match="declared labels"):
        ClassifyField(name="topic", labels=["a"], label_descriptions={"b": "x"})
    with pytest.raises(ValidationError, match="descriptions must be non-empty"):
        ClassifyField(name="topic", labels=["a"], label_descriptions={"a": " "})
    with pytest.raises(ValidationError, match="only category fields"):
        ClassifyField(name="risk", type="score", labels=["a"])
    field = ClassifyField(
        name="topic", labels=["a", "b"], label_descriptions={"a": "A"}
    )
    assert field.type == "category"


@pytest.mark.parametrize("name", ["outcome", "topic_confidence", " ", "error"])
def test_field_names_are_validated_as_runner_output_names(name: str) -> None:
    with pytest.raises(ValidationError):
        ClassifyField(name=name, labels=["a"])


def test_field_names_must_be_unique_across_the_output_family() -> None:
    with pytest.raises(ValidationError, match="unique"):
        _llm(fields=[LLM_FIELDS[0], LLM_FIELDS[0]])
    with pytest.raises(ValidationError, match="unique"):
        _llm(fields=[LLM_FIELDS[0], {"name": "beat_justification", "type": "score"}])


def test_source_is_a_typed_column_list_or_template() -> None:
    with pytest.raises(ValidationError):
        _local(source=[])
    with pytest.raises(ValidationError):
        _local(source=["story", "story"])
    with pytest.raises(ValidationError):
        _local(source={"text": "literal only"})
    assert _local(source={"text": "{{ story }}"}).source.text == "{{ story }}"
    with pytest.raises(ValidationError):
        _local(input_columns=["story"])


# --- (b) Output projection -------------------------------------------------


ALL_TYPE_FIELDS = [
    *LLM_FIELDS,
    {"name": "is_public", "type": "boolean", "description": "Public body involved."},
    {"name": "mentions", "type": "integer"},
    {"name": "amount", "type": "number", "description": "Dollar amount."},
    {"name": "note", "type": "text"},
]


def test_outputs_project_each_field_as_one_outcome_column() -> None:
    outputs = classify_outputs(
        _llm(
            fields=ALL_TYPE_FIELDS,
            include_justification=False,
            include_confidence=False,
        )
    )
    assert list(outputs) == [
        "beat",
        "risk_score",
        "is_public",
        "mentions",
        "amount",
        "note",
    ]
    outputs["risk_score"].model_validate({"status": "ok", "value": 7})
    for invalid_score in (-1, 11):
        with pytest.raises(ValidationError):
            outputs["risk_score"].model_validate(
                {"status": "ok", "value": invalid_score}
            )
    assert outputs["is_public"] is Outcome[bool]
    assert outputs["mentions"] is Outcome[int]
    assert outputs["amount"] is Outcome[float]
    assert outputs["note"] is Outcome[str]
    beat_value = outputs["beat"].model_fields["value"].annotation
    assert (
        typing.get_args(beat_value)[0]
        == typing.Literal["accountability", "infrastructure"]
    )
    outputs["beat"].model_validate({"status": "ok", "value": "accountability"})
    with pytest.raises(ValidationError):
        outputs["beat"].model_validate({"status": "ok", "value": "sports"})


def test_outputs_add_companion_columns_only_when_requested() -> None:
    with_flags = classify_outputs(_llm())
    assert list(with_flags) == [
        "beat",
        "risk_score",
        "beat_justification",
        "risk_score_justification",
        "beat_confidence",
    ]
    assert with_flags["beat_justification"] == (str | None)
    from pydantic import TypeAdapter

    confidence = TypeAdapter(with_flags["beat_confidence"])
    assert confidence.validate_python(0.7) == 0.7
    with pytest.raises(ValidationError):
        confidence.validate_python(1.1)
    without_flags = classify_outputs(
        _llm(include_justification=False, include_confidence=False)
    )
    assert list(without_flags) == ["beat", "risk_score"]
    assert list(classify_outputs(_local())) == ["topic"]


def test_response_schema_gates_justification_and_confidence_on_include_flags() -> None:
    schema = classify_response_schema(_llm())
    assert list(schema["properties"]) == [
        "beat",
        "beat_justification",
        "risk_score",
        "risk_score_justification",
        "beat_confidence",
    ]
    assert schema["required"] == list(schema["properties"])
    bare = classify_response_schema(
        _llm(include_justification=False, include_confidence=False)
    )
    assert list(bare["properties"]) == ["beat", "risk_score"]


# --- (c) Complete prompt and constrained response contract -----------------


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"context": ""},
        {"include_justification": False, "include_confidence": False},
        {"fields": [TOPIC_FIELD]},
        {"fields": ALL_TYPE_FIELDS},
        {"fields": ALL_TYPE_FIELDS, "include_justification": False},
        {"source": ["story", "source_url"]},
    ],
)
def test_prompt_preserves_selected_sources_and_field_contract(
    overrides: dict[str, Any],
) -> None:
    params = _llm(**overrides)
    row_values = {
        "story": "City hall awarded a no-bid contract",
        "source_url": "https://example.com/story/1",
    }
    row = Row({column.name: row_values[column.name] for column in params.source})
    prompt = classify_prompt(params, row)
    assert [message["role"] for message in prompt.messages] == ["system", "user"]
    assert prompt.messages[1]["content"][0] == {
        "type": "text",
        "text": "\n".join(
            f"{column.name}: {row_values[column.name]}" for column in params.source
        ),
    }
    field_text = prompt.messages[1]["content"][-1]["text"]
    for field in params.fields:
        assert f"- {field.name}: {field.description or field.name}" in field_text
        for label in field.labels:
            assert label in field_text
    assert ("Dataset context:" in prompt.messages[0]["content"]) == bool(params.context)
    assert prompt.response_schema == classify_response_schema(params)
    assert (
        "Scores run 0 (not at all) to 10 (extremely)" in prompt.messages[0]["content"]
    )


def test_template_source_renders_as_labeled_input() -> None:
    params = _llm(source={"text": "Story: {{ story }}"})
    row = Row({"input": "Story: Routine road work finished early"})
    assert classify_prompt(params, row).messages[1]["content"][0] == {
        "type": "text",
        "text": "input: Story: Routine road work finished early",
    }


def test_prompt_mentions_labels_with_descriptions_and_context() -> None:
    prompt = classify_prompt(_llm(fields=[TOPIC_FIELD]), Row({"story": "x"}))
    assert prompt.messages[0]["content"].endswith(
        "Dataset context: Classify city news stories for an accountability desk."
    )
    text = prompt.messages[1]["content"][-1]["text"]
    assert text.startswith("\nAssess the content above on:\n- topic: topic (one of: ")
    assert "accountability: Government contracts" in text
    assert "other / unclear: No clear topic match" in text


# --- (d) Admitted local classifier -----------------------------------------


def _patch_local_semantic_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    from frisket.ai.embeddings.capabilities import build_batch_result
    from frisket.ai.embeddings.gateway import EmbeddingGateway

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.setenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", "1")
    monkeypatch.setenv("FRISKET_PROVIDERLESS_CLASSIFY_THREADS", "2")
    embedded: list[list[str]] = []

    def fake_embed(  # noqa: ANN001
        self, texts, *, provider, model, modality, local_capability=None
    ):
        del self
        assert provider == "fastembed"
        assert model == "BAAI/bge-small-en-v1.5"
        assert modality == "text"
        assert local_capability == "providerless_classify"
        embedded.append(list(texts))
        vectors = []
        for text in texts:
            lowered = text.lower()
            if "government contracts" in lowered:
                vectors.append([1.0, 0.0])
            elif "roads and public infrastructure" in lowered:
                vectors.append([0.0, 1.0])
            elif "no clear topic" in lowered:
                vectors.append([-1.0, 0.0])
            elif "no-bid contract" in lowered:
                vectors.append([0.95, 0.05])
            elif "road work" in lowered:
                vectors.append([0.1, 0.9])
            elif lowered in ("twin a", "twin b", "twin text"):
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 0.0])
        return build_batch_result(
            vectors,
            provider_id="fastembed",
            provider_kind="local_process",
            actual_model_id="fastembed/BAAI/bge-small-en-v1.5",
        )

    monkeypatch.setattr(EmbeddingGateway, "embed", fake_embed)
    return embedded


TOPIC = (ClassifyField.model_validate(TOPIC_FIELD),)
STORY_A = "City hall awarded a no-bid contract"
STORY_B = "Routine road work finished early"


@pytest.mark.asyncio
async def test_local_classifier_picks_the_cosine_winner_and_caches_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedded = _patch_local_semantic_embedding(monkeypatch)
    classifier = AdmittedClassifier()

    first = await classifier.classify(Row({}), STORY_A, TOPIC)
    second = await classifier.classify(Row({}), STORY_B, TOPIC)

    assert first == {"topic": Outcome.ok("accountability")}
    assert second == {"topic": Outcome.ok("infrastructure")}
    assert embedded == [
        [
            "Government contracts, procurement, and public accountability.",
            "Roads and public infrastructure projects.",
            "No clear topic match from the other descriptions.",
        ],
        [STORY_A],
        [STORY_B],
    ]


@pytest.mark.asyncio
async def test_local_classifier_breaks_ties_toward_the_lower_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_local_semantic_embedding(monkeypatch)
    twins = (ClassifyField(name="twin", labels=["twin a", "twin b"]),)
    assert await AdmittedClassifier().classify(Row({}), "twin text", twins) == {
        "twin": Outcome.ok("twin a")
    }
    reordered = (
        ClassifyField(
            name="twin", labels=["Roads and public infrastructure projects.", "twin b"]
        ),
    )
    assert await AdmittedClassifier().classify(Row({}), "twin text", reordered) == {
        "twin": Outcome.ok("twin b")
    }


@pytest.mark.asyncio
async def test_local_classifier_returns_the_first_label_without_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedded = _patch_local_semantic_embedding(monkeypatch)
    assert await AdmittedClassifier().classify(Row({}), "   \n\t ", TOPIC) == {
        "topic": Outcome.ok("accountability")
    }
    assert embedded == []


@pytest.mark.asyncio
async def test_local_classifier_serializes_rows_under_one_label_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedded = _patch_local_semantic_embedding(monkeypatch)
    classifier = AdmittedClassifier()
    results = await asyncio.gather(
        classifier.classify(Row({}), STORY_A, TOPIC),
        classifier.classify(Row({}), STORY_B, TOPIC),
    )
    assert [result["topic"].value for result in results] == [
        "accountability",
        "infrastructure",
    ]
    assert sum(len(texts) == 3 for texts in embedded) == 1
    assert len(embedded) == 3


@pytest.mark.asyncio
async def test_local_classifier_refuses_anything_but_one_category_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedded = _patch_local_semantic_embedding(monkeypatch)
    llm_fields = tuple(_llm().fields)
    with pytest.raises(RowError) as error:
        await AdmittedClassifier().classify(Row({}), STORY_A, llm_fields)
    assert error.value.code == "invalid_classify_field"
    with pytest.raises(RowError):
        await AdmittedClassifier().classify(Row({}), STORY_A, (llm_fields[1],))
    assert embedded == []


@pytest.mark.asyncio
async def test_local_classifier_refuses_when_closed_or_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedded = _patch_local_semantic_embedding(monkeypatch)
    cancelled = AdmittedClassifier(cancelled=lambda: True)
    with pytest.raises(asyncio.CancelledError):
        await cancelled.classify(Row({}), STORY_A, TOPIC)
    closed = AdmittedClassifier()
    await closed.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await closed.classify(Row({}), STORY_A, TOPIC)
    assert embedded == []


@pytest.mark.asyncio
async def test_bound_classifier_requires_its_admitted_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_local_semantic_embedding(monkeypatch)
    row = Row({"story": STORY_A})
    bound = AdmittedClassifier().bind_row(row, sheet_id=1, row_id=1, sources=None)
    assert await bound.classify(row, STORY_A, TOPIC) == {
        "topic": Outcome.ok("accountability")
    }
    with pytest.raises(RowError):
        await bound.classify(Row({}), STORY_A, TOPIC)


def test_providerless_work_stays_deterministically_bounded() -> None:
    chunks = local_semantic_chunks("x" * (LOCAL_SEMANTIC_CHUNK_CHARS * 20))

    assert len(chunks) == LOCAL_SEMANTIC_MAX_CHUNKS == 8
    assert all(len(chunk) <= LOCAL_SEMANTIC_CHUNK_CHARS for chunk in chunks)


@pytest.mark.asyncio
async def test_global_fence_refuses_before_fastembed_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.ai.embeddings import EmbeddingBackendUnavailable

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.delenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", raising=False)
    real_import = builtins.__import__

    def no_fastembed_import(name, *args, **kwargs):  # noqa: ANN001
        if name == "fastembed" or name.startswith("fastembed."):
            raise AssertionError("the disabled Classify path must not import FastEmbed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fastembed_import)
    with pytest.raises(EmbeddingBackendUnavailable):
        await AdmittedClassifier().classify(
            Row({}),
            "A story requiring classification",
            (ClassifyField(name="topic", labels=["news", "other"]),),
        )


# --- (e) classify_row end to end --------------------------------------------


class _StubClassifier:
    """Engine-agnostic stand-in: one call, one outcome per declared field."""

    def __init__(self) -> None:
        self.calls: list[tuple[Row, str, tuple[ClassifyField, ...]]] = []

    async def classify(
        self, row: Row, text: str, fields: Sequence[ClassifyField]
    ) -> dict[str, Outcome[Any]]:
        self.calls.append((row, text, tuple(fields)))
        return {
            field.name: Outcome.ok(field.labels[1])
            if field.type == "category"
            else Outcome.ok(7, confidence=0.8, justification="Public spending.")
            for field in fields
        }


@pytest.mark.asyncio
async def test_classify_row_joins_scalar_sources_and_returns_the_outcome() -> None:
    params = _local(source=["story", "meta", "count", "flag"])
    row = Row(
        {
            "story": STORY_B,
            "meta": {"blob": "ignored"},
            "count": 3,
            "flag": None,
        }
    )
    classifier = _StubClassifier()

    result = await classify_row(params, row, classifier)

    assert result == RowResult(
        output=DynamicOutput({"topic": Outcome.ok("infrastructure")})
    )
    ((seen_row, text, fields),) = classifier.calls
    assert seen_row is row
    assert text == f"{STORY_B}\n3"
    assert fields == tuple(params.fields)
    assert classify_outputs(params)["topic"].model_validate(
        result.output.root["topic"].model_dump()
    )


@pytest.mark.asyncio
async def test_classify_row_uses_the_rendered_template_as_source_text() -> None:
    params = _local(source={"text": "Story: {{ story }}"})
    classifier = _StubClassifier()
    await classify_row(params, Row({"input": "Story: road work"}), classifier)
    assert classifier.calls[0][1] == "Story: road work"


def test_classify_completion_preserves_assessments_and_companions() -> None:
    params = _llm()
    result = classify_complete(
        params,
        Row({"story": STORY_A}),
        DynamicOutput(
            {
                "beat": "infrastructure",
                "risk_score": 7,
                "beat_justification": "Road maintenance.",
                "risk_score_justification": "Public spending.",
                "beat_confidence": 0.8,
            }
        ),
    )
    outputs = classify_outputs(params)
    assert list(result.output.root) == list(outputs)
    assert result.output.root["beat"] == Outcome.ok(
        "infrastructure", confidence=0.8, justification="Road maintenance."
    )
    assert result.output.root["risk_score"].confidence is None
    assert result.output.root["risk_score"].justification == "Public spending."
    # Companion columns are derived from the same outcomes, never re-asked.
    assert result.output.root["beat_justification"] == "Road maintenance."
    assert result.output.root["risk_score_justification"] == "Public spending."
    assert result.output.root["beat_confidence"] == 0.8
    for name, annotation in outputs.items():
        value = result.output.root[name]
        if isinstance(value, Outcome):
            annotation.model_validate(value.model_dump())
        else:
            TypeAdapter(annotation).validate_python(value)


def test_classify_completion_omits_companions_unless_requested() -> None:
    params = _llm(include_justification=False, include_confidence=False)
    result = classify_complete(
        params,
        Row({"story": STORY_A}),
        DynamicOutput(
            {
                "beat": "infrastructure",
                "risk_score": 7,
            }
        ),
    )
    assert list(result.output.root) == ["beat", "risk_score"]


@pytest.mark.asyncio
async def test_classify_row_fails_the_row_when_an_outcome_is_missing() -> None:
    class _Partial(_StubClassifier):
        async def classify(self, row, text, fields):  # noqa: ANN001
            return {"beat": Outcome.ok("accountability")}

    with pytest.raises(RowError) as error:
        await classify_row(_llm(), Row({"story": STORY_A}), _Partial())
    assert error.value.code == "classify_output_missing"
    assert "risk_score" in error.value.message

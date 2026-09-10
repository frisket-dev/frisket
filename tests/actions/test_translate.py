import json

import pytest

from frisket.actions.translate import (
    TranslateParams,
    complete_translation,
    translate_prompt,
    translation_outputs,
)
from frisket.actions.types import DynamicOutput, Row, RowError


def test_runtime_default_remains_llm_and_model_is_required():
    with pytest.raises(ValueError, match="requires model"):
        TranslateParams(source=["text"])
    assert (
        TranslateParams(source=["text"], model="anthropic/example").engine.root == "llm"
    )


@pytest.mark.parametrize("engine", ["deepl", "google_translate", "opus_mt", "hy_mt2"])
def test_direct_routes_reject_llm_only_options(engine):
    params = {"source": ["text"], "engine": engine}
    if engine == "opus_mt":
        params["language"] = ["es"]
    assert TranslateParams(**params).model is None
    with pytest.raises(ValueError, match="require the LLM"):
        TranslateParams(**params, model="anthropic/example")
    with pytest.raises(ValueError, match="require the LLM"):
        TranslateParams(**params, context="newsroom")


def test_model_prompt_preserves_context_rich_inputs_and_logical_detection():
    params = TranslateParams(
        source=["text", "image"],
        model="anthropic/example",
        context="Dataset notes",
        language=["es"],
        save_detected_language=True,
    )
    row = Row(
        {"text": "Hola", "image": {"__image_b64__": "aGVsbG8=", "mime": "image/png"}}
    )
    prompt = translate_prompt(params, row)
    assert "Dataset notes" in prompt.messages[0]["content"]
    assert {
        "type": "image",
        "media_type": "image/png",
        "data": "aGVsbG8=",
    } in prompt.messages[1]["content"]
    rendered = json.dumps(prompt.messages, ensure_ascii=False)
    assert "Hola" in rendered
    assert "source language is es" in rendered
    assert "`detected_language`" in rendered
    assert "do not summarize or omit" in rendered
    assert translation_outputs(params) == ("translation", "detected_language")
    result = complete_translation(
        params,
        row,
        DynamicOutput({"translation": "Hello", "detected_language": "PT-br"}),
    )
    assert result.output.detected_language.root == "pt-BR"
    assert result.output.translation == "Hello"


def test_detection_is_inactive_by_default_and_unknown_is_successful_absence():
    params = TranslateParams(source=["text"], model="anthropic/example")
    assert translation_outputs(params) == ("translation",)
    row = Row({"text": "Hola"})
    result = complete_translation(
        params, row, DynamicOutput({"translation": "Hello", "detected_language": "ES"})
    )
    assert result.output.detected_language is None


def test_detected_language_is_required_when_active_and_materializes_as_category():
    from frisket.actions.registry import ACTION_REGISTRY
    from jsonschema import Draft202012Validator

    params = TranslateParams(
        source=["text"], model="anthropic/example", save_detected_language=True
    )
    row = Row({"text": "Hola"})
    schema = translate_prompt(params, row).response_schema
    assert not Draft202012Validator(schema).is_valid({"translation": "Hello"})
    assert Draft202012Validator(schema).is_valid(
        {"translation": "Hello", "detected_language": None}
    )
    with pytest.raises(RowError, match="missing detected_language"):
        complete_translation(params, row, DynamicOutput({"translation": "Hello"}))
    fields = ACTION_REGISTRY.get("map.translate").definition.run.resolve_output_fields(
        params
    )
    assert (
        next(field for field in fields if field.key == "detected_language").column_type
        == "category"
    )
    params.save_detected_language = False
    assert translate_prompt(params, row).response_schema["required"] == ["translation"]
    params.save_detected_language = True
    result = complete_translation(
        params,
        row,
        DynamicOutput(
            {"translation": "Hello", "detected_language": "nonsense language"}
        ),
    )
    assert result.output.detected_language is None

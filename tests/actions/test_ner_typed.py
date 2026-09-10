from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.ner import (
    NER,
    NerModelOutput,
    NerParams,
    ner_complete,
    ner_direct,
    ner_prompt,
)
from frisket.actions.types import Row


def test_route_validation_keeps_local_free_and_requires_an_explicit_llm_model():
    for params in (
        {"engine": "llm"},
        {"engine": "spacy", "model": "openai/gpt-4o-mini"},
        {"engine": "gliner", "model": "openai/gpt-4o-mini"},
        {"labels": []},
        {"labels": ["person", " person "]},
        {"labels": [" "]},
        {"threshold": 1.1},
    ):
        with pytest.raises(ValidationError):
            NerParams.model_validate(
                {"source": ["body"], "labels": ["person"], **params}
            )
    assert NerParams(source=["body"], labels=["person"]).model is None


def test_provider_schema_excludes_comparison_keys_and_stored_output_stays_feedable():
    schema = NerModelOutput.model_json_schema()
    properties = schema["$defs"]["EntityModelValue"]["properties"]
    # ``label`` is the raw provider spelling canonical normalization maps onto
    # ``type`` (see ner_complete below); the server-derived comparison keys
    # never reach the provider schema.
    assert set(properties) == {"text", "type", "label", "start", "end", "score"}
    assert {"fingerprint", "metadata"}.isdisjoint(properties)
    output = NER.run.output_fields[0]
    assert output.key == "entities"
    assert output.named_result.may_feed == ["derive.table_from_list"]


def test_model_request_uses_the_exact_unicode_input_and_normalizes_server_keys():
    params = NerParams(
        source=["body"],
        labels=["organization"],
        engine="llm",
        model="openai/gpt-4o-mini",
    )
    row = Row({"body": "😀 ACME Corp."})
    prompt = ner_prompt(params, row)
    assert prompt.messages[1]["content"] == [{"type": "text", "text": "😀 ACME Corp."}]
    result = ner_complete(
        params,
        row,
        NerModelOutput(
            entities=[
                {"text": "ACME Corp.", "type": "ORG", "start": 2, "end": 12},
                {"text": "invalid missing coordinates"},
            ]
        ),
    )
    assert result.output.entities == [
        {
            "text": "ACME Corp.",
            "type": "organization",
            "start": 2,
            "end": 12,
            "score": None,
            "fingerprint": "acme corp",
            "metadata": {"raw_label": "ORG"},
        }
    ]


@pytest.mark.asyncio
async def test_direct_handler_passes_the_actual_rendered_text_labels_and_threshold():
    class Extractor:
        async def extract(self, text, *, labels, threshold):
            assert (text, labels, threshold) == ("Name: Ada", ["person"], 0.7)
            return [{"text": "Ada", "label": "person", "start": 6, "end": 9}]

    params = NerParams(
        source={"text": "Name: {{body}}"},
        labels=["person"],
        engine="gliner",
        threshold=0.7,
    )
    result = await ner_direct(params, Row({"input": "Name: Ada"}), Extractor())
    assert result.output.entities[0]["text"] == "Ada"


@pytest.mark.asyncio
async def test_local_adapter_skips_blank_input_and_uses_real_call_arguments(
    monkeypatch,
):
    from frisket.ops import ner_local

    calls = []

    async def sidecar(context, path, *, json, op):
        calls.append((context, path, json, op))
        return {"results": [[{"text": "Ada", "label": "person", "start": 0, "end": 3}]]}

    monkeypatch.setattr(ner_local, "sidecar_post", sidecar)
    context = object()
    extractor = ner_local.LocalNerExtractor("gliner", context)
    assert await extractor.extract(" ", labels=["person"], threshold=0.2) == []
    result = await extractor.extract("Ada", labels=["person"], threshold=0.2)
    assert calls == [
        (
            context,
            "/ner",
            {
                "texts": ["Ada"],
                "labels": ["person"],
                "threshold": 0.2,
                "engine": "gliner",
            },
            "ner",
        )
    ]
    assert result[0]["fingerprint"] == "ada"

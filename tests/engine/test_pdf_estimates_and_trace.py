"""Direct document estimates and diagnostics never price or retain base64 bytes."""

import base64
import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart

from frisket.ai.llm.pdf_estimates import estimate_input_tokens
from frisket.ai.llm.pricing import estimate_tokens
from frisket.operability.trace import TraceWriter, redact

PDF = b"%PDF-1.7\nprivate document bytes\n%%EOF"
ENCODED = base64.b64encode(PDF).decode()


def document(data=ENCODED, **metadata):
    return {
        "type": "document",
        "media_type": "application/pdf",
        "data": data,
        "filename": "evidence.pdf",
        "page_count": 2,
        "text_char_count": 800,
        **metadata,
    }


def test_pdf_estimate_uses_pages_and_native_text_not_encoded_length():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Summarize"}, document()]}
    ]
    tokens = estimate_input_tokens(messages)
    messages[0]["content"][1]["data"] = ENCODED * 100_000
    assert estimate_input_tokens(messages) == tokens
    assert 6200 < tokens < 6300
    messages[0]["content"][1]["page_count"] = 3
    assert estimate_input_tokens(messages) == tokens + 3000


def test_text_and_image_estimates_preserve_existing_json_math():
    for messages in (
        [{"role": "user", "content": "Hello"}],
        [{"type": "image", "data": ENCODED}],
    ):
        assert estimate_input_tokens(messages) == estimate_tokens(json.dumps(messages))


@pytest.mark.parametrize(
    "metadata", [{"page_count": None}, {"page_count": False}, {"text_char_count": -1}]
)
def test_missing_pdf_metadata_remains_unknown(metadata):
    assert estimate_input_tokens([document(**metadata)]) is None


@pytest.mark.parametrize(
    "payload",
    [
        document(),
        {"__pdf_b64__": ENCODED, "filename": "evidence.pdf"},
        {
            "type": "document",
            "source": {
                "media_type": "application/pdf",
                "type": "base64",
                "data": ENCODED,
            },
        },
        {
            "type": "file",
            "file": {
                "filename": "evidence.pdf",
                "file_data": "data:application/pdf;base64," + ENCODED,
            },
        },
        BinaryContent(data=PDF, media_type="application/pdf"),
        {"nested": BinaryContent(data=PDF, media_type="application/pdf")},
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=[BinaryContent(data=PDF, media_type="application/pdf")]
                )
            ]
        ),
        json.dumps({"content": [document()]}),
    ],
)
def test_traces_keep_metadata_hash_never_pdf_bytes(tmp_path, payload):
    writer = TraceWriter.open(tmp_path, 11)
    writer.write_row(
        {
            "kind": "row",
            "row_id": 1,
            "calls": [{"model": "openai/test", "request": {"messages": payload}}],
        }
    )
    with gzip.open(writer.path, "rt") as stream:
        stored = stream.read()
    assert ENCODED not in stored
    assert "private document bytes" not in stored
    assert hashlib.sha256(PDF).hexdigest() in stored
    assert "application/pdf" in stored


def test_trace_pdf_sanitization_keeps_existing_cycle_and_truncation_guards():
    data = {"document": document()}
    data["self"] = data
    result = redact(data)
    assert result["self"] == "[CYCLE]"
    assert redact(result) == result


def test_runner_pdf_estimate_discloses_approximation_and_preserves_unknown(monkeypatch):
    from frisket.engine.runner import validation

    messages = [{"role": "user", "content": [document()]}]
    project = SimpleNamespace(columns=lambda _sheet: [])
    recipe = SimpleNamespace(
        is_llm=lambda _spec: True,
        render=lambda _values, _spec: SimpleNamespace(messages=messages),
    )
    monkeypatch.setattr(validation, "target_rows", lambda *_args: [1, 2])
    monkeypatch.setattr(validation, "row_values", lambda *_args: {})
    monkeypatch.setattr(
        validation,
        "model_pricing",
        lambda _model: SimpleNamespace(
            price=(1.0, 2.0), cost_source="known", pricing_key="openai/test"
        ),
    )
    estimate = validation._estimate_run(
        project, {"sheet_id": 1}, program=recipe
    ).presentation
    assert estimate["cost"] > 0
    assert "approximate" in estimate["warning"]
    assert 6200 < estimate["avg_input_tokens"] < 6300
    messages[0]["content"][0]["page_count"] = None
    unknown = validation._estimate_run(
        project, {"sheet_id": 1}, program=recipe
    ).presentation
    assert unknown["cost"] is None
    assert unknown["cost_source"] == "unknown"


def test_cloud_pdf_estimate_does_not_price_base64():
    from frisket.cloud_execution import cloud_llm_estimated_spend_micro
    from frisket.contracts.http.cloud_connection import CloudLLMRequest, CloudTokenPrice

    price = CloudTokenPrice(
        kind="tokens",
        input_micro_per_million_tokens=1_000_000,
        output_micro_per_million_tokens=2_000_000,
    )
    request = CloudLLMRequest(
        model="openai/gpt-4.1",
        messages=[{"role": "user", "content": [document()]}],
        max_tokens=200,
        temperature=0,
    )
    first = cloud_llm_estimated_spend_micro(request, price)
    request.messages[0]["content"][0]["data"] = ENCODED * 100_000
    assert cloud_llm_estimated_spend_micro(request, price) == first
    assert first > 6200


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"content": [document(data=ENCODED * 100_000)]}),
        '{"type":"document","media_type":"application/pdf","data":"' + ENCODED,
        repr(BinaryContent(data=PDF, media_type="application/pdf")),
    ],
)
def test_oversized_or_partial_serialized_pdf_diagnostics_do_not_leak(payload):
    safe = json.dumps(redact({"events": [payload]}))
    assert ENCODED not in safe
    assert "private document bytes" not in safe
    assert "omitted" in safe


def test_pdf_with_unextractable_native_text_still_has_page_estimate():
    assert estimate_input_tokens([document(text_char_count=None)]) > 6000

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from pydantic_ai.messages import BinaryContent

from frisket.ai.llm.adapters import AnthropicAdapter, OpenAICompatAdapter
from frisket.ai.llm.structured import (
    StructuredCompleter,
    StructuredRequest,
    _content_from_pai,
    _content_to_pai,
)
from frisket.ai.llm.types import LLMError, LLMRequest, LLMResponse
from frisket.ai.message_content import render_input_block

PDF_BYTES = b"%PDF-1.4\noriginal PDF content\n%%EOF\n"
PDF = {
    "type": "document",
    "media_type": "application/pdf",
    "data": base64.b64encode(PDF_BYTES).decode(),
    "filename": "report.pdf",
    "page_count": 2,
    "text_char_count": 42,
}


def test_render_mixed_text_pdf_and_image_without_stringifying_pdf():
    content = render_input_block(
        {
            "Question": "What happened?",
            "Report": {
                "__pdf_b64__": PDF["data"],
                "mime": "application/pdf",
                "filename": "report.pdf",
                "page_count": 2,
                "text_char_count": 42,
            },
            "Photo": {"__image_b64__": "AA==", "mime": "image/png"},
        }
    )
    assert content[1] == PDF
    assert content[2]["type"] == "image"
    assert "Question: What happened?" in content[0]["text"]
    assert "Report: PDF attachment (report.pdf)" in content[0]["text"]
    assert PDF["data"] not in content[0]["text"]
    assert _content_from_pai(_content_to_pai(content)) == content


def test_pdf_binary_roundtrip_preserves_metadata_and_bytes():
    binary = _content_to_pai([PDF])[0]
    assert isinstance(binary, BinaryContent)
    assert binary.data == PDF_BYTES
    assert _content_from_pai([binary]) == [PDF]
    assert _content_from_pai(
        [BinaryContent(data=PDF_BYTES, media_type="application/pdf")]
    ) == [{key: PDF[key] for key in ("type", "media_type", "data")}]


@pytest.mark.parametrize(
    "part",
    [
        {"type": "document", "media_type": "text/plain", "data": "AA=="},
        {"type": "file", "data": "AA=="},
    ],
)
def test_structured_bridge_refuses_unsupported_content_instead_of_dropping(part):
    with pytest.raises(TypeError):
        _content_to_pai([part])


def test_provider_wire_payloads_deliver_original_pdf_bytes():
    for provider, model in [
        ("openai", "openai/gpt-5-mini"),
        ("anthropic", "anthropic/claude-sonnet-4-5"),
    ]:
        seen = []

        async def transport(request):
            seen.append(json.loads(request.content))
            if provider == "openai":
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"content": "Done"}, "finish_reason": "stop"}
                        ],
                        "usage": {},
                    },
                )
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "Done"}], "usage": {}}
            )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(transport)
            ) as client:
                adapter = (
                    OpenAICompatAdapter("fake", "https://api.openai.com/v1")
                    if provider == "openai"
                    else AnthropicAdapter("fake")
                )
                return await adapter.complete(
                    LLMRequest(
                        model=model,
                        messages=[
                            {
                                "role": "user",
                                "content": [{"type": "text", "text": "Summarize"}, PDF],
                            }
                        ],
                    ),
                    client,
                )

        asyncio.run(run())
        part = seen[0]["messages"][0]["content"][1]
        if provider == "openai":
            assert part == {
                "type": "file",
                "file": {
                    "filename": "report.pdf",
                    "file_data": "data:application/pdf;base64," + PDF["data"],
                },
            }
            data = part["file"]["file_data"].split(",", 1)[1]
        else:
            assert part == {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": PDF["data"],
                },
                "title": "report.pdf",
                "citations": {"enabled": False},
            }
            data = part["source"]["data"]
        assert base64.b64decode(data) == PDF_BYTES


@pytest.mark.parametrize(
    "model",
    [
        "gemini/gemini-2.5-flash",
        "ollama/@local/model",
        "openrouter/openai/gpt-5-mini",
        "openai/gpt-3.5-turbo",
    ],
)
def test_unsupported_routes_refuse_before_http(model):
    calls = []

    async def transport(request):
        calls.append(request)
        raise AssertionError("must not send")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as client:
            await OpenAICompatAdapter("fake", "https://provider.invalid").complete(
                LLMRequest(model=model, messages=[{"role": "user", "content": [PDF]}]),
                client,
            )

    with pytest.raises(LLMError):
        asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"data": "not-base64"},
        {"data": base64.b64encode(b"not a PDF").decode()},
        {"media_type": "application/octet-stream"},
    ],
)
def test_bad_pdf_refuses_before_http_without_echoing_data(change):
    part = PDF | change

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: (_ for _ in ()).throw(AssertionError("must not send"))
            )
        ) as client:
            await OpenAICompatAdapter("fake", "https://api.openai.com/v1").complete(
                LLMRequest(
                    model="openai/gpt-5-mini",
                    messages=[{"role": "user", "content": [part]}],
                ),
                client,
            )

    with pytest.raises(LLMError) as caught:
        asyncio.run(run())
    assert part["data"] not in str(caught.value)


def test_structured_repair_preserves_pdf_and_metadata():
    class Router:
        seen = []

        def note_schema_reject(self, provider):
            pass

        async def complete_transport(self, req, **kwargs):
            self.seen.append(req)
            data = {} if len(self.seen) == 1 else {"answer": "yes"}
            return LLMResponse(
                content=json.dumps(data),
                data=data,
                tokens_in=1,
                tokens_out=1,
                cost=0.0,
                model=req.model,
            )

    router = Router()
    result = asyncio.run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="openai/gpt-5-mini",
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Extract"}, PDF],
                    }
                ],
                schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            )
        )
    )
    assert result.response.data == {"answer": "yes"}
    assert len(router.seen) == 2
    for request in router.seen:
        assert request.messages[0]["content"][1] == PDF


def test_pdf_total_request_limit_is_aggregate_and_pre_egress(monkeypatch):
    from frisket.ai.llm import document_input

    monkeypatch.setattr(document_input, "MAX_PDF_INPUT_BYTES", len(PDF_BYTES))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: (_ for _ in ()).throw(AssertionError("must not send"))
            )
        ) as client:
            await OpenAICompatAdapter("fake", "https://api.openai.com/v1").complete(
                LLMRequest(
                    model="openai/gpt-5-mini",
                    messages=[{"role": "user", "content": [PDF, PDF]}],
                ),
                client,
            )

    with pytest.raises(LLMError, match="request limit"):
        asyncio.run(run())


def test_provider_error_cannot_echo_pdf_request_data():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    429,
                    json={
                        "error": {
                            "code": "insufficient_quota",
                            "message": "echoed input " + PDF["data"],
                        }
                    },
                )
            )
        ) as client:
            await OpenAICompatAdapter("fake", "https://api.openai.com/v1").complete(
                LLMRequest(
                    model="openai/gpt-5-mini",
                    messages=[{"role": "user", "content": [PDF]}],
                ),
                client,
            )

    with pytest.raises(LLMError) as caught:
        asyncio.run(run())
    assert PDF["data"] not in str(caught.value)
    assert "echoed input" not in str(caught.value)
    assert caught.value.provider_code == "insufficient_quota"


def test_structured_unsupported_pdf_model_refuses_before_transport():
    class Router:
        async def complete_transport(self, *args, **kwargs):
            raise AssertionError("must not send")

    with pytest.raises(LLMError):
        asyncio.run(
            StructuredCompleter(Router()).complete(
                StructuredRequest(
                    model="gemini/gemini-2.5-flash",
                    messages=[{"role": "user", "content": [PDF]}],
                    schema={"type": "object"},
                )
            )
        )


@pytest.mark.parametrize("pages, allowed", [([50, 50], True), ([100, 100], False)])
def test_pdf_page_request_limit_sums_multiple_document_columns(pages, allowed):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as client:
            return await OpenAICompatAdapter(
                "fake", "https://api.openai.com/v1"
            ).complete(
                LLMRequest(
                    model="openai/gpt-5-mini",
                    messages=[
                        {
                            "role": "user",
                            "content": [PDF | {"page_count": count} for count in pages],
                        }
                    ],
                ),
                client,
            )

    if allowed:
        assert asyncio.run(run()).content == "Done"
        assert len(calls) == 1
    else:
        with pytest.raises(LLMError, match="100-page request limit"):
            asyncio.run(run())
        assert calls == []

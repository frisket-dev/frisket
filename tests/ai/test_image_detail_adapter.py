from __future__ import annotations

import base64

from frisket.ai.llm.adapters import OpenAICompatAdapter
from frisket.ai.llm.structured import _content_from_pai, _content_to_pai


def test_image_detail_survives_structured_message_translation() -> None:
    content = [
        {"type": "text", "text": "read this"},
        {
            "type": "image",
            "media_type": "image/png",
            "data": base64.b64encode(b"image bytes").decode(),
            "detail": "high",
        },
    ]

    assert _content_from_pai(_content_to_pai(content)) == content


def test_openai_compat_adapter_emits_nested_image_detail() -> None:
    adapter = OpenAICompatAdapter("key", "https://example.invalid/v1")

    content = adapter._content(
        [
            {
                "type": "image",
                "media_type": "image/png",
                "data": "AA==",
                "detail": "high",
            }
        ]
    )

    assert content == [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,AA==",
                "detail": "high",
            },
        }
    ]


def test_ollama_string_image_url_does_not_receive_unsupported_detail_shape() -> None:
    adapter = OpenAICompatAdapter(
        "key",
        "https://example.invalid/v1",
        image_url_as_string=True,
    )

    content = adapter._content(
        [
            {
                "type": "image",
                "media_type": "image/png",
                "data": "AA==",
                "detail": "high",
            }
        ]
    )

    assert content == [
        {
            "type": "image_url",
            "image_url": "data:image/png;base64,AA==",
        }
    ]

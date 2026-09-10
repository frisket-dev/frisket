from __future__ import annotations

import base64
import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from app import ENGINE, PROMPT, build_vllm_payload, create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _png(color: str, size: tuple[int, int] = (1008, 504)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


async def _one_block(_image_url: str) -> str:
    return json.dumps(
        [{"bbox": [0, 0, 1008, 504], "category": "Text", "text": "hello"}]
    )


def test_health_is_public_but_worker_routes_require_bearer() -> None:
    client = TestClient(create_app(token=TOKEN, inference=_one_block))

    assert client.get("/health").json() == {"ok": True}
    assert (
        client.post(
            "/ocr", files={"files": ("page.png", _png("white"), "image/png")}
        ).status_code
        == 401
    )


def test_vllm_payload_uses_official_prompt_and_sampling_settings() -> None:
    payload = build_vllm_payload("data:image/png;base64,aGVsbG8=")

    assert payload["model"] == ENGINE
    assert payload["temperature"] == 0.1
    assert payload["top_p"] == 0.9
    assert payload["max_completion_tokens"] == 32_768
    content = payload["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["text"] == f"<|img|><|imgpad|><|endofimg|>{PROMPT}"


def test_multipart_pages_preserve_order_and_return_no_fabricated_scores() -> None:
    seen: list[str] = []

    async def infer(image_url: str) -> str:
        raw = base64.b64decode(image_url.partition(",")[2])
        with Image.open(io.BytesIO(raw)) as image:
            color = image.getpixel((0, 0))
        text = "red" if color == (255, 0, 0) else "blue"
        seen.append(text)
        return json.dumps(
            [
                {
                    "bbox": [0, 0, 1008, 504],
                    "category": "Text",
                    "text": text,
                }
            ]
        )

    client = TestClient(create_app(token=TOKEN, inference=infer))
    response = client.post(
        "/ocr",
        headers=AUTH,
        files=[
            ("files", ("red.png", _png("red"), "image/png")),
            ("files", ("blue.png", _png("blue"), "image/png")),
        ],
    )

    assert response.status_code == 200, response.text
    assert seen == ["red", "blue"]
    assert response.json() == {
        "pages": [
            {
                "text": "red",
                "blocks": [
                    {
                        "text": "red",
                        "bbox": [[0, 0], [1008, 0], [1008, 504], [0, 504]],
                    }
                ],
            },
            {
                "text": "blue",
                "blocks": [
                    {
                        "text": "blue",
                        "bbox": [[0, 0], [1008, 0], [1008, 504], [0, 504]],
                    }
                ],
            },
        ]
    }


def test_ocr_rejects_invalid_images() -> None:
    client = TestClient(create_app(token=TOKEN, inference=_one_block))

    assert (
        client.post(
            "/ocr",
            headers=AUTH,
            files={"files": ("bad.png", b"not an image", "image/png")},
        ).status_code
        == 400
    )


def test_malformed_page_falls_back_without_losing_other_pages() -> None:
    calls = 0

    async def inference(_image_url: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "```json\nnot structured\n```"
        return json.dumps(
            [{"bbox": [0, 0, 1008, 504], "category": "Text", "text": "ok"}]
        )

    client = TestClient(create_app(token=TOKEN, inference=inference))
    response = client.post(
        "/ocr",
        headers=AUTH,
        files=[
            ("files", ("first.png", _png("white"), "image/png")),
            ("files", ("second.png", _png("white"), "image/png")),
        ],
    )

    assert response.status_code == 200
    assert response.json()["pages"] == [
        {"text": "not structured", "blocks": []},
        {
            "text": "ok",
            "blocks": [
                {
                    "text": "ok",
                    "bbox": [[0, 0], [1008, 0], [1008, 504], [0, 504]],
                }
            ],
        },
    ]


def test_ocr_requires_at_least_one_multipart_file() -> None:
    client = TestClient(create_app(token=TOKEN, inference=_one_block))
    assert client.post("/ocr", headers=AUTH).status_code == 422

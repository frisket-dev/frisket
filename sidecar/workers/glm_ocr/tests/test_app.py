from __future__ import annotations

import base64
import io

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import ENGINE, build_vllm_payload, create_app, infer_vllm
from frisket_models.glm_ocr_identity import PROFILE

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _png(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 32), color).save(output, format="PNG")
    return output.getvalue()


async def _text(image_url: str) -> str:
    raw = base64.b64decode(image_url.partition(",")[2])
    with Image.open(io.BytesIO(raw)) as image:
        color = image.getpixel((0, 0))
    return "red page" if color[0] > color[2] else "blue page"


def test_health_is_public_but_ocr_requires_bearer() -> None:
    client = TestClient(create_app(token=TOKEN, inference=_text))

    assert client.get("/health").json() == {"ok": True}
    response = client.post(
        "/ocr", files={"files": ("page.png", _png("white"), "image/png")}
    )
    assert response.status_code == 401
    assert (
        client.post(
            "/ocr",
            headers={"Authorization": "Bearer wrong"},
            files={"files": ("page.png", _png("white"), "image/png")},
        ).status_code
        == 403
    )


def test_vllm_payload_uses_official_text_recognition_prompt() -> None:
    payload = build_vllm_payload("data:image/jpeg;base64,aGVsbG8=")

    assert payload == {
        "model": ENGINE,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,aGVsbG8="},
                    },
                    {"type": "text", "text": "Text Recognition:"},
                ],
            }
        ],
        "temperature": 0.0,
        "top_p": 0.00001,
        "top_k": 1,
        "repetition_penalty": 1.1,
        "max_completion_tokens": 8192,
    }
    assert PROFILE.prompt == "Text Recognition:"
    assert PROFILE.image_format == "JPEG"
    assert PROFILE.max_model_tokens == 32768


def test_ocr_preserves_page_order_and_does_not_fabricate_geometry() -> None:
    client = TestClient(create_app(token=TOKEN, inference=_text))
    response = client.post(
        "/ocr",
        headers=AUTH,
        files=[
            ("files", ("red.png", _png("red"), "image/png")),
            ("files", ("blue.png", _png("blue"), "image/png")),
        ],
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "pages": [
            {"text": "red page", "blocks": []},
            {"text": "blue page", "blocks": []},
        ]
    }


def test_ocr_rejects_invalid_images_and_upstream_failure() -> None:
    client = TestClient(create_app(token=TOKEN, inference=_text))
    invalid = client.post(
        "/ocr",
        headers=AUTH,
        files={"files": ("bad.png", b"not an image", "image/png")},
    )
    assert invalid.status_code == 400

    async def fail(_image_url: str) -> str:
        raise RuntimeError("sensitive upstream detail")

    failed = TestClient(create_app(token=TOKEN, inference=fail)).post(
        "/ocr",
        headers=AUTH,
        files={"files": ("page.png", _png("white"), "image/png")},
    )
    assert failed.status_code == 502
    assert failed.json() == {"detail": "GLM-OCR inference failed"}


def test_infer_vllm_requires_non_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"choices": [{"message": {"content": "  "}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    with pytest.raises(RuntimeError, match="GLM-OCR inference failed"):
        import asyncio

        asyncio.run(infer_vllm("data:image/png;base64,aGVsbG8="))

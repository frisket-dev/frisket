"""Authenticated Frisket proxy for a loopback-only dots.mocr vLLM server."""

from __future__ import annotations

import base64
import hmac
import io
import os
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from PIL import Image, UnidentifiedImageError

from frisket_models.dots_mocr_identity import ENGINE
from parser import LayoutOutputError, fallback_text, parse_page

NATIVE_URL = "http://127.0.0.1:8000/v1/chat/completions"

PROMPT = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""

Inference = Callable[[str], Awaitable[str]]


def build_vllm_payload(image_url: str) -> dict[str, Any]:
    """Build the publisher-recommended vLLM request without client magic."""

    return {
        "model": ENGINE,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {
                        "type": "text",
                        "text": f"<|img|><|imgpad|><|endofimg|>{PROMPT}",
                    },
                ],
            }
        ],
        "temperature": 0.1,
        "top_p": 0.9,
        "max_completion_tokens": 32_768,
    }


async def infer_vllm(image_url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=900.0) as client:
            response = await client.post(NATIVE_URL, json=build_vllm_payload(image_url))
            response.raise_for_status()
            body = response.json()
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("completion content is not text")
        return content
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("dots.mocr inference failed") from exc


def _image_data(raw: bytes) -> tuple[str, int, int]:
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            width, height = source.size
            if "transparency" in source.info or source.mode in {"LA", "RGBA"}:
                rgba = source.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                image = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                image = source.convert("RGB")
            encoded = io.BytesIO()
            image.save(encoded, format="PNG")
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ValueError("upload is not a valid image") from exc
    value = base64.b64encode(encoded.getvalue()).decode("ascii")
    return f"data:image/png;base64,{value}", width, height


def _required_token(token: str | None) -> str:
    value = (
        token if token is not None else os.environ.get("FRISKET_OCR_DOTS_WORKER_TOKEN")
    )
    if not value or value != value.strip():
        raise ValueError(
            "FRISKET_OCR_DOTS_WORKER_TOKEN must be a non-empty bearer token"
        )
    return value


def create_app(
    *, token: str | None = None, inference: Inference | None = None
) -> FastAPI:
    resolved_token = _required_token(token)
    run_inference = inference or infer_vllm
    app = FastAPI(title="frisket-dots-mocr-worker")

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    def authorize(request: Request) -> None:
        header = request.headers.get("Authorization")
        if header is None:
            raise HTTPException(status_code=401, detail="missing bearer token")
        if not hmac.compare_digest(header, f"Bearer {resolved_token}"):
            raise HTTPException(status_code=403, detail="invalid bearer token")

    @app.post("/ocr", dependencies=[Depends(authorize)])
    async def ocr(
        files: Annotated[list[UploadFile], File()],
    ) -> dict[str, list[dict[str, Any]]]:
        pages: list[dict[str, Any]] = []
        for upload in files:
            raw = await upload.read()
            try:
                image_url, width, height = _image_data(raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            try:
                output = await run_inference(image_url)
            except RuntimeError:
                raise HTTPException(
                    status_code=502, detail="dots.mocr inference failed"
                ) from None
            try:
                pages.append(parse_page(output, width=width, height=height))
            except LayoutOutputError:
                pages.append({"text": fallback_text(output), "blocks": []})
        return {"pages": pages}

    return app

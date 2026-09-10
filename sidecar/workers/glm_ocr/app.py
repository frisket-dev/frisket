"""Authenticated Frisket proxy for a loopback-only GLM-OCR vLLM server."""

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

from frisket_models.glm_ocr_identity import ENGINE, PROFILE

NATIVE_URL = "http://127.0.0.1:8000/v1/chat/completions"

Inference = Callable[[str], Awaitable[str]]


def build_vllm_payload(image_url: str) -> dict[str, Any]:
    """Build the official lightweight whole-page text-recognition request."""

    return {
        "model": ENGINE,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": PROFILE.prompt},
                ],
            }
        ],
        "temperature": PROFILE.temperature,
        "top_p": PROFILE.top_p,
        "top_k": PROFILE.top_k,
        "repetition_penalty": PROFILE.repetition_penalty,
        "max_completion_tokens": PROFILE.max_completion_tokens,
    }


async def infer_vllm(image_url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=900.0) as client:
            response = await client.post(NATIVE_URL, json=build_vllm_payload(image_url))
            response.raise_for_status()
            body = response.json()
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise TypeError("completion content is not non-empty text")
        return content.strip()
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("GLM-OCR inference failed") from exc


def _image_data(raw: bytes) -> str:
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            if "transparency" in source.info or source.mode in {"LA", "RGBA"}:
                rgba = source.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                image = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                image = source.convert("RGB")
            encoded = io.BytesIO()
            image.save(encoded, format=PROFILE.image_format)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ValueError("upload is not a valid image") from exc
    value = base64.b64encode(encoded.getvalue()).decode("ascii")
    return f"data:{PROFILE.image_mime};base64,{value}"


def _required_token(token: str | None) -> str:
    value = token if token is not None else os.environ.get("FRISKET_MODELS_TOKEN")
    if not value or value != value.strip():
        raise ValueError("FRISKET_MODELS_TOKEN must be a non-empty bearer token")
    return value


def create_app(
    *, token: str | None = None, inference: Inference | None = None
) -> FastAPI:
    resolved_token = _required_token(token)
    run_inference = inference or infer_vllm
    app = FastAPI(title="frisket-glm-ocr-worker")

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
            try:
                image_url = _image_data(await upload.read())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            try:
                text = await run_inference(image_url)
            except RuntimeError:
                raise HTTPException(
                    status_code=502, detail="GLM-OCR inference failed"
                ) from None
            pages.append({"text": text.strip(), "blocks": []})
        return {"pages": pages}

    return app

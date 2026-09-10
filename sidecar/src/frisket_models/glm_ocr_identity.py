"""Immutable identity and inference profile of the hosted GLM-OCR model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GlmOcrProfile:
    engine: str
    model_id: str
    revision: str
    prompt: str
    image_format: str
    image_mime: str
    max_model_tokens: int
    max_completion_tokens: int
    temperature: float
    top_p: float
    top_k: int
    repetition_penalty: float


PROFILE = GlmOcrProfile(
    engine="glm-ocr",
    model_id="zai-org/GLM-OCR",
    revision="ca5d8b3e287e52589e37c28385d9655ee4372f9d",
    prompt="Text Recognition:",
    image_format="JPEG",
    image_mime="image/jpeg",
    max_model_tokens=32768,
    max_completion_tokens=8192,
    temperature=0.0,
    top_p=0.00001,
    top_k=1,
    repetition_penalty=1.1,
)

ENGINE = PROFILE.engine
MODEL_ID = PROFILE.model_id
MODEL_REVISION = PROFILE.revision

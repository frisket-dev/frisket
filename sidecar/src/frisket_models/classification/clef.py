"""Text-only Clef decisions through the publisher's joint schema head."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL_ID = "Cloudflare/clef-flash"
MODEL_REVISION = "17f0b0ad64efb65d273590632833508766b2aae6"
SNAPSHOT_ENV = "FRISKET_MODELS_CLEF_SNAPSHOT"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 65536
MAX_SCHEMA_CHARS = 65536
MAX_TOTAL_OPTIONS = 512
MAX_LENGTH = 16384

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
Description = Annotated[str, Field(max_length=4096)]


class ClefQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["choice", "noul", "score"]
    instructions: Description = ""
    criteria: dict[Identifier, Description] | list[Description] | None = None

    @model_validator(mode="after")
    def valid_criteria(self) -> ClefQuestion:
        if self.type == "noul":
            if self.criteria is not None and (
                not isinstance(self.criteria, dict)
                or not set(self.criteria) <= {"true", "false"}
            ):
                raise ValueError("noul criteria may describe only true and false")
        elif self.type == "choice":
            if (
                not isinstance(self.criteria, dict)
                or not 2 <= len(self.criteria) <= 254
            ):
                raise ValueError("choice requires 2 to 254 named criteria")
        elif not isinstance(self.criteria, list) or not 2 <= len(self.criteria) <= 254:
            raise ValueError("score requires 2 to 254 ordered criteria")
        return self


class ClassifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    engine: Literal["clef-flash"] = "clef-flash"
    text: Annotated[str, Field(min_length=1, max_length=MAX_TEXT_CHARS)]
    questions: Annotated[
        dict[Identifier, ClefQuestion], Field(min_length=1, max_length=64)
    ]

    @model_validator(mode="after")
    def bounded_schema(self) -> ClassifyBody:
        chars = 0
        options = 0
        for name, question in self.questions.items():
            chars += len(name) + len(question.instructions)
            criteria = question.criteria
            options += 2 if question.type == "noul" else len(criteria or [])
            if isinstance(criteria, dict):
                chars += sum(len(k) + len(v) for k, v in criteria.items())
            elif isinstance(criteria, list):
                chars += sum(map(len, criteria))
        if chars > MAX_SCHEMA_CHARS or options > MAX_TOTAL_OPTIONS:
            raise ValueError("classification schema exceeds character or option limit")
        return self


def load_clef_flash() -> Any:
    """Fetch pinned model data on first use, then load the bundled decision head."""
    device = os.environ.get("FRISKET_MODELS_CLEF_DEVICE", "cuda")
    if device not in {"cuda", "cpu"}:
        raise RuntimeError("FRISKET_MODELS_CLEF_DEVICE must be cuda or cpu")
    configured = os.environ.get(SNAPSHOT_ENV)
    if configured:
        snapshot = Path(configured).expanduser()
    else:
        try:
            from huggingface_hub import snapshot_download

            snapshot = Path(
                snapshot_download(
                    MODEL_ID,
                    revision=MODEL_REVISION,
                    allow_patterns=["*.json", "*.safetensors", "chat_template.jinja"],
                    token=False,
                )
            )
        except Exception:
            raise RuntimeError(
                "Clef could not download its pinned model snapshot"
            ) from None
    if not snapshot.is_dir() or snapshot.name != MODEL_REVISION:
        raise RuntimeError(
            f"{SNAPSHOT_ENV} must name the pinned Clef snapshot directory"
        )
    required = (
        "config.json",
        "model.safetensors.index.json",
        "joint_head.safetensors",
        "joint_head_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "processor_config.json",
    )
    if any(not (snapshot / name).is_file() for name in required):
        raise RuntimeError(f"{SNAPSHOT_ENV} is missing required model artifacts")
    try:
        from ._clef_inference import load_release_model, systemone

        model, processor = load_release_model(snapshot, device=device)
    except Exception:
        # Registry retains loader errors in capabilities. Do not publish paths,
        # upstream exceptions, request text, or environment credentials there.
        raise RuntimeError("Clef could not load its local model snapshot") from None

    def classify(text: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        request = {"model": "clef-flash", "state": text, "questions": questions}
        return systemone(model, processor, request, max_length=MAX_LENGTH)

    return classify

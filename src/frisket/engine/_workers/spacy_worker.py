"""Offline spaCy worker sharing one loaded pipeline across a single action."""

from __future__ import annotations

from typing import Any

from frisket.engine._workers.framing import FrameCodec, WorkerExit, serve
from frisket.engine._workers.spacy_entities import extract_entities

CODEC = FrameCodec(4 * 1024 * 1024, 4 * 1024 * 1024, ValueError)


def _load(request: dict[str, Any]) -> Any:
    if request.get("type") != "init":
        raise WorkerExit
    try:
        import spacy

        return spacy.load(request["model_path"])
    except Exception:
        raise WorkerExit(
            {
                "type": "error",
                "message": "The spaCy model could not load. Retry setup and check available memory.",
            }
        ) from None


def _extract(pipeline: Any, request: dict[str, Any]) -> dict[str, Any]:
    if request.get("type") != "extract":
        raise WorkerExit
    text, labels = request.get("text"), request.get("labels")
    if (
        not isinstance(text, str)
        or not isinstance(labels, list)
        or not labels
        or any(not isinstance(label, str) or not label.strip() for label in labels)
    ):
        raise WorkerExit
    try:
        entities = extract_entities(pipeline, [text], set(labels))[0]
    except Exception:
        raise WorkerExit(
            {"type": "error", "message": "spaCy could not process this row."}
        ) from None
    return {"type": "result", "entities": entities}


def framed_main() -> int:
    return serve(
        CODEC,
        load=_load,
        handle=_extract,
        ready={"type": "ready"},
        close={"type": "close"},
    )

"""Fixed, offline classifier entrypoint; heavy packages live in the private runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.engine._workers.framing import FrameCodec, WorkerExit, serve

CODEC = FrameCodec(4 * 1024 * 1024, 8192, ValueError)


def _load(request: dict[str, Any]) -> Any:
    from frisket_models.classification import GLiClassBase, Jeff

    if request.get("type") != "init":
        raise WorkerExit
    adapters = {"gliclass": GLiClassBase, "jeff": Jeff}
    try:
        return adapters[request["engine"]](Path(request["snapshot"]))
    except Exception:
        raise WorkerExit(
            {
                "type": "error",
                "code": "model_unavailable",
                "message": "The classifier could not load. Check available memory and reinstall the model if needed.",
            }
        ) from None


def _classify(adapter: Any, request: dict[str, Any]) -> dict[str, Any]:
    if request.get("type") != "classify":
        raise WorkerExit
    try:
        result = adapter.classify(
            request["text"],
            request["labels"],
            descriptions=request["descriptions"],
            instruction=request["instruction"],
        )
    except ValueError as exc:
        # Owned adapters emit short, content-free validation messages.
        return {"type": "error", "code": "classify_invalid_input", "message": str(exc)}
    except Exception:
        raise WorkerExit(
            {
                "type": "error",
                "code": "classify_failed",
                "message": "The local classifier could not process this row.",
            }
        ) from None
    return {"type": "result", **result}


def framed_main() -> int:
    return serve(
        CODEC,
        load=_load,
        handle=_classify,
        ready={"type": "ready"},
        close={"type": "close"},
    )

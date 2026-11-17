"""Offline, framed Opus-MT worker; pair provisioning stays in the host."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.engine._workers import opus_mt_inference
from frisket.engine._workers.framing import FrameCodec, WorkerExit, serve

MAX_FRAME_BYTES = 4 * 1024 * 1024
CODEC = FrameCodec(MAX_FRAME_BYTES, MAX_FRAME_BYTES, ValueError)


def _load(request: dict[str, Any]) -> tuple[Path, Any]:
    import ctranslate2

    if set(request) != {"type", "directory"} or request["type"] != "init":
        raise WorkerExit
    directory = Path(request["directory"])
    if not directory.is_absolute() or not all(
        (directory / name).is_file()
        for name in ("model.bin", "source.spm", "target.spm")
    ):
        raise WorkerExit({"type": "error"})
    try:
        translator = ctranslate2.Translator(
            str(directory), device="cpu", compute_type="int8"
        )
    except Exception:
        raise WorkerExit({"type": "error"}) from None
    return directory, translator


def _translate(state: tuple[Path, Any], request: dict[str, Any]) -> dict[str, Any]:
    if set(request) != {"type", "texts"} or request["type"] != "translate":
        raise WorkerExit
    texts = request["texts"]
    if (
        not isinstance(texts, list)
        or not texts
        or not all(isinstance(text, str) for text in texts)
    ):
        raise WorkerExit
    directory, translator = state
    try:
        results = opus_mt_inference.translate_directory(directory, texts, translator)
    except Exception:
        raise WorkerExit({"type": "error"}) from None
    return {"type": "result", "texts": results}


def framed_main() -> int:
    return serve(
        CODEC,
        load=_load,
        handle=_translate,
        ready={"type": "ready"},
        close={"type": "close"},
    )

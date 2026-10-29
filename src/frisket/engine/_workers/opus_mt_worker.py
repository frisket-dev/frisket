"""Offline, framed Opus-MT worker; pair provisioning stays in the host."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from frisket.engine._workers.framing import FrameCodec

MAX_FRAME_BYTES = 4 * 1024 * 1024
CODEC = FrameCodec(MAX_FRAME_BYTES, MAX_FRAME_BYTES, ValueError)


def framed_main() -> int:
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    with contextlib.redirect_stdout(sys.stderr):
        import ctranslate2

        from frisket.engine._workers.opus_mt_inference import translate_directory

        request = CODEC.read_frame(source)
        if (
            request is None
            or set(request) != {"type", "directory"}
            or request["type"] != "init"
        ):
            return 1
        directory = Path(request["directory"])
        if not directory.is_absolute() or not all(
            (directory / name).is_file()
            for name in ("model.bin", "source.spm", "target.spm")
        ):
            CODEC.write_frame(sink, {"type": "error"})
            return 1
        try:
            translator = ctranslate2.Translator(
                str(directory), device="cpu", compute_type="int8"
            )
        except Exception:
            CODEC.write_frame(sink, {"type": "error"})
            return 1
        CODEC.write_frame(sink, {"type": "ready"})
        while (request := CODEC.read_frame(source)) is not None:
            if request == {"type": "close"}:
                return 0
            if set(request) != {"type", "texts"} or request["type"] != "translate":
                return 1
            texts = request["texts"]
            if (
                not isinstance(texts, list)
                or not texts
                or not all(isinstance(text, str) for text in texts)
            ):
                return 1
            try:
                results = translate_directory(directory, texts, translator)
            except Exception:
                CODEC.write_frame(sink, {"type": "error"})
                return 1
            CODEC.write_frame(sink, {"type": "result", "texts": results})
    return 0

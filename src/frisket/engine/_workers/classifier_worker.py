"""Fixed, offline classifier entrypoint; heavy packages live in the private runtime."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from frisket.engine._workers.framing import FrameCodec

CODEC = FrameCodec(4 * 1024 * 1024, 8192, ValueError)


def framed_main() -> int:
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    # Model libraries sometimes print diagnostics. stdout belongs to framing.
    with contextlib.redirect_stdout(sys.stderr):
        from frisket_models.classification import GLiClassBase, Jeff

        request = CODEC.read_frame(source)
        if request is None or request.get("type") != "init":
            return 1
        adapters = {"gliclass": GLiClassBase, "jeff": Jeff}
        try:
            adapter = adapters[request["engine"]](Path(request["snapshot"]))
        except Exception:
            CODEC.write_frame(
                sink,
                {
                    "type": "error",
                    "code": "model_unavailable",
                    "message": "The classifier could not load. Check available memory and reinstall the model if needed.",
                },
            )
            return 1
        CODEC.write_frame(sink, {"type": "ready"})
        while (request := CODEC.read_frame(source)) is not None:
            if request.get("type") == "close":
                return 0
            if request.get("type") != "classify":
                return 1
            try:
                result = adapter.classify(
                    request["text"],
                    request["labels"],
                    descriptions=request["descriptions"],
                    instruction=request["instruction"],
                )
            except ValueError as exc:
                # Owned adapters emit short, content-free validation messages.
                CODEC.write_frame(
                    sink,
                    {
                        "type": "error",
                        "code": "classify_invalid_input",
                        "message": str(exc),
                    },
                )
            except Exception:
                CODEC.write_frame(
                    sink,
                    {
                        "type": "error",
                        "code": "classify_failed",
                        "message": "The local classifier could not process this row.",
                    },
                )
                return 1
            else:
                CODEC.write_frame(sink, {"type": "result", **result})
    return 0

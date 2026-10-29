"""Offline spaCy worker sharing one loaded pipeline across a single action."""

from __future__ import annotations

import contextlib
import sys

from frisket.engine._workers.framing import FrameCodec

CODEC = FrameCodec(4 * 1024 * 1024, 4 * 1024 * 1024, ValueError)


def framed_main() -> int:
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    with contextlib.redirect_stdout(sys.stderr):
        from frisket.engine._workers.spacy_entities import extract_entities

        request = CODEC.read_frame(source)
        if request is None or request.get("type") != "init":
            return 1
        try:
            import spacy

            pipeline = spacy.load(request["model_path"])
        except Exception:
            CODEC.write_frame(
                sink,
                {
                    "type": "error",
                    "message": "The spaCy model could not load. Retry setup and check available memory.",
                },
            )
            return 1
        CODEC.write_frame(sink, {"type": "ready"})
        while (request := CODEC.read_frame(source)) is not None:
            if request.get("type") == "close":
                return 0
            if request.get("type") != "extract":
                return 1
            text, labels = request.get("text"), request.get("labels")
            if (
                not isinstance(text, str)
                or not isinstance(labels, list)
                or not labels
                or any(
                    not isinstance(label, str) or not label.strip() for label in labels
                )
            ):
                return 1
            try:
                entities = extract_entities(pipeline, [text], set(labels))[0]
            except Exception:
                CODEC.write_frame(
                    sink,
                    {"type": "error", "message": "spaCy could not process this row."},
                )
                return 1
            CODEC.write_frame(sink, {"type": "result", "entities": entities})
    return 0

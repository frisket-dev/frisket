"""Length-prefixed finite-JSON framing shared by resident local workers.

Child workers import this module before their model libraries, so it must stay
standard-library only.
"""

from __future__ import annotations

import contextlib
import json
import os
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO


def encode_frame(frame: dict[str, Any]) -> bytes:
    """Encode a protocol frame as compact UTF-8 JSON, rejecting non-finite."""

    return json.dumps(
        frame, allow_nan=False, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def reject_constant(value: str) -> None:
    """``json.loads`` ``parse_constant`` hook that bars ``NaN``/``Infinity``."""

    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def protocol_stdout() -> BinaryIO:
    """Reserve the original stdout pipe and redirect ordinary stdout to stderr."""

    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return os.fdopen(protocol_fd, "wb", buffering=0)


@dataclass(frozen=True)
class FrameCodec:
    """Preserve the workers' small, bounded stdin/stdout frame contract."""

    max_request_bytes: int
    max_response_bytes: int
    error_type: type[Exception]

    def _error(self, message: str) -> None:
        raise self.error_type(message)

    def _decode_object(self, data: bytes) -> dict[str, Any]:
        try:
            value = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise self.error_type("frame is not valid finite UTF-8 JSON") from error
        if not isinstance(value, dict):
            self._error("frame must be a JSON object")
        return value

    def _read_exact(self, stream: BinaryIO, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                self._error("unexpected EOF inside a frame")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_frame(self, stream: BinaryIO) -> dict[str, Any] | None:
        prefix = stream.read(4)
        if not prefix:
            return None
        if len(prefix) != 4:
            self._error("unexpected EOF inside a frame prefix")
        (size,) = struct.unpack(">I", prefix)
        if size > self.max_request_bytes:
            self._error("request frame exceeds the allowed size")
        return self._decode_object(self._read_exact(stream, size))

    def write_frame(self, stream: BinaryIO, value: dict[str, Any]) -> None:
        payload = encode_frame(value)
        if len(payload) > self.max_response_bytes:
            self._error("response frame exceeds the allowed size")
        stream.write(struct.pack(">I", len(payload)))
        stream.write(payload)
        stream.flush()


class WorkerExit(Exception):
    """Stop a served worker with status 1, writing ``frame`` first when given."""

    def __init__(self, frame: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.frame = frame


def serve[S](
    codec: FrameCodec,
    *,
    load: Callable[[dict[str, Any]], S],
    handle: Callable[[S, dict[str, Any]], dict[str, Any]],
    ready: dict[str, Any],
    close: dict[str, Any],
) -> int:
    """Run one worker's init/ready handshake and its one-at-a-time request loop.

    ``load`` validates the init frame and returns the loaded state; ``handle``
    answers one request with its response frame. Either raises ``WorkerExit``
    to end the process. Exactly ``close`` (or EOF) ends it with status 0.
    Library output on ordinary stdout is redirected to stderr throughout.
    """

    source, sink = sys.stdin.buffer, sys.stdout.buffer
    with contextlib.redirect_stdout(sys.stderr):
        init = codec.read_frame(source)
        if init is None:
            return 1
        try:
            state = load(init)
            codec.write_frame(sink, ready)
            while (request := codec.read_frame(source)) is not None:
                if request == close:
                    return 0
                codec.write_frame(sink, handle(state, request))
        except WorkerExit as stop:
            if stop.frame is not None:
                codec.write_frame(sink, stop.frame)
            return 1
    return 0

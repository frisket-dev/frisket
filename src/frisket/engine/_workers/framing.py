"""Length-prefixed finite-JSON framing shared by resident local workers."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, BinaryIO


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


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
            value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise self.error_type("frame is not valid finite UTF-8 JSON") from error
        if not isinstance(value, dict):
            self._error("frame must be a JSON object")
        return value

    def _encode_object(self, value: dict[str, Any]) -> bytes:
        return json.dumps(
            value, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

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
        payload = self._encode_object(value)
        if len(payload) > self.max_response_bytes:
            self._error("response frame exceeds the allowed size")
        stream.write(struct.pack(">I", len(payload)))
        stream.write(payload)
        stream.flush()

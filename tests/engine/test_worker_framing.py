from __future__ import annotations

import io
import struct

import pytest

from frisket.engine._workers.framing import FrameCodec


class _ProtocolError(RuntimeError):
    pass


_CODEC = FrameCodec(
    max_request_bytes=16,
    max_response_bytes=16,
    error_type=_ProtocolError,
)


class _PayloadFragmentStream:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)
        self._first_read = True

    def read(self, size: int = -1) -> bytes:
        if self._first_read:
            self._first_read = False
            return self._stream.read(size)
        return self._stream.read(min(size, 1))


def test_frame_codec_reassembles_fragmented_payload_and_rejects_nan() -> None:
    payload = b'{"kind":"frame"}'
    stream = _PayloadFragmentStream(struct.pack(">I", len(payload)) + payload)
    assert _CODEC.read_frame(stream) == {"kind": "frame"}

    non_finite = b'{"number":NaN}'
    with pytest.raises(_ProtocolError, match="finite UTF-8 JSON"):
        _CODEC.read_frame(io.BytesIO(struct.pack(">I", len(non_finite)) + non_finite))


def test_frame_codec_keeps_prefix_and_response_bounds() -> None:
    with pytest.raises(_ProtocolError, match="frame prefix"):
        _CODEC.read_frame(io.BytesIO(b"\x00\x00"))

    with pytest.raises(_ProtocolError, match="response frame exceeds"):
        _CODEC.write_frame(io.BytesIO(), {"value": "x" * 16})

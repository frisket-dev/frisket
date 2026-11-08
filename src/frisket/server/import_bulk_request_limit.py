"""ASGI request-envelope guard for multipart import endpoints."""

from __future__ import annotations

import errno
import json
import tempfile

from starlette.formparsers import MultiPartException, MultiPartParser

from frisket.engine.store.disk_capacity import require_disk_headroom

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_TOO_LARGE_BODIES = {
    "bulk_plan": json.dumps(
        {"detail": "bulk import request exceeds deployment limit"},
        separators=(",", ":"),
    ).encode(),
    "csv_import": json.dumps(
        {"detail": "CSV import request exceeds deployment limit"},
        separators=(",", ":"),
    ).encode(),
    "csv_preview": json.dumps(
        {"detail": "CSV import preview request exceeds deployment limit"},
        separators=(",", ":"),
    ).encode(),
    "import": json.dumps(
        {"detail": "import request exceeds deployment limit"},
        separators=(",", ":"),
    ).encode(),
}


_DISK_FULL_BODY = json.dumps(
    {"detail": "Insufficient temporary disk space to receive this upload."},
    separators=(",", ":"),
).encode()


class _RequestTooLarge(MultiPartException):
    pass


class _InsufficientDisk(MultiPartException):
    pass


def _check_disk(amount: int) -> None:
    try:
        require_disk_headroom(tempfile.gettempdir(), amount)
    except OSError as exc:
        if exc.errno != errno.ENOSPC:
            raise
        raise _InsufficientDisk("Insufficient temporary disk space") from exc


class BulkImportRequestLimitMiddleware:
    """Bound raw import envelopes before FastAPI parses multipart bodies.

    ``max_upload_bytes`` is enforced later on decoded file content. This guard
    deliberately covers the separate request-body quota, including multipart
    field metadata and requests without a Content-Length header.
    """

    def __init__(self, app: ASGIApp, *, max_request_bytes: int) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_kind = _limited_import_request_kind(scope)
        if request_kind is None:
            await self.app(scope, receive, send)
            return
        too_large_body = _TOO_LARGE_BODIES[request_kind]

        expected = _content_length(scope)
        if expected is not None and expected > self.max_request_bytes:
            await _send_too_large(send, body=too_large_body)
            return

        multipart = any(
            name.lower() == b"content-type"
            and value.split(b";", 1)[0].strip().lower() == b"multipart/form-data"
            for name, value in scope.get("headers", [])
        )
        if multipart and expected is not None:
            try:
                _check_disk(expected)
            except _InsufficientDisk:
                await _send_error(send, status=507, body=_DISK_FULL_BODY)
                return

        received = 0
        exceeded = False
        disk_full = False
        limit_sent = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded, disk_full
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_request_bytes:
                    exceeded = True
                    raise _RequestTooLarge("Import request exceeds deployment limit")
                if multipart:
                    try:
                        _check_disk(
                            len(message.get("body", b""))
                            + MultiPartParser.spool_max_size
                        )
                    except _InsufficientDisk:
                        disk_full = True
                        raise
            return message

        async def limited_send(message: Message) -> None:
            nonlocal limit_sent
            if exceeded or disk_full:
                # FastAPI converts request-form failures to a 400 itself. Do
                # not let that hide the quota result; replace its whole
                # response with the stable 413 payload.
                if message["type"] == "http.response.start" and not limit_sent:
                    if disk_full:
                        await _send_error(send, status=507, body=_DISK_FULL_BODY)
                    else:
                        await _send_too_large(send, body=too_large_body)
                    limit_sent = True
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except (_RequestTooLarge, _InsufficientDisk):
            if not limit_sent:
                if disk_full:
                    await _send_error(send, status=507, body=_DISK_FULL_BODY)
                else:
                    await _send_too_large(send, body=too_large_body)


def _limited_import_request_kind(scope: Scope) -> str | None:
    if scope["method"] != "POST":
        return None
    segments = scope["path"].split("/")
    if (
        len(segments) >= 6
        and segments[1:3] == ["api", "projects"]
        and bool(segments[3])
        and segments[4] == "import"
    ):
        request_kind = "import"
    else:
        return None
    if (
        len(segments) == 7
        and segments[1:3] == ["api", "projects"]
        and bool(segments[3])
        and segments[4:] == ["import", "bulk", "plan"]
    ):
        return "bulk_plan"
    if (
        len(segments) == 6
        and segments[1:3] == ["api", "projects"]
        and bool(segments[3])
        and segments[4:] == ["import", "csv"]
    ):
        return "csv_import"
    if (
        len(segments) == 7
        and segments[1:3] == ["api", "projects"]
        and bool(segments[3])
        and segments[4:] == ["import", "csv", "preview"]
    ):
        return "csv_preview"
    return request_kind


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        try:
            length = int(value)
        except ValueError:
            return None
        return length if length >= 0 else None
    return None


async def _send_too_large(send: Send, *, body: bytes) -> None:
    await _send_error(send, status=413, body=body)


async def _send_error(send: Send, *, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = ["BulkImportRequestLimitMiddleware"]

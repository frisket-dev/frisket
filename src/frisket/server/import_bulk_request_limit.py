"""ASGI request-envelope guard for multipart import endpoints."""

from __future__ import annotations

import json

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


class _RequestTooLarge(Exception):
    pass


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

        received = 0
        exceeded = False
        limit_sent = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_request_bytes:
                    exceeded = True
                    raise _RequestTooLarge
            return message

        async def limited_send(message: Message) -> None:
            nonlocal limit_sent
            if exceeded:
                # FastAPI converts request-form failures to a 400 itself. Do
                # not let that hide the quota result; replace its whole
                # response with the stable 413 payload.
                if message["type"] == "http.response.start" and not limit_sent:
                    await _send_too_large(send, body=too_large_body)
                    limit_sent = True
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _RequestTooLarge:
            if not limit_sent:
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
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = ["BulkImportRequestLimitMiddleware"]

"""Credit-free observability and diagnostic routes for the open team app."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from threading import Lock
from typing import Any

import sqlalchemy as sa
from fastapi import Body, FastAPI, HTTPException, Request

from frisket.contracts.http.admin_overview import AdminOverviewResponse
from frisket.contracts.http.error_intake import DiagnosticBundleResult
from frisket.server.route_errors import http_error_responses
from frisket.team.diagnostics import sanitize_text
from frisket.team.schema import (
    DEFAULT_DIAGNOSTIC_REPORT_RETENTION_DAYS,
    DEFAULT_DIAGNOSTIC_REPORT_RETENTION_MAX_ROWS,
    audit_log,
    client_errors,
    orgs,
    pending_invites,
    projects,
    users,
)


_DIAGNOSTIC_BODY_MAX_BYTES = 64 * 1024
_DIAGNOSTIC_RATE_LIMIT = 20
_DIAGNOSTIC_RATE_WINDOW_SECONDS = 60


_RAW_CONTENT_KEYS = frozenset(
    {
        "body",
        "cell",
        "cellvalue",
        "cells",
        "cellvalues",
        "csv",
        "data",
        "file",
        "filecontents",
        "input",
        "messages",
        "output",
        "prompt",
        "prompts",
        "prompttext",
        "raw",
        "rawcell",
        "rawcells",
        "rawoutput",
        "request",
        "requestbody",
        "response",
        "responsebody",
        "rows",
        "value",
        "values",
    }
)
_SECRET_KEY_TOKENS = frozenset(
    {
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "secret",
        "session",
        "token",
    }
)


def _normalized_key(value: Any) -> str:
    """Return a separator/case-neutral diagnostic field name."""
    return "".join(character for character in str(value).lower() if character.isalnum())


def _excluded_diagnostic_key(key: str) -> bool:
    """Whether a client field is unrepresentable in the open diagnostic schema."""
    normalized = _normalized_key(key)
    return normalized in _RAW_CONTENT_KEYS or any(
        token in normalized for token in _SECRET_KEY_TOKENS
    )


class _DiagnosticBodyLimitMiddleware:
    """Reject oversize diagnostic payloads before FastAPI parses their body."""

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("path") != "/api/diagnostic-bundle"
            or scope.get("method") != "POST"
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", ()))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > self.max_bytes
            except ValueError:
                too_large = False
            if too_large:
                await self._payload_too_large(send)
                return

        messages: deque[dict[str, Any]] = deque()
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                messages.append(message)
                break
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                await self._payload_too_large(send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay_receive() -> dict[str, Any]:
            if messages:
                return messages.popleft()
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    async def _payload_too_large(
        self, send: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        payload = json.dumps(
            {"detail": "diagnostic bundle exceeds the 65536-byte limit"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def sanitize_client_tree(value: Any) -> Any:
    """Bounded client-data sanitizer, shared with the client-errors intake:
    the same drop-credential-branches/depth-cap projection the diagnostic
    bundle applies, so both intake wires sanitize context identically."""

    return _sanitize(value)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """Project arbitrary client data into a small, JSON-safe redacted tree."""
    if depth >= 5:
        return "[truncated]"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, raw_value in islice(value.items(), 100):
            key = sanitize_text(raw_key, limit=120)
            if _excluded_diagnostic_key(key):
                # The public diagnostic schema carries shape/count facts, never
                # client content or credential-bearing fields.  Drop these
                # branches entirely so their values cannot leak through either
                # the response or the persisted JSON representation.
                continue
            out[key] = _sanitize(raw_value, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, depth=depth + 1) for item in islice(value, 100)]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_text(value, limit=1000)


def _blob_probe(root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    root.mkdir(parents=True, exist_ok=True)
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=root, prefix=".health-", delete=False
        ) as fh:
            path = Path(fh.name)
            fh.write(b"frisket-health")
            fh.flush()
        read_ok = path.read_bytes() == b"frisket-health"
        path.unlink()
        delete_ok = not path.exists()
        path = None
        disk = shutil.disk_usage(root)
        return {
            "ok": bool(read_ok and delete_ok),
            "write": True,
            "read": read_ok,
            "delete": delete_ok,
            "free_bytes": disk.free,
            "total_bytes": disk.total,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except OSError:
        return {
            "ok": False,
            "write": False,
            "read": False,
            "delete": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def register_observability_routes(
    app: FastAPI,
    *,
    engine: sa.Engine,
    org_id: int,
    require_user: Callable[..., dict[str, Any]],
    run_queue: Any,
    workspace: Any,
) -> None:
    """Register S2-D routes; coordinator wires this registrar into the app."""

    app.add_middleware(
        _DiagnosticBodyLimitMiddleware, max_bytes=_DIAGNOSTIC_BODY_MAX_BYTES
    )
    rate_windows: dict[int, deque[float]] = {}
    rate_lock = Lock()

    def enforce_diagnostic_rate_limit(actor_id: int) -> None:
        now = time.monotonic()
        cutoff = now - _DIAGNOSTIC_RATE_WINDOW_SECONDS
        with rate_lock:
            window = rate_windows.setdefault(actor_id, deque())
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= _DIAGNOSTIC_RATE_LIMIT:
                raise HTTPException(
                    status_code=429,
                    detail="diagnostic report rate limit exceeded",
                    headers={"Retry-After": str(_DIAGNOSTIC_RATE_WINDOW_SECONDS)},
                )
            window.append(now)

    def retain_diagnostic_reports(cx: sa.Connection, *, report_id: int) -> None:
        """Purge expired reports and oldest excess rows without evicting this one."""
        cutoff = datetime.now(UTC) - timedelta(
            days=DEFAULT_DIAGNOSTIC_REPORT_RETENTION_DAYS
        )
        report_filter = (
            client_errors.c.source == "diagnostic_report",
            client_errors.c.org_id == org_id,
        )
        cx.execute(
            client_errors.delete().where(
                *report_filter,
                client_errors.c.created_at < cutoff,
                client_errors.c.id != report_id,
            )
        )
        total = int(
            cx.execute(
                sa.select(sa.func.count())
                .select_from(client_errors)
                .where(*report_filter)
            ).scalar_one()
        )
        excess = total - DEFAULT_DIAGNOSTIC_REPORT_RETENTION_MAX_ROWS
        if excess <= 0:
            return
        expired_ids = cx.execute(
            sa.select(client_errors.c.id)
            .where(*report_filter, client_errors.c.id != report_id)
            .order_by(client_errors.c.created_at, client_errors.c.id)
            .limit(excess)
        ).scalars()
        ids = list(expired_ids)
        if ids:
            cx.execute(client_errors.delete().where(client_errors.c.id.in_(ids)))

    @app.get("/api/admin/health")
    def admin_health(request: Request) -> dict[str, Any]:
        require_user(request, browser=True, admin=True)
        started = time.perf_counter()
        try:
            with engine.connect() as cx:
                cx.execute(sa.text("SELECT 1"))
            db = {
                "ok": True,
                "dialect": engine.dialect.name,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        except Exception:  # noqa: BLE001
            db = {"ok": False, "dialect": engine.dialect.name}
        try:
            counts = dict(run_queue.counts())
            active_runs = int(counts.get("running", 0))
            queue = {"ok": True, "counts": counts}
        except Exception:  # noqa: BLE001
            active_runs, queue = 0, {"ok": False, "counts": {}}
        blob = _blob_probe(Path(workspace.root))
        return {
            "ok": bool(db["ok"] and blob["ok"] and queue["ok"]),
            "db": db,
            "blob_store": blob,
            "active_runs": active_runs,
            "queue": queue,
        }

    @app.get(
        "/api/admin/overview",
        response_model=AdminOverviewResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 500),
    )
    def admin_overview(request: Request) -> dict[str, Any]:
        require_user(request, browser=True, admin=True)
        with engine.connect() as cx:
            totals = {
                "orgs": int(
                    cx.execute(
                        sa.select(sa.func.count()).select_from(orgs)
                    ).scalar_one()
                ),
                "users": int(
                    cx.execute(
                        sa.select(sa.func.count()).select_from(users)
                    ).scalar_one()
                ),
                "projects": int(
                    cx.execute(
                        sa.select(sa.func.count())
                        .select_from(projects)
                        .where(projects.c.org_id == org_id)
                    ).scalar_one()
                ),
                "pending_invites": int(
                    cx.execute(
                        sa.select(sa.func.count())
                        .select_from(pending_invites)
                        .where(pending_invites.c.org_id == org_id)
                    ).scalar_one()
                ),
            }
        return {"totals": totals}

    @app.post(
        "/api/diagnostic-bundle",
        response_model=DiagnosticBundleResult,
        response_model_exclude_unset=True,
    )
    def diagnostic_bundle(
        request: Request, body: dict[str, Any] = Body(default_factory=dict)
    ) -> DiagnosticBundleResult:
        actor = require_user(request)
        enforce_diagnostic_rate_limit(int(actor["id"]))
        message = sanitize_text(
            body.get("message", "diagnostic report")
            if isinstance(body, dict)
            else "diagnostic report",
            limit=1000,
        )
        bundle = _sanitize(body)
        if not isinstance(bundle, dict):
            bundle = {"value": bundle}
        # ``message`` is the single deliberately retained human-readable field.
        # Always replace the client representation with its bounded sanitizer
        # result, even if the client supplied an odd nested shape.
        bundle["message"] = message
        bundle["include_raw_values"] = False
        encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
        with engine.begin() as cx:
            result = cx.execute(
                client_errors.insert().values(
                    org_id=org_id,
                    user_id=int(actor["id"]),
                    source="diagnostic_report",
                    message=message,
                    bundle_json=encoded,
                    context_json="{}",
                )
            )
            report_id = int(result.inserted_primary_key[0])
            retain_diagnostic_reports(cx, report_id=report_id)
            cx.execute(
                audit_log.insert().values(
                    user_id=int(actor["id"]),
                    org_id=org_id,
                    action="diagnostic_report_created",
                    detail=str(report_id),
                )
            )
        return DiagnosticBundleResult.model_validate(
            {"ok": True, "report_id": report_id, "bundle": bundle}
        )


__all__ = ["register_observability_routes"]

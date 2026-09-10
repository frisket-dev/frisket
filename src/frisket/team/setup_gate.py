"""Early default-deny gate for an unclaimed server."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.responses import JSONResponse


class SetupGate:
    def __init__(
        self,
        app: Any,
        *,
        claimed: Callable[[], bool],
        operator_probe: Callable[[str], bool] | None = None,
    ):
        self.app = app
        self.claimed = claimed
        # Optional pre-claim pass-through for the operator-token admin API:
        # the installer mints the initial operator token before any human has
        # claimed /setup, and `frisket remote link`/`users add` must work in
        # that window. The probe authenticates the Authorization header by
        # stored-hash comparison; only /api/admin/* is ever admitted this way.
        self.operator_probe = operator_probe

    def _operator_allowed(self, scope: Any) -> bool:
        if self.operator_probe is None:
            return False
        if not str(scope.get("path") or "").startswith("/api/admin/"):
            return False
        authorization = ""
        for name, value in scope.get("headers") or ():
            if name == b"authorization":
                authorization = value.decode("latin-1")
        return bool(authorization) and self.operator_probe(authorization)

    async def __call__(self, scope, receive, send) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope_type == "http":
            path = str(scope.get("path") or "")
            method = str(scope.get("method") or "GET").upper()
            # Liveness must stay shallow when the control database is down;
            # readiness owns the sanitized dependency checks. Setup itself
            # rechecks owner state transactionally in its route handlers.
            if method == "GET" and path in {"/api/health", "/api/ready"}:
                await self.app(scope, receive, send)
                return
            if path == "/setup" and method in {"GET", "POST"}:
                await self.app(scope, receive, send)
                return
        if self.claimed():
            await self.app(scope, receive, send)
            return
        if scope_type == "http" and self._operator_allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope_type != "http":
            return
        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET").upper()
        allowed = path.startswith("/setup-assets/") and method == "GET"
        if allowed:
            await self.app(scope, receive, send)
            return
        await JSONResponse(
            {"detail": "server setup is required"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)

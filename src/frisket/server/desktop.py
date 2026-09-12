"""Desktop-owned local server composition.

The Electron main process provides one launch record on stdin and receives one
readiness record on stdout.  Everything else, including the queue worker, uses
stderr so the readiness channel cannot be confused with application output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
from typing import BinaryIO, Callable, TextIO

from starlette.types import ASGIApp, Receive, Scope, Send


_CONFIG_LIMIT_BYTES = 4096
_TOKEN_HEADER = b"x-frisket-desktop-token"
_TOKEN_RE = re.compile(r"[0-9a-f]{64}")
_UNAUTHORIZED_BODY = b'{"detail":"Unauthorized"}'


class DesktopLaunchError(ValueError):
    """A deliberately detail-free failure at the desktop process boundary."""


@dataclass(frozen=True)
class DesktopLaunchConfig:
    workspace: Path
    token: str = field(repr=False)


def read_launch_config(stream: BinaryIO) -> DesktopLaunchConfig:
    """Read the sole bounded launch record before any filesystem access."""
    raw = stream.read(_CONFIG_LIMIT_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > _CONFIG_LIMIT_BYTES:
        raise DesktopLaunchError("invalid desktop launch configuration")
    try:
        value = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DesktopLaunchError("invalid desktop launch configuration") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "workspace", "token"}:
        raise DesktopLaunchError("invalid desktop launch configuration")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise DesktopLaunchError("invalid desktop launch configuration")
    workspace = value.get("workspace")
    token = value.get("token")
    if (
        not isinstance(workspace, str)
        or "\x00" in workspace
        or not Path(workspace).is_absolute()
        or not isinstance(token, str)
        or _TOKEN_RE.fullmatch(token) is None
    ):
        raise DesktopLaunchError("invalid desktop launch configuration")
    return DesktopLaunchConfig(workspace=Path(workspace), token=token)


class DesktopTokenGate:
    """Require the private launch token before every desktop ASGI surface."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token.encode("ascii")

    def _authorized(self, scope: Scope) -> bool:
        values = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == _TOKEN_HEADER
        ]
        return len(values) == 1 and hmac.compare_digest(values[0], self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and not self._authorized(scope):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


class _ReadyServer:
    """Emit readiness at Uvicorn's post-bind startup boundary exactly once."""

    def __init__(
        self,
        server,
        *,
        ready_stdout: TextIO,
        port: int,
        ready_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self._server = server
        self._startup = server.startup
        self._ready_stdout = ready_stdout
        self._port = port
        self._ready_sent = False
        self._ready_allowed = ready_allowed or (lambda: True)

    async def _startup_with_ready(self, sockets=None) -> None:
        await self._startup(sockets=sockets)
        if self._server.started and not self._ready_sent and self._ready_allowed():
            payload = {
                "schema": 1,
                "type": "ready",
                "host": "127.0.0.1",
                "port": self._port,
                "pid": os.getpid(),
            }
            self._ready_stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self._ready_stdout.flush()
            self._ready_sent = True

    def run(self, sockets) -> None:
        self._server.startup = self._startup_with_ready
        try:
            self._server.run(sockets=sockets)
        finally:
            self._server.startup = self._startup

    @property
    def should_exit(self) -> bool:
        return self._server.should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._server.should_exit = value


def _request_worker_stop(process: subprocess.Popen | None) -> None:
    try:
        if process is not None and process.poll() is None:
            process.terminate()
    except ProcessLookupError:
        pass


def _serve(config: DesktopLaunchConfig, *, ready_stdout: TextIO) -> int:
    """Compose the Solo ASGI app, local worker, and owned loopback socket."""
    import uvicorn

    from frisket.operability.structured_logging import configure_logging
    from frisket.runtime.launch import worker_argv
    from frisket.runtime.supervisor import spawn_service, stop_service
    from frisket.server.app import create_app
    from frisket.server.standalone import StandaloneLifetimeLock, StandaloneRuntimeState
    from frisket.server.static_serving import packaged_static_dir

    static_dir = packaged_static_dir()
    if static_dir is None:
        raise DesktopLaunchError("desktop static assets are unavailable")

    workspace = config.workspace.resolve()
    secret_key = workspace / ".frisket" / "secrets" / "master.key"
    state = StandaloneRuntimeState()
    worker: subprocess.Popen | None = None
    watcher: threading.Thread | None = None
    sock: socket.socket | None = None
    lock = StandaloneLifetimeLock(workspace)
    lock.acquire()
    try:
        os.environ["FRISKET_SECRETS_KEY_FILE"] = str(secret_key)
        configure_logging(stream=sys.stderr, force=True)
        app = create_app(workspace, static_dir=static_dir)
        app.state.standalone_runtime = state
        gated_app = DesktopTokenGate(app, config.token)
        worker = spawn_service(
            worker_argv("cli-worker", str(workspace)),
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        state.worker_pid = worker.pid
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(socket.SOMAXCONN)
        port = int(sock.getsockname()[1])
        server = _ReadyServer(
            uvicorn.Server(
                uvicorn.Config(
                    gated_app,
                    host="127.0.0.1",
                    port=port,
                    log_config=None,
                    proxy_headers=False,
                )
            ),
            ready_stdout=ready_stdout,
            port=port,
            ready_allowed=lambda: (
                worker is not None
                and worker.poll() is None
                and not state.worker_exited_unexpectedly
            ),
        )
        original_handle_exit = getattr(server._server, "handle_exit", None)
        if callable(original_handle_exit):

            def handle_exit(sig, frame) -> None:
                state.begin_stopping()
                _request_worker_stop(worker)
                original_handle_exit(sig, frame)

            server._server.handle_exit = handle_exit

        def watch_worker() -> None:
            worker.wait()
            if not state.stopping:
                state.worker_exited_unexpectedly = True
                server.should_exit = True

        watcher = threading.Thread(target=watch_worker, daemon=True)
        watcher.start()
        server.run(sockets=[sock])
        return 1 if state.worker_exited_unexpectedly else 0
    finally:
        state.begin_stopping()
        try:
            _request_worker_stop(worker)
            stop_service(worker)
        finally:
            try:
                if watcher is not None:
                    watcher.join(timeout=1)
                if sock is not None:
                    sock.close()
            finally:
                lock.release()


def main() -> int:
    """Bootstrap target for Electron; never write diagnostics to stdout."""
    if sys.argv != ["desktop-server"]:
        print("desktop server: invalid launch configuration", file=sys.stderr)
        return 2
    try:
        config = read_launch_config(sys.stdin.buffer)
    except DesktopLaunchError:
        print("desktop server: invalid launch configuration", file=sys.stderr)
        return 2

    ready_stdout = sys.stdout
    try:
        sys.stdout = sys.stderr
        return _serve(config, ready_stdout=ready_stdout)
    except DesktopLaunchError as exc:
        print(f"desktop server: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - desktop protocol intentionally reveals no details
        print("desktop server: failed to start", file=sys.stderr)
        return 1
    finally:
        sys.stdout = ready_stdout


__all__ = ["DesktopLaunchConfig", "DesktopTokenGate", "main", "read_launch_config"]

"""Public-boundary checks for the desktop-owned local server."""

from __future__ import annotations

import io
import json
import os
import socket
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect
import uvicorn


TOKEN = "a" * 64


def _static_dir(tmp_path):
    directory = tmp_path / "static"
    directory.mkdir()
    (directory / "index.html").write_text("<html>desktop app</html>")
    (directory / "asset.js").write_text("console.log('desktop')")
    return directory


def test_desktop_token_gate_covers_real_health_static_and_docs(tmp_path):
    from frisket.server.app import create_app
    from frisket.server.desktop import DesktopTokenGate

    app = DesktopTokenGate(
        create_app(tmp_path / "workspace", static_dir=_static_dir(tmp_path)), TOKEN
    )
    client = TestClient(app)

    for path in ("/api/health", "/", "/asset.js", "/docs", "/not-found"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.content == b'{"detail":"Unauthorized"}'

    assert (
        client.get(
            "/api/health", headers={"X-Frisket-Desktop-Token": TOKEN}
        ).status_code
        == 200
    )
    assert (
        client.get("/", headers={"X-Frisket-Desktop-Token": TOKEN}).text
        == "<html>desktop app</html>"
    )
    assert (
        client.get(
            "/api/health",
            headers=[
                ("X-Frisket-Desktop-Token", TOKEN),
                ("X-Frisket-Desktop-Token", TOKEN),
            ],
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/health", headers={"X-Frisket-Desktop-Token": "b" * 64}
        ).status_code
        == 401
    )
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/health", headers={"X-Frisket-Desktop-Token": TOKEN}
        ):
            pass
    assert excinfo.value.code == 1008


def test_desktop_launch_config_refuses_invalid_input_without_echoing_it(
    monkeypatch, capsys
):
    from frisket.server import desktop

    monkeypatch.setattr(
        desktop.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(b'{"token":"leak-me"}')),
    )
    monkeypatch.setattr(desktop.sys, "argv", ["desktop-server"])
    monkeypatch.setattr(desktop, "_serve", lambda *_args, **_kwargs: pytest.fail())
    assert desktop.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "leak-me" not in captured.err


def test_desktop_bootstrap_kind_is_fixed_and_worker_stdio_can_be_redirected():
    from frisket.runtime._bootstrap import KINDS
    from frisket.runtime.supervisor import spawn_service

    assert "desktop-server" in KINDS
    assert spawn_service.__kwdefaults__ is not None
    assert {"stdin", "stdout", "stderr"} <= set(spawn_service.__kwdefaults__)


def test_ready_is_emitted_after_a_live_ephemeral_socket_accepts_connections():
    from frisket.server.desktop import _ReadyServer

    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    ready = io.StringIO()
    server = _ReadyServer(
        uvicorn.Server(
            uvicorn.Config(app, log_config=None, proxy_headers=False),
        ),
        ready_stdout=ready,
        port=port,
    )
    thread = threading.Thread(target=server.run, args=([sock],), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        record = json.loads(ready.getvalue())
        assert record == {
            "schema": 1,
            "type": "ready",
            "host": "127.0.0.1",
            "port": port,
            "pid": os.getpid(),
        }
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()


def test_unexpected_worker_exit_stops_server_nonzero_and_cleans_up(
    tmp_path, monkeypatch
):
    from frisket.server import desktop
    import frisket.operability.structured_logging as structured_logging
    import frisket.runtime.launch as launch
    import frisket.runtime.supervisor as supervisor
    import frisket.server.app as app_module
    import frisket.server.static_serving as static_serving

    events: list[object] = []
    app = SimpleNamespace(state=SimpleNamespace())

    class Worker:
        pid = 17

        def poll(self):
            return 1

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            return 1

    class FakeServer:
        should_exit = False

        async def startup(self, sockets=None):
            return None

        def handle_exit(self, _sig, _frame):
            return None

        def run(self, sockets=None):
            events.append(("run", sockets))

    monkeypatch.setattr(app_module, "create_app", lambda *_args, **_kwargs: app)
    monkeypatch.setattr(
        static_serving, "packaged_static_dir", lambda: _static_dir(tmp_path)
    )
    monkeypatch.setattr(structured_logging, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(launch, "worker_argv", lambda *_args: ["worker"])
    monkeypatch.setattr(supervisor, "spawn_service", lambda *_args, **_kwargs: Worker())
    monkeypatch.setattr(
        supervisor, "stop_service", lambda worker: events.append(("stop", worker))
    )
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda _config: FakeServer())

    assert (
        desktop._serve(
            desktop.DesktopLaunchConfig(tmp_path / "workspace", TOKEN),
            ready_stdout=io.StringIO(),
        )
        == 1
    )
    assert app.state.standalone_runtime.worker_exited_unexpectedly is True
    assert any(item[0] == "run" for item in events if isinstance(item, tuple))
    assert any(item[0] == "stop" for item in events if isinstance(item, tuple))

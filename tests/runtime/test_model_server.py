from __future__ import annotations

import socket
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.realtime


class _Process:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode


def _write_fake_factory(root: Path) -> None:
    package = root / "frisket_models"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "local.py").write_text(
        "import os\n"
        "def create_app():\n"
        " token=os.environ['FRISKET_LOCAL_MODELS_TOKEN']\n"
        " async def app(scope,receive,send):\n"
        "  headers=dict(scope.get('headers',[]))\n"
        "  ok=(scope.get('path')=='/capabilities' and "
        "headers.get(b'authorization')==f'Bearer {token}'.encode())\n"
        "  status=200 if ok else 403\n"
        "  body=b'{}'\n"
        "  await send({'type':'http.response.start','status':status,'headers':[]})\n"
        "  await send({'type':'http.response.body','body':body})\n"
        " return app\n"
    )


def test_connection_is_stable_and_child_receives_only_internal_credentials(
    tmp_path, monkeypatch
):
    from frisket.runtime import model_server

    environment = {
        "FRISKET_MODELS_URL": "https://operator.example",
        "FRISKET_MODELS_TOKEN": "operator-secret",
        "OPENAI_API_KEY": "provider-secret",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret",
        "PYTHONPATH": "/untrusted/injection",
        "HTTPS_PROXY": "http://proxy.example",
        "REQUESTS_CA_BUNDLE": "/certs/company.pem",
        "HF_HUB_OFFLINE": "1",
        "LANG": "en_US.UTF-8",
    }
    process = _Process()
    launch = {}
    monkeypatch.setattr(model_server, "is_installed", lambda: True)
    monkeypatch.setattr(model_server, "runtime_python", lambda: tmp_path / "python")
    monkeypatch.setattr(model_server, "runtime_dir", lambda: tmp_path)

    def spawn(argv, **kwargs):
        launch.update(argv=argv, **kwargs)
        return process

    monkeypatch.setattr(model_server, "spawn_service", spawn)
    monkeypatch.setattr(model_server, "stop_service", lambda proc: None)
    owned = model_server.LocalModelServer(environ=environment)
    monkeypatch.setattr(owned, "_wait_until_ready", lambda proc: True)
    url = owned.url
    token = environment[model_server.LOCAL_MODELS_TOKEN_ENV]

    assert owned._start_if_installed() is True
    assert owned.url == url
    assert launch["env"][model_server.LOCAL_MODELS_TOKEN_ENV] == token
    assert launch["env"]["HF_HOME"] == str(tmp_path / "cache")
    assert launch["env"]["HOME"] == str(tmp_path / "home")
    assert launch["env"]["PYTHONNOUSERSITE"] == "1"
    assert launch["env"]["HTTPS_PROXY"] == "http://proxy.example"
    assert launch["env"]["REQUESTS_CA_BUNDLE"] == "/certs/company.pem"
    assert launch["env"]["HF_HUB_OFFLINE"] == "1"
    assert launch["env"]["LANG"] == "en_US.UTF-8"
    assert "OPENAI_API_KEY" not in launch["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in launch["env"]
    assert "PYTHONPATH" not in launch["env"]
    assert "FRISKET_MODELS_TOKEN" not in launch["env"]
    assert launch["stdout"] is sys.stderr
    assert launch["stderr"] is sys.stderr
    assert "operator-secret" not in launch["argv"]
    assert environment["FRISKET_MODELS_URL"] == "https://operator.example"
    assert environment["FRISKET_MODELS_TOKEN"] == "operator-secret"
    owned.stop()
    assert model_server.LOCAL_MODELS_URL_ENV not in environment
    assert model_server.LOCAL_MODELS_TOKEN_ENV not in environment


def test_server_can_start_after_install_finishes(tmp_path, monkeypatch):
    from frisket.runtime import model_server

    installed = threading.Event()
    launched = threading.Event()
    launches = []
    monkeypatch.setattr(model_server, "is_installed", installed.is_set)
    monkeypatch.setattr(model_server, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(model_server, "runtime_python", lambda: tmp_path / "python")
    monkeypatch.setattr(model_server, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(
        model_server,
        "spawn_service",
        lambda argv, **kwargs: (
            launches.append((argv, kwargs)),
            launched.set(),
            _Process(),
        )[-1],
    )
    monkeypatch.setattr(model_server, "stop_service", lambda proc: None)

    owned = model_server.LocalModelServer(environ={})
    monkeypatch.setattr(owned, "_wait_until_ready", lambda proc: True)
    owned.start()
    assert not launched.is_set()
    installed.set()
    assert launched.wait(1)
    assert len(launches) == 1
    owned.stop()


def test_failed_readiness_stops_child_and_throttles_retry(tmp_path, monkeypatch):
    from frisket.runtime import model_server

    launches, stops = [], []
    monkeypatch.setattr(model_server, "is_installed", lambda: True)
    monkeypatch.setattr(model_server, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(model_server, "runtime_python", lambda: tmp_path / "python")
    monkeypatch.setattr(
        model_server,
        "spawn_service",
        lambda *_a, **_kw: launches.append(_Process()) or launches[-1],
    )
    monkeypatch.setattr(model_server, "stop_service", stops.append)
    owned = model_server.LocalModelServer(environ={})
    monkeypatch.setattr(owned, "_wait_until_ready", lambda _process: False)
    try:
        assert not owned._start_if_installed()
        assert not owned._start_if_installed()
        assert len(launches) == 1
        assert stops == launches
        assert owned._process is None
    finally:
        owned.stop()


def test_readiness_retries_until_authenticated_response(monkeypatch):
    from frisket.runtime import model_server
    from contextlib import nullcontext
    from types import SimpleNamespace

    requests = []

    def open_request(request, *, timeout):
        requests.append(request)
        if len(requests) == 1:
            raise ConnectionRefusedError
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(
        model_server.urllib.request,
        "build_opener",
        lambda *_args: SimpleNamespace(open=open_request),
    )
    monkeypatch.setattr(model_server.time, "sleep", lambda _: None)
    assert model_server.wait_until_ready(
        "http://127.0.0.1:1234", "test-secret", stopped=lambda: False
    )
    assert len(requests) == 2
    assert requests[-1].get_header("Authorization") == "Bearer test-secret"
    assert not model_server.wait_until_ready(
        "http://127.0.0.1:1234", "test-secret", stopped=lambda: True
    )
    assert len(requests) == 2


def test_real_server_requires_token_and_is_stopped(tmp_path, monkeypatch):
    from frisket.runtime import model_server
    from frisket.runtime.launch import PythonRuntime

    _write_fake_factory(tmp_path)
    environment = {"PYTHONPATH": str(tmp_path), "PATH": ""}
    monkeypatch.setattr(model_server, "is_installed", lambda: True)
    monkeypatch.setattr(
        model_server, "runtime_python", lambda: PythonRuntime.current().executable
    )
    monkeypatch.setattr(model_server, "runtime_dir", lambda: tmp_path)
    real_spawn = model_server.spawn_service

    def spawn_test_factory(argv, **kwargs):
        kwargs["env"]["PYTHONPATH"] = str(tmp_path)
        return real_spawn(argv, **kwargs)

    monkeypatch.setattr(model_server, "spawn_service", spawn_test_factory)

    owned = model_server.LocalModelServer(environ=environment)
    assert owned._start_if_installed() is True
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(f"{owned.url}/capabilities", timeout=1)  # noqa: S310
    assert denied.value.code == 403
    host, port = "127.0.0.1", int(owned.url.rsplit(":", 1)[1])
    owned.stop()
    with pytest.raises(OSError):
        socket.create_connection((host, port), timeout=0.25)


def test_real_server_dies_with_its_application_parent(tmp_path):
    _write_fake_factory(tmp_path)
    source_root = Path(__file__).resolve().parents[2] / "src"
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        "import os\n"
        "from frisket.runtime import model_server\n"
        "from frisket.runtime.launch import PythonRuntime\n"
        "model_server.is_installed=lambda: True\n"
        "model_server.runtime_python=lambda: PythonRuntime.current().executable\n"
        f"model_server.runtime_dir=lambda: __import__('pathlib').Path({str(tmp_path)!r})\n"
        "real_spawn=model_server.spawn_service\n"
        f"def spawn(argv,**kwargs):\n kwargs['env']['PYTHONPATH']={str(tmp_path)!r}\n return real_spawn(argv,**kwargs)\n"
        "model_server.spawn_service=spawn\n"
        "owned=model_server.LocalModelServer(environ=os.environ)\n"
        "assert owned._start_if_installed()\n"
        "print(owned.url,flush=True)\n"
        "os._exit(0)\n"
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join((str(source_root), str(tmp_path))),
    }
    output_path = tmp_path / "parent.stdout"
    with output_path.open("wb") as output:
        parent = subprocess.Popen(
            [sys.executable, str(launcher)],
            env=environment,
            stdout=output,
            stderr=subprocess.DEVNULL,
        )
        assert parent.wait(timeout=15) == 0
    output_lines = output_path.read_text().splitlines()
    assert len(output_lines) == 1  # Child Uvicorn output cannot corrupt stdout IPC.
    port = int(output_lines[0].rsplit(":", 1)[1])
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.1)
        except OSError:
            break
        connection.close()
        if time.monotonic() >= deadline:
            pytest.fail("model server survived its application parent")
        time.sleep(0.05)

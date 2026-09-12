"""Focused contract tests for the single-replica ``frisket server`` launcher."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket import cli
from frisket.server.standalone import (
    StandaloneAlreadyRunning,
    StandaloneLifetimeLock,
)
from frisket.server.workspace import (
    DataRootUnwritable,
    ensure_writable_data_root,
)


def _attempt_lock(data_dir: str, result) -> None:
    try:
        with StandaloneLifetimeLock(Path(data_dir)):
            result.put("acquired")
    except StandaloneAlreadyRunning:
        result.put("blocked")


def test_lifetime_lock_rejects_another_process_before_app_state(tmp_path):
    """The second launcher cannot reach app initialization on the same root."""
    result = multiprocessing.Queue()
    with StandaloneLifetimeLock(tmp_path):
        process = multiprocessing.Process(
            target=_attempt_lock, args=(str(tmp_path), result)
        )
        process.start()
        process.join(timeout=10)
    assert process.exitcode == 0
    assert result.get(timeout=2) == "blocked"


def _revoke_write(root: Path) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("write access cannot be revoked reliably here")
    root.chmod(0o555)


def test_unwritable_data_root_fails_loudly_with_the_chown_fix(tmp_path):
    """A root-owned /data inherited from a pre-non-root image must produce
    the actionable ownership message, not a bare EACCES traceback."""
    root = tmp_path / "data"
    root.mkdir()
    _revoke_write(root)
    try:
        with pytest.raises(DataRootUnwritable) as excinfo:
            ensure_writable_data_root(root)
    finally:
        root.chmod(0o755)
    message = str(excinfo.value)
    assert "is not writable by this process" in message
    assert "chown -R 10001:10001" in message
    assert "After fixing ownership, restart Frisket." in message


def test_writable_data_root_probe_leaves_no_residue(tmp_path):
    ensure_writable_data_root(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_lifetime_lock_surfaces_the_unwritable_root_message(tmp_path):
    """The standalone launcher's lock probes before FileLock so the operator
    sees the chown instruction instead of a PermissionError traceback."""
    root = tmp_path / "data"
    root.mkdir()
    _revoke_write(root)
    try:
        with pytest.raises(DataRootUnwritable):
            with StandaloneLifetimeLock(root):
                pass
    finally:
        root.chmod(0o755)


def test_database_backed_worker_uses_explicit_flat_projects_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRISKET_PROJECTS_ROOT", str(tmp_path / "flat-data"))
    assert cli._handler_workspace_root(None) == tmp_path / "flat-data"


def test_server_requires_explicit_public_base_url(monkeypatch):
    monkeypatch.delenv("FRISKET_BASE_URL", raising=False)
    assert cli.server([]) == 2


def test_server_initializes_app_before_shared_queue_worker_and_exits_on_child_death(
    tmp_path, monkeypatch
):
    events: list[object] = []
    app = SimpleNamespace(state=SimpleNamespace())

    def fake_app():
        events.append("app")
        return app

    class FakeProc:
        pid = 4321

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            return 1

        def poll(self):
            return 1

    def fake_popen(argv):
        events.append(("worker", argv))
        return FakeProc()

    class FakeConfig:
        def __init__(self, app, **kwargs):
            events.append(("config", app, kwargs))

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            events.append("uvicorn")

    import frisket.team.app as team_app
    import uvicorn

    monkeypatch.setattr(team_app, "create_team_app_from_env", fake_app)
    monkeypatch.setattr(cli, "spawn_service", fake_popen)
    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(cli, "_stop_server_worker", lambda proc: events.append("stop"))
    monkeypatch.setenv("FRISKET_BASE_URL", "https://frisket.example")

    assert cli.server(["--data-dir", str(tmp_path), "--port", "9123"]) == 1
    worker = next(
        item for item in events if isinstance(item, tuple) and item[0] == "worker"
    )
    assert events.index("app") < events.index(worker)
    assert worker[1] == cli._database_worker_argv(
        f"sqlite:///{tmp_path / 'server.sqlite3'}"
    )
    assert (
        os.environ["FRISKET_TEAM_DATABASE_URL"]
        == f"sqlite:///{tmp_path / 'server.sqlite3'}"
    )
    assert os.environ["FRISKET_DATABASE_URL"] == os.environ["FRISKET_TEAM_DATABASE_URL"]
    assert (
        os.environ["FRISKET_RUN_QUEUE_DATABASE_URL"]
        == os.environ["FRISKET_TEAM_DATABASE_URL"]
    )
    assert os.environ["FRISKET_RUN_QUEUE_SCHEMA_MODE"] == "initialize"
    assert "FRISKET_WORKER_VERSION_REFUSE" not in os.environ
    assert os.environ["FRISKET_PROJECTS_ROOT"] == str(tmp_path)
    config = next(
        item for item in events if isinstance(item, tuple) and item[0] == "config"
    )
    assert config[2] == {
        "host": "0.0.0.0",
        "port": 9123,
        "log_config": None,
        "proxy_headers": False,
    }
    assert app.state.standalone_runtime.worker_exited_unexpectedly is True


def test_server_stops_spawned_worker_if_http_configuration_fails(tmp_path, monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace())
    proc = SimpleNamespace(pid=9)
    stopped: list[object] = []

    import frisket.team.app as team_app
    import uvicorn

    monkeypatch.setattr(team_app, "create_team_app_from_env", lambda: app)
    monkeypatch.setattr(cli, "spawn_service", lambda _argv: proc)
    monkeypatch.setattr(
        uvicorn,
        "Config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad bind")),
    )
    monkeypatch.setattr(cli, "_stop_server_worker", stopped.append)
    monkeypatch.setenv("FRISKET_BASE_URL", "https://frisket.example")

    with pytest.raises(RuntimeError, match="bad bind"):
        cli.server(["--data-dir", str(tmp_path)])
    assert stopped == [proc]

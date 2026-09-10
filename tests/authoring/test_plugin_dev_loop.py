from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from helpers import CliResult
from helpers import run_cli as _cli

TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"

# Real-clock: a real uvicorn server must become ready on a real socket, which
# a manual clock cannot make happen. The CLI itself runs in-process via
# helpers.run_cli; its `--once` output/receipt assertions don't depend on a
# process boundary.
# The only real-time wait is positive (server startup), generous timeout.
pytestmark = pytest.mark.realtime


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


@contextlib.contextmanager
def _live_server(tmp_path: Path) -> Iterator[str]:
    """Serve the real app over a real socket on an ephemeral port.

    Standard uvicorn embedding pattern (uvicorn.Server run in a background
    thread) -- the repo has no prior test that boots a full ASGI app on a
    real socket (existing server tests use FastAPI's in-process TestClient,
    e.g. tests/plugin_contract/test_plugin_backend_gauntlet.py:_client), so
    this is the documented uvicorn convention for "a CLI needs a base URL",
    not an invented mechanism.
    """
    app = create_app(tmp_path / "workspace")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "test uvicorn server did not start in time"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def _create_project(server: str, *, name: str) -> str:
    response = httpx.post(f"{server}/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def _init_plugin(tmp_path: Path, plugin_id: str) -> Path:
    package_dir = tmp_path / plugin_id.replace(".", "_")
    init = _cli(
        "plugin",
        "init",
        "--id",
        plugin_id,
        "--output",
        str(package_dir),
        "--with",
        "action",
    )
    assert init.returncode == 0, init.stderr
    return package_dir


def _runtime_index(server: str, project_id: str) -> dict:
    response = httpx.get(f"{server}/api/projects/{project_id}/workbench/plugins")
    assert response.status_code == 200, response.text
    return response.json()


def _dev_once(package_dir: Path, *, project_id: str, server: str) -> CliResult:
    return _cli(
        "plugin",
        "dev",
        str(package_dir),
        "--project",
        project_id,
        "--server",
        server,
        "--once",
    )


def test_once_produces_installed_and_activated_receipt(tmp_path: Path) -> None:
    with _live_server(tmp_path) as server:
        project_id = _create_project(server, name="dev-loop-once")
        plugin_id = "test.devlooponce"
        package_dir = _init_plugin(tmp_path, plugin_id)

        run = _dev_once(package_dir, project_id=project_id, server=server)
        assert run.returncode == 0, run.stderr
        payload = json.loads(run.stdout)
        assert payload["pluginId"] == plugin_id
        assert payload["receiptId"]
        # The default `plugin init` template declares
        # plugin:trusted_local_backend, so the backend/activate leg must have
        # run too.
        assert payload["backendActivated"] is True

        index = _runtime_index(server, project_id)
        by_id = {entry["pluginId"]: entry for entry in index["plugins"]}
        assert plugin_id in by_id
        entry = by_id[plugin_id]
        assert entry["installState"] == "enabled"
        assert entry["receiptId"] == payload["receiptId"]


def test_source_edit_and_second_once_yields_a_new_content_addressed_receipt(
    tmp_path: Path,
) -> None:
    with _live_server(tmp_path) as server:
        project_id = _create_project(server, name="dev-loop-edit")
        plugin_id = "test.devloopedit"
        package_dir = _init_plugin(tmp_path, plugin_id)

        first = _dev_once(package_dir, project_id=project_id, server=server)
        assert first.returncode == 0, first.stderr
        first_payload = json.loads(first.stdout)

        first_index = _runtime_index(server, project_id)
        first_entry = {e["pluginId"]: e for e in first_index["plugins"]}[plugin_id]
        first_package_sha256 = first_entry["packageSha256"]

        plugin_py = package_dir / "plugin.py"
        source = plugin_py.read_text(encoding="utf-8")
        plugin_py.write_text(
            source + "\n# dev-loop-edit marker: content-addressed hash must change\n",
            encoding="utf-8",
        )

        second = _dev_once(package_dir, project_id=project_id, server=server)
        assert second.returncode == 0, second.stderr
        second_payload = json.loads(second.stdout)

        assert second_payload["receiptId"] != first_payload["receiptId"]

        second_index = _runtime_index(server, project_id)
        second_entry = {e["pluginId"]: e for e in second_index["plugins"]}[plugin_id]
        assert second_entry["receiptId"] == second_payload["receiptId"]
        assert second_entry["packageSha256"] != first_package_sha256


def test_build_failure_exits_nonzero_without_disturbing_prior_receipt(
    tmp_path: Path,
) -> None:
    with _live_server(tmp_path) as server:
        project_id = _create_project(server, name="dev-loop-build-fail")
        plugin_id = "test.devloopbuildfail"
        package_dir = _init_plugin(tmp_path, plugin_id)

        good = _dev_once(package_dir, project_id=project_id, server=server)
        assert good.returncode == 0, good.stderr
        good_payload = json.loads(good.stdout)

        before_index = _runtime_index(server, project_id)
        before_entry = {e["pluginId"]: e for e in before_index["plugins"]}[plugin_id]
        assert before_entry["receiptId"] == good_payload["receiptId"]

        # The default workspace is backend-only, so plugin.py is the source
        # whose failure must stop the cycle before it disturbs the receipt.
        config_path = package_dir / "plugin.py"
        good_config = config_path.read_text(encoding="utf-8")
        config_path.write_text("this is not valid python {{{", encoding="utf-8")

        try:
            broken = _dev_once(package_dir, project_id=project_id, server=server)
            assert broken.returncode != 0
            assert broken.stdout == ""
            assert broken.stderr

            after_index = _runtime_index(server, project_id)
            after_entry = {e["pluginId"]: e for e in after_index["plugins"]}[plugin_id]
            assert after_entry["receiptId"] == good_payload["receiptId"]
            assert after_entry["installState"] == "enabled"
        finally:
            config_path.write_text(good_config, encoding="utf-8")

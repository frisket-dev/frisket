from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from frisket.authoring.plugin_dev import _mtime_snapshot
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from helpers import run_cli

# realtime: this suite drives the real `frisket.cli` as a child process
# (subprocess.Popen) and a real uvicorn server on a background thread, then
# polls their real stdout/liveness. A manual clock cannot make an external
# child or server advance, so all poll loops wait FOR a real event (subprocess
# output, server startup); none assert something did NOT happen within the
# window except as a bounded liveness check already gated by the same wait
# (e.g. the process is still running after finding what it was waiting for).
pytestmark = pytest.mark.realtime

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


# subprocess-boundary: the `plugin dev` watch-loop tests supervise a real,
# long-lived child (liveness via poll(), terminate/kill teardown, streaming
# stdout drains) — that cannot run in-process. One-shot CLI calls go through
# helpers.run_cli instead.
def _cli_watch(*args: str, **popen_kwargs: object) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "frisket.cli", *args],
        cwd=ROOT,
        text=True,
        **popen_kwargs,
    )


_cli = run_cli


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


def _init_and_build(tmp_path: Path, plugin_id: str) -> Path:
    package_dir = _init_plugin(tmp_path, plugin_id)
    build = _cli("plugin", "build", str(package_dir))
    assert build.returncode == 0, build.stderr
    return package_dir


# ---------------------------------------------------------------------------
# httpx.HTTPError handling per cycle in plugin_dev.py
# ---------------------------------------------------------------------------

UNREACHABLE_SERVER = "http://127.0.0.1:1"


def test_dev_once_reports_clean_error_when_server_unreachable(
    tmp_path: Path,
) -> None:
    plugin_id = "test.devunreachonce"
    package_dir = _init_plugin(tmp_path, plugin_id)

    run = _cli(
        "plugin",
        "dev",
        str(package_dir),
        "--project",
        "does-not-matter",
        "--server",
        UNREACHABLE_SERVER,
        "--once",
    )
    assert run.returncode != 0
    assert run.stdout == ""
    assert run.stderr.strip() != ""
    # Clean one-line error report -- not a raw httpx traceback. (stderr also
    # carries the build validation report `_build` deliberately routes there
    # to keep stdout receipt-only, so assert on the error line, not on stderr
    # being a single line.)
    assert "Traceback (most recent call last)" not in run.stderr
    error_lines = [
        line for line in run.stderr.splitlines() if line.startswith("error:")
    ]
    assert len(error_lines) == 1, run.stderr
    assert UNREACHABLE_SERVER in error_lines[0]


def test_dev_watch_keeps_polling_after_server_unreachable(tmp_path: Path) -> None:
    """A dead --server (e.g. mid-watch restart) must not kill the watch loop
    -- it should report the cycle error and keep watching for the next source
    change, not crash. Proven by pointing watch mode at an address nothing is
    listening on for its entire lifetime (the simplest deterministic stand-in
    for "server goes away mid-watch": the first cycle inside `_watch` already
    exercises the exact same httpx-error path a restart would hit) and
    observing the process stays alive (`poll() is None`) well past the point
    the unfixed code would have propagated the uncaught httpx.ConnectError."""
    plugin_id = "test.devunreachwatch"
    package_dir = _init_plugin(tmp_path, plugin_id)

    proc = _cli_watch(
        "plugin",
        "dev",
        str(package_dir),
        "--project",
        "does-not-matter",
        "--server",
        UNREACHABLE_SERVER,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert isinstance(proc, subprocess.Popen)
    stderr_chunks = _drain_stdout(proc.stderr)
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail("watch loop exited instead of continuing to watch")
            if "error:" in "".join(stderr_chunks):
                break
            time.sleep(0.05)
        else:
            pytest.fail(
                "watch cycle did not report the unreachable server within the "
                f"timeout; stderr so far: {''.join(stderr_chunks)[:4000]}"
            )
        assert proc.poll() is None, "watch loop exited instead of continuing to watch"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    stderr_text = "".join(stderr_chunks)
    assert "Traceback (most recent call last)" not in stderr_text
    assert "error:" in stderr_text


# ---------------------------------------------------------------------------
# Watch-mode receipt/banner prints flush when piped
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _live_server(tmp_path: Path) -> Iterator[str]:
    """Real bound HTTP server (uvicorn.Server in a background thread on an
    ephemeral port) -- same pattern as
    tests/test_plugin_dev_loop.py:_live_server, duplicated here rather than
    imported across test modules (no existing precedent for that in this
    suite, and this file must stay self-contained)."""
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


def _drain_stdout(stream: object) -> list[str]:
    """Read raw stdout bytes one chunk at a time on a background thread and
    accumulate them, so the main thread can observe how MUCH has arrived
    after a bounded wait -- the direct way to prove `print(..., flush=True)`
    (vs. Python's default block buffering on a pipe) without depending on
    process exit, which would flush anyway and hide the bug. This is the
    "bounded read on a piped subprocess" option from the task brief; chosen
    over a unit-level print-path assertion because the repo has no existing
    precedent for asserting `flush=True` was passed to `print` other than by
    observing the actual buffering behavior it causes."""
    chunks: list[str] = []

    def _reader() -> None:
        while True:
            chunk = stream.read(1)  # type: ignore[attr-defined]
            if not chunk:
                return
            chunks.append(chunk)

    threading.Thread(target=_reader, daemon=True).start()
    return chunks


def test_watch_mode_receipt_and_banner_flush_when_piped(tmp_path: Path) -> None:
    with _live_server(tmp_path) as server:
        project_id = _create_project(server, name="flush-check")
        plugin_id = "test.devflushcheck"
        package_dir = _init_plugin(tmp_path, plugin_id)

        proc = _cli_watch(
            "plugin",
            "dev",
            str(package_dir),
            "--project",
            project_id,
            "--server",
            server,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert isinstance(proc, subprocess.Popen)
        chunks = _drain_stdout(proc.stdout)
        stderr_chunks = _drain_stdout(proc.stderr)
        try:
            deadline = time.monotonic() + 30.0
            combined = ""
            while time.monotonic() < deadline:
                combined = "".join(chunks)
                if "backendActivated" in combined:
                    break
                time.sleep(0.05)
            else:
                pytest.fail(
                    "watch-mode receipt did not appear on the piped stdout "
                    "within the timeout (buffered without flush=True); "
                    f"stderr so far: {''.join(stderr_chunks)[:4000]}"
                )
            assert "watching" in combined
            assert "backendActivated" in combined
        finally:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Watch snapshot includes workspace-root *.py siblings
# ---------------------------------------------------------------------------


def test_mtime_snapshot_includes_workspace_root_py_sibling(tmp_path: Path) -> None:
    plugin_id = "test.devsiblingwatch"
    package_dir = _init_plugin(tmp_path, plugin_id)

    before = _mtime_snapshot(package_dir)
    assert "plugin.py" in before
    assert "helper.py" not in before

    helper = package_dir / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")

    after_create = _mtime_snapshot(package_dir)
    assert "helper.py" in after_create, (
        "a top-level *.py sibling of plugin.py must be part of the watched "
        "snapshot so editing it triggers a rebuild"
    )
    assert after_create != before

    time.sleep(0.01)
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    after_edit = _mtime_snapshot(package_dir)
    assert after_edit["helper.py"] != after_create["helper.py"], (
        "editing the sibling must change its tracked mtime"
    )

    # Generated artifacts at plugin_root are never *.py (plugin.json,
    # workbench-descriptors.json, .frisket-sdk/*.mjs), so building must not
    # introduce a spurious extra *.py entry beyond plugin.py + helper.py.
    build = _cli("plugin", "build", str(package_dir))
    assert build.returncode == 0, build.stderr
    after_build = _mtime_snapshot(package_dir)
    py_keys = {k for k in after_build if k.endswith(".py") and "/" not in k}
    assert py_keys == {"plugin.py", "helper.py"}


def test_dev_watch_rebuilds_on_workspace_root_sibling_edit(tmp_path: Path) -> None:
    """End-to-end proof (not just the unit-level snapshot check above): in a
    live watch loop, editing a workspace-root sibling module actually
    triggers a new install/activate cycle (a new receipt printed to stdout),
    the same content-addressing contract
    test_plugin_dev_loop.py::test_source_edit_and_second_once_yields_a_new_content_addressed_receipt
    proves for plugin.py itself."""
    with _live_server(tmp_path) as server:
        project_id = _create_project(server, name="sibling-watch")
        plugin_id = "test.devsiblingcycle"
        package_dir = _init_plugin(tmp_path, plugin_id)
        helper = package_dir / "helper.py"
        helper.write_text("VALUE = 1\n", encoding="utf-8")

        proc = _cli_watch(
            "plugin",
            "dev",
            str(package_dir),
            "--project",
            project_id,
            "--server",
            server,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert isinstance(proc, subprocess.Popen)
        chunks = _drain_stdout(proc.stdout)
        try:
            first_receipt = _wait_for_nth_receipt(chunks, n=1, timeout=15.0)
            assert first_receipt is not None, "first cycle never printed a receipt"

            time.sleep(0.05)
            helper.write_text("VALUE = 2\n", encoding="utf-8")

            second_receipt = _wait_for_nth_receipt(chunks, n=2, timeout=15.0)
            assert second_receipt is not None, (
                "editing a workspace-root *.py sibling did not trigger a "
                "second watch cycle"
            )
            assert second_receipt["receiptId"] != first_receipt["receiptId"]
        finally:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def _wait_for_nth_receipt(chunks: list[str], *, n: int, timeout: float) -> dict | None:
    """Poll the accumulating piped-stdout buffer for the Nth `{...}` JSON
    receipt object (each `_run_cycle` success prints exactly one, pretty
    printed with `indent=2`) and parse it. Each receipt must be flushed or the
    watch client cannot observe it on the pipe."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        combined = "".join(chunks)
        objects = _extract_top_level_json_objects(combined)
        if len(objects) >= n:
            return json.loads(objects[n - 1])
        time.sleep(0.05)
    return None


def _extract_top_level_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    depth = 0
    start = None
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return objects

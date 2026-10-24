"""Exercise real child processes without loading models or using external sockets."""

import os
import signal
import socket
import sys
import time

import pytest

from frisket_models.server_startup import _stop_group, container_address, start_server

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Linux model containers"),
    pytest.mark.realtime,  # Real child processes: assert bounded failure/cleanup.
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_exited_child_fails_without_waiting_for_startup_deadline():
    before = time.monotonic()
    with pytest.raises(RuntimeError, match="broken exited.*23"):
        start_server(
            # subprocess-boundary: observe a real child's exit status.
            [sys.executable, "-c", "raise SystemExit(23)"],
            name="broken",
            host="127.0.0.1",
            port=free_port(),
            timeout=30,
        )
    assert time.monotonic() - before < 3


def test_ready_child_keeps_serving_after_launcher_returns():
    port = free_port()
    process = start_server(
        # subprocess-boundary: readiness must leave the child serving.
        [sys.executable, "-m", "http.server", str(port), "--bind", "0.0.0.0"],
        name="ready",
        host="127.0.0.1",
        port=port,
        timeout=5,
    )
    try:
        assert process.poll() is None
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    finally:
        _stop_group(process, 0.1)
    assert process.poll() is not None


def test_timeout_kills_children_even_when_parent_exits_on_term(tmp_path):
    pid_file = tmp_path / "child.pid"
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent = (
        "import subprocess,sys,time,pathlib; "
        # subprocess-boundary: descendant ignores TERM after its parent exits.
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    before = time.monotonic()
    with pytest.raises(TimeoutError, match="did not listen"):
        start_server(
            # subprocess-boundary: verify process-group timeout cleanup.
            [sys.executable, "-c", parent],
            name="timeout",
            host="127.0.0.1",
            port=free_port(),
            timeout=0.5,
            stop_grace=0.1,
        )
    assert time.monotonic() - before < 3
    pid = int(pid_file.read_text())
    # A killed orphan may briefly be a zombie until container PID 1 reaps it.
    from pathlib import Path

    state = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 2
    while state.exists() and state.read_text().split()[2] != "Z":
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            pytest.fail("startup cleanup left a live descendant")
        time.sleep(0.01)


def test_loopback_only_server_does_not_satisfy_container_readiness():
    # Sandbox network namespaces may have only loopback. A distinct loopback
    # address still verifies that readiness probes the supplied interface.
    port = free_port()
    with pytest.raises(TimeoutError):
        start_server(
            # subprocess-boundary: a real listener binds the wrong interface.
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            name="loopback-only",
            host="127.0.0.2",
            port=port,
            timeout=0.5,
            stop_grace=0.1,
        )


def test_container_address_uses_eth0_when_present():
    if "eth0" not in dict(socket.if_nameindex()).values():
        pytest.skip("isolated test namespace has no eth0")
    assert not container_address().startswith("127.")

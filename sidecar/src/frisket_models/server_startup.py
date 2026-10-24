"""Bounded startup for subprocess-backed model servers on Linux containers."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from collections.abc import Mapping, Sequence


def container_address() -> str:
    """Use eth0, like Modal's proxy; a loopback-only server is not ready."""
    import fcntl
    import struct

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        result = fcntl.ioctl(sock, 0x8915, struct.pack("256s", b"eth0"))
    return socket.inet_ntoa(result[20:24])


def _stop_group(process: subprocess.Popen, grace: float) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # The group leader may exit before its children. Always check the group,
    # rather than treating wait() on the leader as proof that cleanup is done.
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def start_server(
    command: Sequence[str],
    *,
    name: str,
    host: str,
    port: int = 9000,
    timeout: float = 1800,
    env: Mapping[str, str] | None = None,
    stop_grace: float = 5,
) -> subprocess.Popen:
    """Return when reachable, or raise and clean up the entire process group.

    stdout/stderr stay inherited so startup exceptions retain their real cause.
    After success the caller's HTTP runtime owns the server's lifecycle.
    """
    process = subprocess.Popen(command, env=env, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            status = process.poll()
            if status is not None:
                raise RuntimeError(f"{name} exited during startup (exit code {status})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"{name} did not listen on port {port} within {timeout}s"
                )
            try:
                with socket.create_connection((host, port), timeout=min(1, remaining)):
                    if process.poll() is None:
                        return process
            except OSError:
                pass
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except BaseException:
        _stop_group(process, stop_grace)
        raise

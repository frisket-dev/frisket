"""Lifecycle for the optional, app-owned local model server."""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, MutableMapping

from frisket.runtime.model_install import (
    is_installed,
    model_child_environment,
    runtime_dir,
    runtime_python,
)
from frisket.runtime.supervisor import spawn_service, stop_service

LOCAL_MODELS_URL_ENV = "FRISKET_LOCAL_MODELS_URL"
LOCAL_MODELS_TOKEN_ENV = "FRISKET_LOCAL_MODELS_TOKEN"
_POLL_SECONDS = 1.0
_STARTUP_TIMEOUT_SECONDS = 10.0
_RETRY_SECONDS = 30.0

logger = logging.getLogger(__name__)


def _child_environment(source: MutableMapping[str, str]) -> dict[str, str]:
    root = runtime_dir()
    environment = model_child_environment(source)
    environment.update(
        {
            "HF_HOME": str(root / "cache"),
            "PYTHONNOUSERSITE": "1",
        }
    )
    return environment


def wait_until_ready(
    url: str,
    token: str,
    *,
    stopped: Callable[[], bool],
    timeout: float = 45,
) -> bool:
    """Bounded authenticated readiness shared by startup and the setup job."""
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        f"{url}/capabilities", headers={"Authorization": f"Bearer {token}"}
    )
    # The owned endpoint is loopback, never an operator's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline and not stopped():
        try:
            with opener.open(request, timeout=0.25) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.HTTPError):
            pass
        time.sleep(0.05)
    return False


def _reserve_loopback_port() -> int:
    """Ask the OS for a loopback port, then release it for the child bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LocalModelServer:
    """Own one optional model-server tree for a Solo application process.

    Connection values are selected once and published before the web app or
    queue worker is created.  The monitor lets an installation completed by a
    running app become usable without a browser-owned continuation.
    """

    def __init__(self, *, environ: MutableMapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ
        self._port = _reserve_loopback_port()
        self._token = secrets.token_hex(32)
        self._url = f"http://127.0.0.1:{self._port}"
        self._process: subprocess.Popen | None = None
        self._next_attempt = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

        # These are deliberately distinct from the operator-owned gateway
        # variables. Both the backend and its worker inherit this stable pair.
        self._environ[LOCAL_MODELS_URL_ENV] = self._url
        self._environ[LOCAL_MODELS_TOKEN_ENV] = self._token

    @property
    def url(self) -> str:
        return self._url

    def _start_if_installed(self) -> bool:
        with self._lock:
            if self._process is not None:
                if self._process.poll() is None:
                    return True
                logger.error(
                    "local_model_server_exited",
                    extra={
                        "event": "local_model_server_exited",
                        "returncode": self._process.returncode,
                    },
                )
                self._process = None
                self._next_attempt = time.monotonic() + _RETRY_SECONDS
            if time.monotonic() < self._next_attempt or not is_installed():
                return False
            child_env = _child_environment(self._environ)
            child_env[LOCAL_MODELS_TOKEN_ENV] = self._token
            try:
                self._process = spawn_service(
                    [
                        str(runtime_python()),
                        "-m",
                        "uvicorn",
                        "--factory",
                        "frisket_models.local:create_app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(self._port),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stderr,
                    stderr=sys.stderr,
                    env=child_env,
                )
            except (OSError, RuntimeError):
                logger.exception("local_model_server_start_failed")
                self._next_attempt = time.monotonic() + _RETRY_SECONDS
                return False
            process = self._process

        if self._wait_until_ready(process):
            logger.info(
                "local_model_server_ready",
                extra={"event": "local_model_server_ready", "url": self.url},
            )
            return True
        with self._lock:
            if self._process is process:
                self._process = None
            self._next_attempt = time.monotonic() + _RETRY_SECONDS
        try:
            stop_service(process)
        except RuntimeError:
            logger.exception("local_model_server_cleanup_failed")
        logger.error(
            "Local model server did not become ready; will retry. "
            "If this persists, restart Frisket to choose a fresh port.",
            extra={"event": "local_model_server_start_failed", "url": self.url},
        )
        return False

    def _wait_until_ready(self, process: subprocess.Popen) -> bool:
        """Require an authenticated response from the child we just launched."""
        return wait_until_ready(
            self.url,
            self._token,
            stopped=lambda: self._stop.is_set() or process.poll() is not None,
            timeout=_STARTUP_TIMEOUT_SECONDS,
        )

    def start(self) -> None:
        """Start now when installed and keep watching for a later install."""
        self._start_if_installed()
        if self._monitor is not None:
            return

        def monitor() -> None:
            while not self._stop.wait(_POLL_SECONDS):
                try:
                    self._start_if_installed()
                except Exception:  # noqa: BLE001 - optional service cannot kill Solo
                    logger.exception("local_model_server_monitor_failed")

        self._monitor = threading.Thread(
            target=monitor, name="frisket-model-server-monitor", daemon=True
        )
        self._monitor.start()

    def stop(self) -> None:
        """Stop monitoring and synchronously tear down the owned process tree."""
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=2)
        with self._lock:
            process, self._process = self._process, None
        stop_service(process)
        if self._environ.get(LOCAL_MODELS_URL_ENV) == self.url:
            self._environ.pop(LOCAL_MODELS_URL_ENV, None)
        if self._environ.get(LOCAL_MODELS_TOKEN_ENV) == self._token:
            self._environ.pop(LOCAL_MODELS_TOKEN_ENV, None)


__all__ = [
    "LOCAL_MODELS_TOKEN_ENV",
    "LOCAL_MODELS_URL_ENV",
    "LocalModelServer",
]

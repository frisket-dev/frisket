"""Install the optional native model server in a per-user environment."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path

from filelock import FileLock, Timeout

from frisket.runtime.supervisor import spawn_service, stop_service

_RUNTIME_DIR_ENV = "FRISKET_MODEL_RUNTIME_DIR"
_READY_MARKER = ".ready"
_CHILD_ENV_NAMES = frozenset(
    {
        "ALL_PROXY",
        "APPDATA",
        "CURL_CA_BUNDLE",
        "COMSPEC",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USERPROFILE",
        "WINDIR",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        # Explicit offline controls used by the native model runtime.
        "HF_DATASETS_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
    }
)


class ModelInstallCancelled(Exception):
    """Raised after cancellation has stopped an active installer process."""


def model_child_environment(
    environ: Mapping[str, str] | None = None,
    *,
    installer: bool = False,
) -> dict[str, str]:
    """Return the narrow environment shared by installer and model children."""

    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if name in _CHILD_ENV_NAMES
        or (installer and name.startswith("UV_") and not name.startswith("UV_PUBLISH_"))
    }


def runtime_dir() -> Path:
    """Return Frisket's per-user model-server runtime directory."""

    override = os.environ.get(_RUNTIME_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "frisket" / "model-server"


def runtime_python() -> Path:
    """Return the Python executable belonging to the optional runtime."""

    if sys.platform == "win32":
        return runtime_dir() / "venv" / "Scripts" / "python.exe"
    return runtime_dir() / "venv" / "bin" / "python"


def install_lock(*, timeout: float = -1) -> AbstractContextManager[FileLock]:
    """Serialize mutations of the model-server environment across processes."""

    root = runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    return FileLock(root / ".install.lock", timeout=timeout)


def is_installed() -> bool:
    """Passively report whether a completed runtime is present."""

    return (runtime_dir() / _READY_MARKER).is_file() and runtime_python().is_file()


def _probe_install() -> bool:
    """Import the installed server exactly once before publishing readiness."""

    try:
        result = subprocess.run(  # noqa: S603
            [
                str(runtime_python()),
                "-c",
                "from docling.document_converter import DocumentConverter; "
                "from frisket_models.local import create_app; "
                "assert DocumentConverter and callable(create_app)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=model_child_environment(),
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _uv_command() -> list[str]:
    uv = shutil.which("uv")
    if uv is not None:
        return [uv]
    try:
        import uv as uv_package

        return [str(uv_package.find_uv_bin())]
    except (ImportError, AttributeError, OSError) as exc:
        raise RuntimeError("the packaged uv installer is unavailable") from exc


def _acquire_install_lock(
    *, should_cancel: Callable[[], bool], progress: Callable[[str], None]
) -> FileLock:
    lock = FileLock(runtime_dir() / ".install.lock")
    announced = False
    while True:
        if should_cancel():
            raise ModelInstallCancelled
        try:
            lock.acquire(timeout=0)
            return lock
        except Timeout:
            if not announced:
                progress("Waiting for another model-server installation")
                announced = True
            time.sleep(0.1)


def _run_uv(
    argv: list[str],
    *,
    should_cancel: Callable[[], bool],
    progress: Callable[[str], None],
) -> None:
    process = spawn_service(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=model_child_environment(installer=True),
    )
    while process.poll() is None:
        if should_cancel():
            stop_service(process)
            raise ModelInstallCancelled
        time.sleep(0.05)
    if process.returncode:
        raise RuntimeError(
            "model-server installation failed; retry after checking network "
            "connectivity and available disk space"
        )


def install_docling(
    *, should_cancel: Callable[[], bool], progress: Callable[[str], None]
) -> None:
    """Install the packaged model server with only its Docling dependency set."""

    if should_cancel():
        raise ModelInstallCancelled
    runtime_dir().mkdir(parents=True, exist_ok=True)
    lock = _acquire_install_lock(should_cancel=should_cancel, progress=progress)
    try:
        if is_installed():
            progress("Docling model server is already installed")
            return
        root = runtime_dir()
        (root / _READY_MARKER).unlink(missing_ok=True)
        uv = _uv_command()
        progress("Creating the private model-server environment")
        venv_argv = [*uv, "venv", "--python", sys.executable, str(root / "venv")]
        if (root / "venv").exists():
            venv_argv.append("--clear")
        _run_uv(
            venv_argv,
            should_cancel=should_cancel,
            progress=progress,
        )
        version = importlib.metadata.version("frisket-data")
        install_argv = [
            *uv,
            "pip",
            "install",
            "--python",
            str(runtime_python()),
        ]
        if sys.platform in {"linux", "win32"}:
            install_argv.extend(["--torch-backend", "cpu"])
        install_argv.append(f"frisket-data[models]=={version}")
        progress("Installing the CPU Docling runtime")
        _run_uv(install_argv, should_cancel=should_cancel, progress=progress)
        if not _probe_install():
            raise RuntimeError(
                "installed model-server environment failed its import check"
            )
        (root / _READY_MARKER).touch()
        progress("Docling model server installed")
    finally:
        lock.release()


__all__ = [
    "ModelInstallCancelled",
    "install_docling",
    "install_lock",
    "is_installed",
    "model_child_environment",
    "runtime_dir",
    "runtime_python",
]

"""Explicit installation and passive readiness for Playwright's Chromium."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from frisket.runtime.model_install import ModelInstallCancelled, model_child_environment
from frisket.runtime.supervisor import spawn_service, stop_service


def chromium_library_available() -> bool:
    """Whether this Python environment includes the Playwright library."""
    return importlib.util.find_spec("playwright") is not None


def _child_environment() -> dict[str, str]:
    env = model_child_environment()
    # Preserve browser location, enterprise download mirrors and trust settings,
    # without passing arbitrary application credentials into installer children.
    for name in (
        "XDG_CACHE_HOME",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_DOWNLOAD_HOST",
        "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
        "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT",
        "NODE_EXTRA_CA_CERTS",
    ):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


@lru_cache(maxsize=8)
def _installation_paths(
    python: str, environment: tuple[tuple[str, str], ...]
) -> tuple[Path, ...]:
    result = subprocess.run(  # noqa: S603
        [python, "-I", "-m", "playwright", "install", "--dry-run", "chromium"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=10,
        env=dict(environment),
    )
    paths = tuple(
        Path(line.split(":", 1)[1].strip())
        for line in result.stdout.splitlines()
        if line.strip().startswith("Install location:")
    )
    if result.returncode or not paths:
        # Exceptions are not cached: a repaired installation can be retried.
        raise RuntimeError("Playwright could not identify its browser installation")
    return paths


def chromium_ready() -> bool:
    """Check the current Playwright revision's completion markers without launch."""
    if not chromium_library_available():
        return False
    try:
        paths = _installation_paths(
            sys.executable, tuple(sorted(_child_environment().items()))
        )
        return all((path / "INSTALLATION_COMPLETE").is_file() for path in paths)
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        return False


def install_chromium(
    *, should_cancel: Callable[[], bool], progress: Callable[[str], None]
) -> None:
    """Install this environment's Chromium; cancellation stops the process tree."""
    if should_cancel():
        raise ModelInstallCancelled
    if not chromium_library_available():
        raise RuntimeError("Playwright is unavailable; reinstall Frisket")
    if chromium_ready():
        progress("Chromium is already installed")
        return
    progress("Downloading Chromium browser")
    process = spawn_service(
        [sys.executable, "-I", "-m", "playwright", "install", "chromium"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_child_environment(),
    )
    try:
        while process.poll() is None:
            if should_cancel():
                raise ModelInstallCancelled
            time.sleep(0.05)
    finally:
        # Also consumes the Windows guardian cleanup proof on normal completion.
        stop_service(process)
    if should_cancel():
        raise ModelInstallCancelled
    if process.returncode:
        raise RuntimeError(
            "Chromium installation failed; retry after checking network "
            "connectivity and available disk space"
        )
    if not chromium_ready():
        raise RuntimeError(
            "Chromium installation did not complete; retry browser setup"
        )
    progress("Chromium browser installed")

"""On-demand environment for the offline Opus-MT worker."""

from __future__ import annotations

from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path
import sys

from frisket.runtime import model_install
from frisket.runtime.launch import PythonRuntime

RUNTIME_PROFILE = "translation-v1"


def runtime_dir() -> Path:
    return model_install.runtime_dir() / RUNTIME_PROFILE


def runtime_python() -> Path:
    return model_install._runtime_python(runtime_dir())


def is_installed() -> bool:
    return model_install._is_installed(runtime_dir())


def host_present() -> bool:
    """Check metadata without importing native translation libraries."""
    try:
        return all(
            (sys.modules.get(name) is not None or find_spec(name) is not None)
            for name in ("ctranslate2", "sentencepiece")
        )
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def runtime_present() -> bool:
    return host_present() or is_installed()


def runtime_for_worker(kind: str = "opus-mt-session") -> PythonRuntime:
    if kind != "opus-mt-session":
        raise ValueError("unknown translation worker")
    current = PythonRuntime.current()
    if host_present():
        return current
    return PythonRuntime(runtime_python(), current.app_code_root)


def _probe_install() -> bool:
    return model_install._probe_python(
        runtime_python(), "import ctranslate2, sentencepiece"
    )


def install_translation(
    *, should_cancel: Callable[[], bool], progress: Callable[[str], None]
) -> None:
    model_install._install_profile(
        model_install._InstallProfile(
            root=runtime_dir(),
            extra=None,
            probe=_probe_install,
            already_installed_message="Local translation components are installed",
            create_message="Creating the private local translation environment",
            install_message="Installing local translation components",
            installed_message="Local translation components installed",
            probe_failure_message="installed translation runtime failed its import check",
            requirements_file="translation-requirements.txt",
            require_hashes=True,
        ),
        should_cancel=should_cancel,
        progress=progress,
    )

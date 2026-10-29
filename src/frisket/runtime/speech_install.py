"""One on-demand environment for the local Parakeet and Whisper workers."""

from __future__ import annotations

from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path

from frisket.runtime import model_install
from frisket.runtime.launch import PythonRuntime

RUNTIME_PROFILE = "speech-v1"
_WORKER_ENGINES = {
    "parakeet-session": "parakeet-tdt",
    "parakeet-artifacts": "parakeet-tdt",
    "faster-whisper": "faster_whisper",
}
_ENGINE_MODULES = {
    "parakeet-tdt": ("onnx_asr", "onnxruntime", "huggingface_hub", "av"),
    "faster_whisper": ("faster_whisper",),
}


def runtime_dir() -> Path:
    return model_install.runtime_dir() / RUNTIME_PROFILE


def runtime_python() -> Path:
    return model_install._runtime_python(runtime_dir())


def is_installed() -> bool:
    return model_install._is_installed(runtime_dir())


def _host_present(engine: str) -> bool:
    modules = _ENGINE_MODULES[engine]
    try:
        return all(find_spec(module) is not None for module in modules)
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def runtime_present(engine: str) -> bool:
    """Probe without launching children, importing native libraries, or installing."""
    return _host_present(engine) or is_installed()


def runtime_for_worker(kind: str) -> PythonRuntime:
    """Preserve ordinary CLI installs; use the owned environment when needed."""
    current = PythonRuntime.current()
    if _host_present(_WORKER_ENGINES[kind]):
        return current
    return PythonRuntime(runtime_python(), current.app_code_root)


def _probe_install() -> bool:
    return model_install._probe_python(
        runtime_python(),
        "import av, faster_whisper, huggingface_hub, onnx_asr, onnxruntime, pydantic; "
        "assert onnx_asr.__version__ == '0.11.0'",
    )


def install_speech(
    *, should_cancel: Callable[[], bool], progress: Callable[[str], None]
) -> None:
    model_install._install_profile(
        model_install._InstallProfile(
            root=runtime_dir(),
            extra=None,
            probe=_probe_install,
            already_installed_message="Local transcription components are installed",
            create_message="Creating the private local transcription environment",
            install_message="Installing local transcription components",
            installed_message="Local transcription components installed",
            probe_failure_message="installed transcription runtime failed its import check",
            requirements_file="speech-requirements.txt",
            require_hashes=True,
        ),
        should_cancel=should_cancel,
        progress=progress,
    )

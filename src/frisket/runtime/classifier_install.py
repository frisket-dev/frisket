"""Install the optional local classifiers in their own per-user environment."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from frisket.runtime import model_install

_PROFILE_NAME = "classification-v1"
_PROBE = (
    "from gliclass import GLiClassModel, ZeroShotClassificationPipeline; "
    "from transformers import AutoTokenizer, Qwen3_5ForConditionalGeneration; "
    "from safetensors.torch import load_file; "
    "import frisket_models, torch, torchvision; "
    "assert GLiClassModel and ZeroShotClassificationPipeline and AutoTokenizer; "
    "assert Qwen3_5ForConditionalGeneration and load_file and frisket_models; "
    "assert torch and torchvision"
)


def runtime_dir() -> Path:
    """Return the versioned classifier profile below the native-runtime root."""

    return model_install.runtime_dir() / _PROFILE_NAME


def runtime_python() -> Path:
    """Return the Python executable belonging to the classifier profile."""

    return model_install._runtime_python(runtime_dir())


def is_installed() -> bool:
    """Passively report whether the classifier profile completed installation."""

    return model_install._is_installed(runtime_dir())


def _probe_install() -> bool:
    return model_install._probe_python(runtime_python(), _PROBE)


def install_classifiers(
    *, should_cancel: Callable[[], bool], progress: Callable[[str], None]
) -> None:
    """Install the fixed CPU classifier dependency profile on demand."""

    model_install._install_profile(
        model_install._InstallProfile(
            root=runtime_dir(),
            extra="classify",
            probe=_probe_install,
            already_installed_message="Local classifiers are already installed",
            create_message="Creating the private classifier environment",
            install_message="Installing the CPU classifier runtime",
            installed_message="Local classifiers installed",
            probe_failure_message=(
                "installed classifier environment failed its import check"
            ),
            constraint_file="classify-constraints.txt",
        ),
        should_cancel=should_cancel,
        progress=progress,
    )


__all__ = ["install_classifiers", "is_installed", "runtime_dir", "runtime_python"]

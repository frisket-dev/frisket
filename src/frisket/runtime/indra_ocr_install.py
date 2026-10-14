"""Install Indra OCR in its own per-user environment."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from frisket.engine._workers.indra_ocr_model import PACKAGE_VERSION
from frisket.runtime import model_install

_PROFILE_NAME = "indra-ocr-v1"
_PROBE = (
    "import indra_ocr; "
    f"assert indra_ocr.__version__ == {PACKAGE_VERSION!r}; "
    "assert callable(indra_ocr.load)"
)


def runtime_dir() -> Path:
    return model_install.runtime_dir() / _PROFILE_NAME


def runtime_python() -> Path:
    return model_install._runtime_python(runtime_dir())


def is_installed() -> bool:
    return model_install._is_installed(runtime_dir())


def _probe_install() -> bool:
    return model_install._probe_python(runtime_python(), _PROBE)


def install_indra_ocr(
    *, should_cancel: Callable[[], bool], progress: Callable[[str], None]
) -> None:
    model_install._install_profile(
        model_install._InstallProfile(
            root=runtime_dir(),
            extra="ocr-indra",
            probe=_probe_install,
            already_installed_message="Indra OCR is already installed",
            create_message="Creating the private Indra OCR environment",
            install_message="Installing the CPU Indra OCR runtime",
            installed_message="Indra OCR installed",
            probe_failure_message="installed Indra OCR runtime failed its import check",
            constraint_file="indra-ocr-constraints.txt",
        ),
        should_cancel=should_cancel,
        progress=progress,
    )


__all__ = ["install_indra_ocr", "is_installed", "runtime_dir", "runtime_python"]

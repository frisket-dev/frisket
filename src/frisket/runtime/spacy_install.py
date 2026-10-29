"""Install spaCy in its fixed, optional per-user environment."""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
import importlib.util

from frisket.runtime import model_install

if TYPE_CHECKING:
    from frisket.runtime.launch import PythonRuntime


def runtime_dir() -> Path:
    return model_install.runtime_dir() / "spacy-v1"


def runtime_python() -> Path:
    return model_install._runtime_python(runtime_dir())


def is_installed() -> bool:
    return model_install._is_installed(runtime_dir())


def runtime_present() -> bool:
    """Passively report host or completed private-runtime readiness."""
    return importlib.util.find_spec("spacy") is not None or is_installed()


def runtime_for_worker() -> "PythonRuntime":
    """Resolve only the fixed private interpreter for the spaCy worker."""
    from frisket.runtime.launch import PythonRuntime

    return PythonRuntime(runtime_python(), PythonRuntime.current().app_code_root)


def install_spacy(
    *, should_cancel: Callable[[], bool], progress: Callable[[str], None]
) -> None:
    model_install._install_profile(
        model_install._InstallProfile(
            root=runtime_dir(),
            extra=None,
            requirements_file="spacy-requirements.txt",
            require_hashes=True,
            probe=lambda: model_install._probe_python(
                runtime_python(),
                "import spacy; assert spacy.__version__.startswith('3.8.')",
            ),
            already_installed_message="spaCy is already installed",
            create_message="Creating the private spaCy environment",
            install_message="Installing the spaCy runtime",
            installed_message="spaCy installed",
            probe_failure_message="installed spaCy environment failed its import check",
        ),
        should_cancel=should_cancel,
        progress=progress,
    )

"""On-demand installation of the local multimodal retrieval runtime."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from frisket.engine._workers.embeddinggemma_model import (
    PILLOW_VERSION,
    RUNTIME_PROFILE,
    TORCH_VERSION,
    TORCHVISION_VERSION,
    TRANSFORMERS_VERSION,
)
from frisket.runtime import model_install


def runtime_dir() -> Path:
    return model_install.runtime_dir() / RUNTIME_PROFILE


def runtime_python() -> Path:
    return model_install._runtime_python(runtime_dir())


def is_installed() -> bool:
    return model_install._is_installed(runtime_dir())


def _probe_install() -> bool:
    return model_install._probe_python(
        runtime_python(),
        "from transformers import AutoProcessor, AutoModel; "
        "from transformers.models.embedding_gemma2 import EmbeddingGemma2Model; "
        "import PIL, torch, torchvision, transformers; "
        f"assert transformers.__version__ == {TRANSFORMERS_VERSION!r}; "
        f"assert PIL.__version__ == {PILLOW_VERSION!r}; "
        f"assert torch.__version__.split('+')[0] == {TORCH_VERSION!r}; "
        f"assert torchvision.__version__.split('+')[0] == {TORCHVISION_VERSION!r}",
    )


def install_embeddinggemma(
    *, should_cancel: Callable[[], bool], progress: Callable[[str], None]
) -> None:
    model_install._install_profile(
        model_install._InstallProfile(
            root=runtime_dir(),
            extra=None,
            probe=_probe_install,
            already_installed_message="Multimodal search is already installed",
            create_message="Creating the private multimodal search environment",
            install_message="Installing the multimodal search runtime",
            installed_message="Multimodal search installed",
            probe_failure_message="installed search runtime failed its import check",
            requirements_file="retrieval-constraints.txt",
        ),
        should_cancel=should_cancel,
        progress=progress,
    )

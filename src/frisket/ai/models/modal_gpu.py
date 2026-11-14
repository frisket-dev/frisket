"""Deployment-time GPU preferences for Modal's native capacity fallback."""

from __future__ import annotations

import os

# Ampere/Ada alternatives support BF16 and the workers' pinned CUDA kernels.
# Keep larger GPUs last; do not let `any` select an incompatible T4.
OCR_GPUS = ("L4", "A10", "L40S", "A100-40GB", "A100-80GB")
RESIDENT_GPUS = ("T4", *OCR_GPUS)
MOSS_GPUS = ("A10", "L4", "L40S", "A100-40GB", "A100-80GB")


def gpu_preferences(variable: str, default: tuple[str, ...]) -> list[str]:
    """Accept one GPU or an ordered comma-separated override; Modal validates IDs."""

    configured = os.environ.get(variable)
    if configured is None:
        return list(default)
    choices = [choice.strip() for choice in configured.split(",")]
    if not all(choices):
        raise ValueError(f"{variable} must contain non-empty GPU choices")
    return choices

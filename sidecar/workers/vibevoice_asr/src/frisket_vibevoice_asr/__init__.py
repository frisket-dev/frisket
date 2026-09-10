"""Fixed native VibeVoice-ASR worker leaf."""

from .adapter import (
    DESCRIPTOR,
    ENGINE,
    MODEL_ID,
    MODEL_REVISION,
    REGISTRATION,
    TRANSFORMERS_VERSION,
    VibeVoiceAsrAdapter,
    VibeVoiceAsrConfig,
    build_registration,
)

__all__ = [
    "DESCRIPTOR",
    "ENGINE",
    "MODEL_ID",
    "MODEL_REVISION",
    "REGISTRATION",
    "TRANSFORMERS_VERSION",
    "VibeVoiceAsrAdapter",
    "VibeVoiceAsrConfig",
    "build_registration",
]

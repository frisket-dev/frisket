"""Fixed Whisper Turbo worker leaf."""

from .adapter import (
    DESCRIPTOR,
    ENGINE,
    FASTER_WHISPER_VERSION,
    MODEL_ID,
    MODEL_REVISION,
    REGISTRATION,
    WhisperTurboAdapter,
    WhisperTurboConfig,
    build_registration,
)

__all__ = [
    "DESCRIPTOR",
    "ENGINE",
    "FASTER_WHISPER_VERSION",
    "MODEL_ID",
    "MODEL_REVISION",
    "REGISTRATION",
    "WhisperTurboAdapter",
    "WhisperTurboConfig",
    "build_registration",
]

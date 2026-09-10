"""Import-light identity for the fixed Whisper Turbo worker."""

from __future__ import annotations

from frisket_models.transcription.config import WorkerDefinition
from frisket_models.transcription.contract import (
    DiarizationMode,
    SpeakerHint,
    TranscriptionEngineDescriptor,
    TranscriptionOptionSupport,
)

WHISPER_TURBO_ENGINE = "whisper-turbo"
WHISPER_TURBO_ENV_PREFIX = "FRISKET_TRANSCRIPTION_WHISPER_TURBO_WORKER"
WHISPER_TURBO_PACKAGE_VERSION = "1.2.1"
WHISPER_TURBO_MODEL_ID = "dropbox-dash/faster-whisper-large-v3-turbo"
WHISPER_TURBO_MODEL_REVISION = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
WHISPER_TURBO_VAD_DEFAULT = True

WHISPER_TURBO_DESCRIPTOR = TranscriptionEngineDescriptor(
    engine=WHISPER_TURBO_ENGINE,
    model_ids=[WHISPER_TURBO_MODEL_ID],
    revision=WHISPER_TURBO_MODEL_REVISION,
    runtime_image_id=None,
    options=TranscriptionOptionSupport(
        diarization_mode=DiarizationMode.NONE,
        speaker_hint=SpeakerHint.NONE,
        language=True,
        model_size=False,
        vad=True,
        context=True,
    ),
)

WHISPER_TURBO_DEFINITION = WorkerDefinition(
    engine=WHISPER_TURBO_ENGINE,
    env_prefix=WHISPER_TURBO_ENV_PREFIX,
    descriptor=WHISPER_TURBO_DESCRIPTOR,
)

__all__ = [
    "WHISPER_TURBO_DEFINITION",
    "WHISPER_TURBO_DESCRIPTOR",
    "WHISPER_TURBO_ENGINE",
    "WHISPER_TURBO_ENV_PREFIX",
    "WHISPER_TURBO_MODEL_ID",
    "WHISPER_TURBO_MODEL_REVISION",
    "WHISPER_TURBO_PACKAGE_VERSION",
    "WHISPER_TURBO_VAD_DEFAULT",
]

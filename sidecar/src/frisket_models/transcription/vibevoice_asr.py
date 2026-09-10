"""Import-light identity for the fixed VibeVoice-ASR GPU worker."""

from __future__ import annotations

from frisket_models.transcription.contract import (
    DiarizationMode,
    SpeakerHint,
    TranscriptionEngineDescriptor,
    TranscriptionOptionSupport,
)

VIBEVOICE_ASR_ENGINE = "vibevoice-asr"
VIBEVOICE_ASR_ENV_PREFIX = "FRISKET_TRANSCRIPTION_VIBEVOICE_ASR_WORKER"
VIBEVOICE_ASR_PACKAGE_VERSION = "5.3.0"
VIBEVOICE_ASR_MODEL_ID = "microsoft/VibeVoice-ASR-HF"
VIBEVOICE_ASR_MODEL_REVISION = "f22241c2062b3b25272bf117397e03d73381037a"

VIBEVOICE_ASR_DESCRIPTOR = TranscriptionEngineDescriptor(
    engine=VIBEVOICE_ASR_ENGINE,
    model_ids=[VIBEVOICE_ASR_MODEL_ID],
    revision=VIBEVOICE_ASR_MODEL_REVISION,
    runtime_image_id=None,
    options=TranscriptionOptionSupport(
        diarization_mode=DiarizationMode.INTRINSIC,
        speaker_hint=SpeakerHint.NONE,
        language=False,
        model_size=False,
        vad=False,
        context=True,
    ),
)

__all__ = [
    "VIBEVOICE_ASR_DESCRIPTOR",
    "VIBEVOICE_ASR_ENGINE",
    "VIBEVOICE_ASR_ENV_PREFIX",
    "VIBEVOICE_ASR_MODEL_ID",
    "VIBEVOICE_ASR_MODEL_REVISION",
    "VIBEVOICE_ASR_PACKAGE_VERSION",
]

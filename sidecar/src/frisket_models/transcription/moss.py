"""Import-light identity for the production MOSS transcription worker."""

from __future__ import annotations

from frisket_models.transcription.contract import (
    TranscriptionEngineDescriptor,
    TranscriptionOptionSupport,
)

MOSS_ENGINE = "moss"
MOSS_ENV_PREFIX = "FRISKET_TRANSCRIPTION_MOSS_WORKER"
MOSS_MODEL_ID = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
MOSS_MODEL_REVISION = "4a1af868018e7974197f4f018730758012b28c27"

MOSS_DESCRIPTOR = TranscriptionEngineDescriptor(
    engine=MOSS_ENGINE,
    model_ids=[MOSS_MODEL_ID],
    revision=MOSS_MODEL_REVISION,
    runtime_image_id=None,
    options=TranscriptionOptionSupport(
        diarization_mode="intrinsic",
        speaker_hint="none",
        language=False,
        model_size=False,
        vad=False,
        context=True,
    ),
)

__all__ = [
    "MOSS_DESCRIPTOR",
    "MOSS_ENGINE",
    "MOSS_ENV_PREFIX",
    "MOSS_MODEL_ID",
    "MOSS_MODEL_REVISION",
]

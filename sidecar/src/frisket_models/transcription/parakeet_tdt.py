"""Fixed identity for the hosted Parakeet TDT worker."""

from __future__ import annotations

from frisket_models.transcription.contract import (
    DiarizationMode,
    SpeakerHint,
    TranscriptionEngineDescriptor,
    TranscriptionOptionSupport,
)

PARAKEET_ENGINE = "parakeet-tdt"
PARAKEET_ENV_PREFIX = "FRISKET_TRANSCRIPTION_PARAKEET_TDT_WORKER"
PARAKEET_MODEL_ID = "istupakov/parakeet-tdt-0.6b-v2-onnx"
PARAKEET_MODEL_REVISION = "0bbb45a3365852604aef28b538a8f066f4ccaa85"
PARAKEET_VAD_ID = "istupakov/silero-vad-onnx"
PARAKEET_VAD_REVISION = "b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6"
DIARIZER_MODEL_ID = "nvidia/diar_streaming_sortformer_4spk-v2.1"
DIARIZER_MODEL_REVISION = "fafaab5faa1617a0ca52d38dd3dc4bd636800d3d"
DIARIZER_FILENAME = "diar_streaming_sortformer_4spk-v2.1.nemo"

PARAKEET_REVISION = (
    f"asr:{PARAKEET_MODEL_REVISION};vad:{PARAKEET_VAD_REVISION};"
    f"diarizer:{DIARIZER_MODEL_REVISION}"
)

PARAKEET_DESCRIPTOR = TranscriptionEngineDescriptor(
    engine=PARAKEET_ENGINE,
    model_ids=[PARAKEET_MODEL_ID, PARAKEET_VAD_ID, DIARIZER_MODEL_ID],
    revision=PARAKEET_REVISION,
    runtime_image_id=None,
    options=TranscriptionOptionSupport(
        diarization_mode=DiarizationMode.OPTIONAL,
        speaker_hint=SpeakerHint.NONE,
        language=False,
        model_size=False,
        vad=True,
        context=False,
    ),
)

__all__ = [
    "DIARIZER_FILENAME",
    "DIARIZER_MODEL_ID",
    "DIARIZER_MODEL_REVISION",
    "PARAKEET_DESCRIPTOR",
    "PARAKEET_ENGINE",
    "PARAKEET_ENV_PREFIX",
    "PARAKEET_MODEL_ID",
    "PARAKEET_MODEL_REVISION",
    "PARAKEET_REVISION",
    "PARAKEET_VAD_ID",
    "PARAKEET_VAD_REVISION",
]

from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_DESCRIPTOR,
    WHISPER_TURBO_ENGINE,
    WHISPER_TURBO_MODEL_ID,
    WHISPER_TURBO_MODEL_REVISION,
)


def test_whisper_turbo_identity_is_exact_and_fixed() -> None:
    assert WHISPER_TURBO_ENGINE == "whisper-turbo"
    assert WHISPER_TURBO_MODEL_ID == "dropbox-dash/faster-whisper-large-v3-turbo"
    assert WHISPER_TURBO_MODEL_REVISION == "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
    assert WHISPER_TURBO_DESCRIPTOR.engine == WHISPER_TURBO_ENGINE
    assert WHISPER_TURBO_DESCRIPTOR.model_ids == [WHISPER_TURBO_MODEL_ID]
    assert WHISPER_TURBO_DESCRIPTOR.options.model_size is False

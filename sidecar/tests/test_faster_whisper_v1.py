"""Resident Faster Whisper implements the shared transcription v1 contract."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from frisket_models.engines import (
    WHISPER_DESCRIPTOR,
    WHISPER_MODEL_ID,
    WHISPER_MODEL_REVISION,
    default_registry,
    load_faster_whisper,
)
from frisket_models.transcription.contract import TranscribeOptions


def test_resident_adapter_pins_model_and_forwards_supported_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    class _Info:
        language = "fr"
        duration = 0.5

    class _Segment:
        start = 0.0
        end = 0.5
        text = " bonjour "
        words = None

    class _WhisperModel:
        def __init__(self, model: str, **kwargs: object) -> None:
            observed["model"] = model
            observed["load"] = kwargs

        def transcribe(self, path: str, **kwargs: object):
            observed["path"] = path
            observed["options"] = kwargs
            return [_Segment()], _Info()

    module = ModuleType("faster_whisper")
    module.WhisperModel = _WhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF fixture")

    adapter = load_faster_whisper()
    options = TranscribeOptions(
        language="fr",
        vad=False,
        context="Frisket",
    )
    result = adapter(audio, options)

    assert observed["model"] == WHISPER_MODEL_ID
    assert observed["load"] == {
        "revision": WHISPER_MODEL_REVISION,
        "device": "cpu",
        "compute_type": "int8",
    }
    assert observed["options"] == {
        "language": "fr",
        "vad_filter": False,
        "initial_prompt": "Frisket",
        "word_timestamps": True,
    }
    assert result.engine == WHISPER_DESCRIPTOR.engine
    assert result.model_ids == [WHISPER_MODEL_ID]
    assert result.revision == WHISPER_MODEL_REVISION
    assert result.accepted_options == options.supplied_options()


def test_resident_adapter_rejects_authorable_model_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WhisperModel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    module = ModuleType("faster_whisper")
    module.WhisperModel = _WhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    adapter = load_faster_whisper()
    with pytest.raises(
        ValueError, match="unsupported transcription option.*model_size"
    ):
        adapter(Path("unused.wav"), TranscribeOptions(model_size="small"))


def test_resident_whisper_turbo_names_the_existing_transcribe_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "frisket_models.engines.importlib.util.find_spec", lambda _name: None
    )

    with pytest.raises(RuntimeError, match=r"frisket-models\[transcribe\]"):
        default_registry().get("whisper-turbo").get()

"""Capability-owned media options retain semantic and supplied-field rules."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from frisket.actions.media_options import OcrOptions, TranscriptionOptions
from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    TRANSCRIBE_ENGINE_TABLE,
)
from frisket.contracts.transcription_sidecar import TRANSCRIPTION_CONTEXT_MAX_CHARS


MAI = "openrouter/microsoft/mai-transcribe-2"


@pytest.mark.parametrize("model", [OcrOptions, TranscriptionOptions])
@pytest.mark.parametrize(
    "field", ["engine", "sheet_id", "source", "output_name", "confirmed"]
)
def test_options_reject_request_envelope_fields(model, field):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate({field: "not an option"})


@pytest.mark.parametrize("engine", [entry.id for entry in OCR_ENGINE_TABLE])
def test_ocr_defaults_remain_engine_independent(engine):
    assert OcrOptions().normalize(engine) == {"dpi": 200, "searchable_pdf": False}


def test_ocr_trimmed_language_and_bounds():
    assert OcrOptions(language="  eng  ", dpi=600, searchable_pdf=True).normalize(
        "tess"
    ) == {
        "language": "eng",
        "dpi": 600,
        "searchable_pdf": True,
    }


@pytest.mark.parametrize("value", [49, 601, True, 200.0, "200"])
def test_ocr_dpi_is_strict_bounded_integer(value):
    with pytest.raises(ValidationError):
        OcrOptions(dpi=value)


def test_ocr_remote_geometry_refusal():
    assert OcrOptions().normalize("gemini/example")["searchable_pdf"] is False
    with pytest.raises(ValueError, match="searchable_pdf_unsupported_engine"):
        OcrOptions(searchable_pdf=True).normalize("gemini/example")


@pytest.mark.parametrize("engine", [entry.id for entry in TRANSCRIBE_ENGINE_TABLE])
def test_transcription_omitted_vad_never_materializes(engine):
    options = TranscriptionOptions()
    before = options.model_dump()
    assert "vad" not in options.normalize(engine)
    assert options.model_dump() == before
    assert options.model_fields_set == set()


@pytest.mark.parametrize(
    ("engine", "raw", "expected"),
    [
        ("faster_whisper", {}, {}),
        (
            "whisper",
            {"vad": False, "model_size": " base "},
            {"vad": False, "model_size": "base"},
        ),
        ("whisper-turbo", {"vad": True}, {"vad": True}),
        ("parakeet-tdt", {}, {"diarize": False}),
        ("parakeet-tdt", {"diarize": True, "language": ["en"]}, {"diarize": True}),
        ("moss", {"context": " Frisket "}, {"context": "Frisket"}),
        ("vibevoice-asr", {}, {}),
        (MAI, {}, {"diarize": True, "clean": False}),
        (MAI, {"diarize": False, "clean": True}, {"diarize": False, "clean": True}),
        ("openai/whisper-1", {"language": " en "}, {"language": ["en"]}),
    ],
)
def test_normalized_semantic_kwargs(engine, raw, expected):
    options = TranscriptionOptions.model_validate(raw)
    assert options.normalize(engine) == expected
    assert options.normalize(engine) == expected  # No presence-changing normalization.


@pytest.mark.parametrize("language", [None, [], ["auto"], "auto", ["", "auto", "auto"]])
def test_language_auto_spellings_normalize_to_absence(language):
    assert TranscriptionOptions(language=language).normalize("faster_whisper") == {}


@pytest.mark.parametrize(
    ("engine", "language"),
    [
        ("faster_whisper", ["en", "fr"]),
        ("faster_whisper", ["auto", "en"]),
        ("faster_whisper", ["not-a-language"]),
        ("parakeet-tdt", ["fr"]),
        ("moss", ["en"]),
        ("vibevoice-asr", ["en"]),
        (MAI, ["tl"]),
    ],
)
def test_engine_language_selection_refuses(engine, language):
    with pytest.raises(ValueError, match="invalid_language_selection"):
        TranscriptionOptions(language=language).normalize(engine)


@pytest.mark.parametrize(
    ("engine", "raw"),
    [
        ("moss", {"vad": False}),
        ("moss", {"diarize": False}),
        ("moss", {"num_speakers": None}),
        ("vibevoice-asr", {"model_size": None}),
        ("openai/whisper-1", {"context": None}),
        ("faster_whisper", {"clean": False}),
        ("parakeet-tdt", {"diarize": True, "num_speakers": 2}),
    ],
)
def test_explicit_unsupported_options_refuse_even_null_or_false(engine, raw):
    with pytest.raises(ValueError, match="transcription_option_unavailable"):
        TranscriptionOptions.model_validate(raw).normalize(engine)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({"num_speakers": 2}, "invalid_params"),
        ({"diarize": True, "num_speakers": 2, "max_speakers": 3}, "invalid_params"),
        ({"diarize": True, "min_speakers": 3, "max_speakers": 2}, "invalid_params"),
        ({"diarize": True, "max_speakers": 5}, "diarization_speaker_cap"),
    ],
)
def test_speaker_constraints_remain_closed(raw, error):
    with pytest.raises(ValueError, match=error):
        TranscriptionOptions.model_validate(raw).normalize("parakeet-tdt")


def test_non_diarizing_engine_rejects_diarization():
    with pytest.raises(ValueError, match="diarization_unavailable"):
        TranscriptionOptions(diarize=True).normalize("faster_whisper")


def test_context_trim_bound_and_null_semantics():
    assert TranscriptionOptions(context=None).normalize("moss") == {}
    at_cap = "x" * TRANSCRIPTION_CONTEXT_MAX_CHARS
    assert TranscriptionOptions(context=f" {at_cap} ").normalize("moss") == {
        "context": at_cap
    }
    for invalid in (" ", "x" * (TRANSCRIPTION_CONTEXT_MAX_CHARS + 1), 42):
        with pytest.raises(ValidationError):
            TranscriptionOptions(context=invalid)


@pytest.mark.parametrize("field", ["vad", "diarize", "clean"])
def test_booleans_are_strict(field):
    with pytest.raises(ValidationError):
        TranscriptionOptions.model_validate({field: 1})


@pytest.mark.parametrize("model", [OcrOptions, TranscriptionOptions])
@pytest.mark.parametrize("engine", [None, "", "unknown", "local"])
def test_unknown_or_retired_engine_refuses(model, engine):
    with pytest.raises(ValueError, match="invalid_.*engine"):
        model().normalize(engine)


def test_mutation_cannot_bypass_actual_option_validation():
    ocr = OcrOptions()
    ocr.dpi = 1000
    with pytest.raises(ValidationError):
        ocr.normalize("rapidocr")
    options = TranscriptionOptions(language=["en"])
    options.language.append("fr")
    with pytest.raises(ValueError, match="invalid_language_selection"):
        options.normalize("faster_whisper")
    options = TranscriptionOptions()
    options.vad = False
    with pytest.raises(ValueError, match="transcription_option_unavailable"):
        options.normalize("moss")


def test_parakeet_diarization_survives_into_target_placement():
    from frisket.execution.definitions import build_static_targets
    from frisket.execution.resolver import support_inability

    options = TranscriptionOptions(diarize=True).normalize("parakeet-tdt")
    supports = {
        target.id: support
        for target in build_static_targets()
        for support in target.engines
        if support.engine == "parakeet-tdt" and support.capability == "transcribe"
    }
    assert support_inability(supports["local-onnx"], options) == "diarization"
    assert support_inability(supports["models-gateway"], options) is None
    assert options == {"diarize": True}


def test_normalization_returns_detached_values():
    options = TranscriptionOptions(language=["en"])
    before = deepcopy(options.model_dump())
    normalized = options.normalize("faster_whisper")
    normalized["language"].append("fr")
    assert options.model_dump() == before

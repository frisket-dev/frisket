from frisket.actions.media import TRANSCRIBE, TranscribeParams
from frisket.actions.media_types import TranscribedMedia


def test_transcript_segments_preserve_absence_and_engine_extras():
    value = {
        "text": "hello",
        "segments": [
            {"text": "hello", "start": 0.0, "end": 1.0, "engine_confidence": 0.8},
            {"words": [{"word": "hello", "probability": 0.9}], "speaker": "S1"},
        ],
    }
    result = TranscribedMedia.model_validate(value).model_dump(
        mode="json", exclude_unset=True
    )
    assert result == value
    assert "speaker" not in result["segments"][0]
    assert "start" not in result["segments"][1]["words"][0]


def test_transcript_catalog_exposes_optional_segment_and_word_fields():
    fields = TRANSCRIBE.run.resolve_output_fields(TranscribeParams(source="audio"))
    schema = next(field for field in fields if field.key == "segments").schema
    segment = schema["$defs"]["TranscriptSegment"]
    word = schema["$defs"]["TranscriptWord"]
    assert segment["properties"]["speaker"]["type"] == "string"
    assert segment["properties"]["speaker_confidence"]["type"] == "string"
    assert segment.get("required", []) == []
    assert word.get("required", []) == []
    assert set(word["properties"]) == {"word", "start", "end"}
    assert segment["additionalProperties"] is True
    assert word["additionalProperties"] is True

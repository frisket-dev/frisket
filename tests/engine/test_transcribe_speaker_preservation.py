"""Speaker labels survive every consumer that reconstructs transcript segments."""

from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store import Project


def test_grounding_segment_token_and_anchor_carry_speaker():
    from frisket.engine.store.grounding_contract import AnchorUnit, SegmentToken

    token = SegmentToken(text="hi", start_ms=0, end_ms=1000, index=0, speaker="S1")
    assert token.speaker == "S1"
    unit = AnchorUnit(unit_id=0, spans=[], text="hi", speaker="S1")
    assert unit.speaker == "S1"
    assert SegmentToken(text="x", start_ms=None, end_ms=None, index=1).speaker is None


def test_evidence_span_selector_carries_speaker():
    from frisket.engine.executor.transcription_evidence import _temporal_spans

    spans = list(
        _temporal_spans(
            [
                {"start": 0, "end": 1, "text": "hi", "speaker": "S2"},
                {"start": 1, "end": 2, "text": "bye"},
            ],
            duration_ms=2000,
        )
    )
    assert spans[0]["selector"]["speaker"] == "S2"
    assert "speaker" not in spans[1]["selector"]


@pytest.fixture
def project(tmp_path: Path):
    value = Project.create(tmp_path / "p.frisket")
    yield value
    value.close()


def _speaker_artifact(project: Project) -> tuple[int, int, int]:
    from frisket.engine.store.evidence import record_source_artifact, record_source_span

    sheet = project.add_sheet("data")
    (row_id,) = project.add_rows(sheet, [{}], {})
    artifact = record_source_artifact(
        project,
        artifact_kind="av",
        media_type="audio/wav",
        blob_hash="deadbeef",
        filename="a.wav",
        duration_ms=5000,
        source_sheet_id=sheet,
        source_row_id=row_id,
        source_column_id=None,
        metadata={},
    )
    record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=0,
        end_ms=1000,
        quote="hi",
        selector={"segment_index": 0, "speaker": "S1"},
        metadata={},
    )
    return sheet, row_id, artifact["id"]


def test_segment_stream_reads_speaker_back(project: Project):
    from frisket.engine.store.evidence import record_source_span
    from frisket.engine.store.transcript_segment_stream import _segments_for_artifact

    _sheet, _row_id, artifact_id = _speaker_artifact(project)
    record_source_span(
        project,
        artifact_id=artifact_id,
        span_kind="temporal",
        start_ms=1000,
        end_ms=2000,
        quote="yo",
        selector={"segment_index": 1},
        metadata={},
    )
    units, tokens, _ = _segments_for_artifact(project, artifact_id)
    assert [token.speaker for token in tokens] == ["S1", None]
    assert [unit.speaker for unit in units] == ["S1", None]


def test_numbered_segments_text_carries_speaker_prefix():
    from frisket.ops.extraction import _numbered_segments_text

    text = _numbered_segments_text(
        [
            {
                "segment_index": 0,
                "start": 1.0,
                "end": 2.0,
                "text": "hi",
                "speaker": "S1",
            },
            {"segment_index": 1, "start": 2.0, "end": 3.0, "text": "yo"},
        ]
    )
    lines = text.splitlines()
    assert "[S1]" in lines[0] and "hi" in lines[0]
    assert "[S" not in lines[1]


def test_inject_grounding_segments_preserves_speaker(project: Project):
    from frisket.engine.runner.grounding import inject_grounding_segments

    sheet, row_id, _artifact_id = _speaker_artifact(project)
    values: dict = {"transcript": "hi"}
    spec = {
        "action_kind": "map.extract",
        "sheet_id": sheet,
        "grounding": {"enabled": True},
    }
    inject_grounding_segments(project, spec, row_id, values)
    injected = values.get("transcript_segments")
    assert injected and injected[0]["speaker"] == "S1"

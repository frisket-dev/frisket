from __future__ import annotations

import copy
import json
from contextlib import closing

import pytest
from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.media_types import AudioColumn, TranscriptText
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionParams, ActionRequest, Row, RowResult, SheetRows
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import (
    _TypedMapRowsProgram,
    run_typed_map_rows_action,
)
from frisket.engine.executor.temporal_transcripts import resolve_timestamped_transcript
from frisket.engine.executor.transcription_evidence import write_transcription_evidence
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


class Params(ActionParams):
    source: AudioColumn


class TextOnly(BaseModel):
    speech: TranscriptText


class Custom(TextOnly):
    rewrite: TranscriptText
    unrelated: str
    segments: list[dict]


def text_only(params: Params, row: Row) -> RowResult[TextOnly]:
    return RowResult(output=TextOnly(speech=TranscriptText("Hello world")))


def custom(params: Params, row: Row) -> RowResult[Custom]:
    return RowResult(
        output=Custom(
            speech=TranscriptText("Hello world"),
            rewrite=TranscriptText("Hello world!"),
            unrelated="Hello world",
            segments=[{"start": 700, "end": 900, "text": "invented"}],
        )
    )


REGISTRY = ActionRegistry(
    [
        ActionNamespace(
            "custom",
            actions=[
                action(
                    name="text_only",
                    title="Text only",
                    description="Keep only transcript text.",
                    category=ActionCategory.EXTRACT,
                    run=map_rows(text_only),
                ),
                action(
                    name="custom",
                    title="Custom",
                    description="Transform sibling output fields.",
                    category=ActionCategory.EXTRACT,
                    run=map_rows(custom),
                ),
            ],
        )
    ]
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    with closing(Project.create(tmp_path / "p.frisket")) as project:
        sheet = project.add_sheet("recordings")
        source_id = project.add_column(sheet, "recording", type="audio")
        digest = project.add_blob(
            b"audio fixture",
            filename="recording.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(probe={"duration_seconds": 99}),
        )
        source_value = media_cell(digest, mime="audio/wav", filename="recording.wav")
        [row_id] = project.add_rows(
            sheet, [{"recording": source_value}], {"recording": source_id}
        )
        read = {
            "kind": "transcribe_read",
            "engine": "faster_whisper",
            "text": "Hello world",
            "language": "en",
            "source": {
                "sheet_id": sheet,
                "row_id": row_id,
                "column_id": source_id,
                "column_type": "audio",
                "source_column": "recording",
                "value": source_value,
                "duration_ms": 2000,
            },
            "segments": [
                {
                    "start": 0.1004,
                    "end": 3,
                    "text": "Hello world",
                    "speaker": "S1",
                    "words": [
                        {"word": "Hello", "start": -1, "end": 1},
                        {"word": "world", "start": 1, "end": 3},
                    ],
                }
            ],
        }
        calls = []
        original = _TypedMapRowsProgram.__init__

        def install_writer(self, *args, **kwargs):
            original(self, *args, **kwargs)

            def write(
                current_project, spec, *, batch, run_id, output_columns, **writer_kwargs
            ):
                calls.append(run_id)
                batch[0]["row_file_calls"] = [copy.deepcopy(read)]
                writer = current_project.db.execute(
                    "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
                ).fetchone()[0]
                columns = {
                    output_columns[self._output_names[field.key]]
                    for field in self._resolved_output_fields
                    if field.annotation is TranscriptText
                }
                write_transcription_evidence(
                    current_project,
                    spec,
                    batch=batch,
                    run_id=run_id,
                    output_columns=output_columns,
                    transcript_columns=columns,
                    writer_attempt_id=read.get("writer_override", writer),
                    **writer_kwargs,
                )

            self.write_result_evidence = write

        monkeypatch.setattr(_TypedMapRowsProgram, "__init__", install_writer)

        def run(kind="text_only"):
            request = ActionRequest(
                action_id=f"custom.{kind}",
                scope=SheetRows(sheet_id=sheet),
                params={"source": "recording"},
                output_names={"speech": "renamed"},
                idempotency_key=f"speech-{kind}",
            )
            return run_typed_map_rows_action(
                project,
                "p",
                BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request),
                None,
                lambda current_project, router: MapRunner(
                    current_project,
                    router or ModelRouter(cache=None, cache_mode="off"),
                    authority=UnroutedOnlyAuthority(current_project),
                ),
            )

        yield project, sheet, row_id, read, calls, run


def test_text_only_renamed_output_has_exact_current_temporal_evidence(setup):
    project, sheet, row_id, read, calls, run = setup
    result = run()
    assert result.status == "completed", result.errors
    [link] = project.db.execute("SELECT * FROM evidence_links").fetchall()
    [span] = project.db.execute("SELECT * FROM source_spans").fetchall()
    assert (span["start_ms"], span["end_ms"]) == (100, 2000)
    assert json.loads(span["metadata"])["raw"]["raw_end_ms"] == 3000
    assert json.loads(span["selector_json"]) == {
        "segment_index": 0,
        "speaker": "S1",
        "words": [
            {"word": "Hello", "start_ms": 100, "end_ms": 1000},
            {"word": "world", "start_ms": 1000, "end_ms": 2000},
        ],
    }
    resolved = resolve_timestamped_transcript(
        project, sheet_id=sheet, row_id=row_id, column_id=link["column_id"]
    )
    assert resolved is not None
    assert resolved.transcript_column_name == "renamed"
    assert (
        resolved.artifact_duration_ms == 2000
    )  # recorded clock, not fresh metadata's 99s
    assert resolved.language == "en"
    assert run().receipt_id == result.receipt_id
    assert len(calls) == 1
    assert project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 1


def test_custom_rewrite_and_equal_incidental_string_do_not_acquire_grounding(setup):
    project, sheet, row_id, read, calls, run = setup
    result = run("custom")
    assert result.status == "completed", result.errors
    links = project.db.execute(
        "SELECT c.name FROM evidence_links el JOIN columns c ON c.id=el.column_id"
    ).fetchall()
    assert [link["name"] for link in links] == ["renamed"]
    [span] = project.db.execute("SELECT * FROM source_spans").fetchall()
    assert span["quote"] == "Hello world"
    assert span["end_ms"] == 2000  # ignores the rewritten segments sibling
    rewrite = project.db.execute(
        "SELECT id FROM columns WHERE name='rewrite'"
    ).fetchone()[0]
    assert (
        resolve_timestamped_transcript(
            project, sheet_id=sheet, row_id=row_id, column_id=rewrite
        )
        is None
    )


@pytest.mark.parametrize("segments", [[], [{"start": 2, "end": 1, "text": "bad"}]])
def test_silence_and_invalid_segments_create_no_orphan_artifacts(setup, segments):
    project, sheet, row_id, read, calls, run = setup
    read["segments"] = segments
    assert run().status == "completed"
    for table in ("source_artifacts", "source_spans", "evidence_links"):
        assert project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_stale_writer_cannot_publish_temporal_evidence(setup):
    project, sheet, row_id, read, calls, run = setup
    read["writer_override"] = "nonexistent-attempt"
    assert run().status == "failed"
    for table in ("source_artifacts", "source_spans", "evidence_links"):
        assert project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_unknown_recorded_duration_is_not_inferred_from_fresh_metadata(setup):
    project, sheet, row_id, read, calls, run = setup
    read["source"]["duration_ms"] = None
    read["segments"][0]["words"] = 7  # malformed optional capture is ignored
    assert run().status == "completed"
    [span] = project.db.execute("SELECT * FROM source_spans").fetchall()
    [artifact] = project.db.execute("SELECT * FROM source_artifacts").fetchall()
    assert span["end_ms"] == 3000
    assert "words" not in json.loads(span["selector_json"])
    assert artifact["duration_ms"] is None
    column_id = project.db.execute("SELECT column_id FROM evidence_links").fetchone()[0]
    assert (
        resolve_timestamped_transcript(
            project, sheet_id=sheet, row_id=row_id, column_id=column_id
        )
        is None
    )

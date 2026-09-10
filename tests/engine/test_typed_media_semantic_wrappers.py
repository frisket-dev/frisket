"""Semantic wrapper grounding through the real row host and admitted readers."""

import json
from contextlib import closing
from io import BytesIO

import pytest
from PIL import Image
from pydantic import create_model

from frisket.actions.core import ActionCategory, RegisteredAction, action, map_rows
from frisket.actions.media import (
    OcrParams,
    TranscribeParams,
    ocr_options,
    transcribe_options,
)
from frisket.actions.media_types import OcrReader, OcrText, Transcriber, TranscriptText
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import Outcome, Row, RowResult
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.temporal_transcripts import resolve_timestamped_transcript
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import owned_media_metadata_document
from frisket.engine.store.ocr_word_stream import resolve_current_ocr_evidence
from frisket.ops.ocr_engines import OcrEngines
from frisket.sdk.ops import transcribe_engines


TEXT = "Hello world"


@pytest.mark.parametrize("kind", ["ocr", "transcribe"])
@pytest.mark.parametrize("wrapper", ["bare", "outcome", "nullable", "outcome_nullable"])
def test_only_original_semantic_output_gets_grounding(
    tmp_path, monkeypatch, kind, wrapper
):
    semantic = OcrText if kind == "ocr" else TranscriptText
    annotation = semantic | None if "nullable" in wrapper else semantic
    if wrapper.startswith("outcome"):
        annotation = Outcome[annotation]
    output_type = create_model(
        "SemanticOutput",
        original=(annotation, ...),
        rewritten=(annotation, ...),
        incidental=(str, ...),
    )

    def output(text):
        rewritten = semantic(TEXT + "!")
        if wrapper.startswith("outcome"):
            text, rewritten = Outcome.ok(text), Outcome.ok(rewritten)
        return RowResult(
            output=output_type(original=text, rewritten=rewritten, incidental=TEXT)
        )

    async def recognize(params: OcrParams, row: Row, reader: OcrReader):
        result = await reader.recognize(row, params.source, options=ocr_options(params))
        return output(result.text)

    async def transcribe(params: TranscribeParams, row: Row, reader: Transcriber):
        result = await reader.transcribe(
            row, params.source, options=transcribe_options(params)
        )
        return output(result.text)

    run = recognize if kind == "ocr" else transcribe
    run.__annotations__["return"] = RowResult[output_type]
    definition = action(
        name="semantic_read",
        title="Semantic read",
        description="Publish original semantic text beside rewritten and plain text.",
        category=ActionCategory.EXTRACT,
        run=map_rows(
            run, engine_options=ocr_options if kind == "ocr" else transcribe_options
        ),
    )
    action_id = "custom.semantic_read"
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {
            **ACTION_REGISTRY._actions,
            action_id: RegisteredAction(action_id, definition),
        },
    )
    dispatched = []

    async def ocr_engine(_self, engine, pages, _ctx, **kwargs):
        dispatched.append(engine)
        assert len(pages) == 1
        return [
            {
                "text": TEXT,
                "blocks": [{"text": TEXT, "bbox": [[1, 2], [10, 2], [10, 6], [1, 6]]}],
            }
        ]

    async def transcribe_engine(engine, path, spec, ctx, **kwargs):
        dispatched.append(engine)
        return transcribe_engines.TranscriptionEngineResult(
            output={
                "text": TEXT,
                "language": "en",
                "cost": 0,
                "segments": [{"text": TEXT, "start": 0.1, "end": 0.9}],
            },
            model_calls=(),
        )

    monkeypatch.setattr(OcrEngines, "run_engine_on_pages", ocr_engine)
    monkeypatch.setattr(
        transcribe_engines, "run_transcription_engine", transcribe_engine
    )
    image = BytesIO()
    Image.new("RGB", (12, 8), "white").save(image, format="PNG")
    engine = "tesseract" if kind == "ocr" else "faster_whisper"
    column_type = "image" if kind == "ocr" else "audio"
    mime = "image/png" if kind == "ocr" else "audio/wav"
    filename = "page.png" if kind == "ocr" else "recording.wav"

    with closing(Project.create(tmp_path / "semantic.frisket")) as project:
        sheet = project.add_sheet("Media")
        source = project.add_column(sheet, "source", type=column_type)
        digest = project.add_blob(
            image.getvalue() if kind == "ocr" else b"audio fixture",
            filename=filename,
            mime=mime,
            metadata=owned_media_metadata_document(probe={"duration_seconds": 1}),
        )
        [row_id] = project.add_rows(
            sheet,
            [{"source": {"blob": digest, "mime": mime, "filename": filename}}],
            {"source": source},
        )
        result = run_action_spec(
            project,
            {
                "action_id": action_id,
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "source", "engine": engine},
                "output_names": {
                    "original": "renamed_original",
                    "rewritten": "edited",
                    "incidental": "same_plain_string",
                },
                "idempotency_key": "semantic-wrapper",
            },
            project_id="semantic",
        )
        assert result.status == "completed", result.errors
        assert dispatched == [engine]
        columns = {item["name"]: item["id"] for item in project.columns(sheet)}
        assert project.get_values(sheet, columns["renamed_original"])[row_id] == TEXT
        assert project.get_values(sheet, columns["same_plain_string"])[row_id] == TEXT
        assert project.get_values(sheet, columns["edited"])[row_id] == TEXT + "!"
        links = project.db.execute("SELECT column_id FROM evidence_links").fetchall()
        assert {link["column_id"] for link in links} == {columns["renamed_original"]}
        assert links
        spans = project.db.execute("SELECT * FROM source_spans").fetchall()
        if kind == "ocr":
            [region] = [span for span in spans if span["span_kind"] == "region"]
            assert region["quote"] == TEXT
            assert json.loads(region["selector_json"])["polygon"] == [
                [1, 2],
                [10, 2],
                [10, 6],
                [1, 6],
            ]
        else:
            [span] = spans
            assert span["quote"] == TEXT
            assert (span["start_ms"], span["end_ms"]) == (100, 900)
        for name in ("renamed_original", "edited", "same_plain_string"):
            column_id = columns[name]
            if kind == "ocr":
                grounding = resolve_current_ocr_evidence(
                    project, sheet_id=sheet, row_id=row_id, column_id=column_id
                )
            else:
                grounding = resolve_timestamped_transcript(
                    project, sheet_id=sheet, row_id=row_id, column_id=column_id
                )
            assert bool(grounding) is (name == "renamed_original")

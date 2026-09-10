from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from typing import Any

from frisket.contracts.action import Receipt
from frisket.engine.executor import ExecutorDeps
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store import Project


PROJECT_ID = "project-media-transcribe-chain"


def _seed_project(project_path: Path) -> tuple[Project, int, list[int], list[str]]:
    project = Project.create(project_path, name="Transcribe Chain")
    sheet_id = project.add_sheet("Episodes")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    blobs = [
        project.add_blob(
            b"RIFF0000WAVEfmt " + label.encode("ascii"),
            filename=f"{label}.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        for label in ("one", "two")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Episode 1",
                "media": media_cell(
                    blobs[0],
                    mime="audio/wav",
                    filename="one.wav",
                ),
            },
            {
                "title": "Episode 2",
                "media": media_cell(
                    blobs[1],
                    mime="audio/wav",
                    filename="two.wav",
                ),
            },
        ],
        columns,
    )
    return project, sheet_id, row_ids, blobs


def _transcribe_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"text": "transcript", "segments": "transcript_segments"},
        "params": {
            "source": "media",
            "engine": "faster_whisper",
        },
        "idempotency_key": "media_transcribe_chain@sha256:transcribe",
    }


def _derive_action(
    *, sheet_id: int, column_id: int, run_id: int, item_schema: dict
) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Transcript Segments",
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": sheet_id,
                "column_id": column_id,
                "run_id": run_id,
                "route": "transcript_segments",
                "schema": "transcript_segments",
            },
            "item_schema": item_schema,
            "columns": [
                {"name": "segment_index", "path": "$.segment_index", "type": "integer"},
                {"name": "start", "path": "$.start", "type": "number"},
                {"name": "end", "path": "$.end", "type": "number"},
                {"name": "text", "path": "$.text", "type": "text"},
            ],
        },
        "idempotency_key": "media_transcribe_chain@sha256:derive",
    }


def _segment_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "segment_index": {"type": "integer"},
            "start": {"type": "number"},
            "end": {"type": "number"},
            "text": {"type": "string"},
            # optional per-turn speaker label: additive, emitted
            # by diarizing runs; a materialized child sheet gains the column.
            "speaker": {
                "type": "string",
                "description": (
                    "optional anonymous speaker turn label; present only on "
                    "diarized runs"
                ),
            },
            # optional "approximate" marker on a majority-assigned turn
            # additive alongside speaker.
            "speaker_confidence": {
                "type": "string",
                "description": (
                    "optional 'approximate' marker on a majority-assigned speaker turn"
                ),
            },
            # word-level timestamps (transcript-segment-citation-scope-v1
            # piece (a)): additive, emitted by word-capable engines.
            "words": {
                "type": "array",
                "description": (
                    "optional per-word timestamps -- present for faster_whisper "
                    "(always) and parakeet (vad=false only); absent otherwise"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "word": {"type": "string"},
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                    },
                },
            },
        },
    }


def _receipt(project: Project, receipt_id: str) -> Receipt:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "columns", "rows", "runs", "results", "ops", "receipts")
    }


def test_media_transcribe_segments_feed_derive_table_from_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project, sheet_id, source_row_ids, blobs = _seed_project(
        tmp_path / "media-transcribe-chain.frisket"
    )

    async def fake_faster_whisper(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, spec
        label = "one" if Path(path).name == blobs[0] else "two"
        return {
            "text": f"episode {label} transcript",
            "segments": [
                {"start": 0.0, "end": 0.5, "text": f"episode {label}"},
                {"start": 0.5, "end": 1.0, "text": "closing"},
            ],
            "language": "en",
            "duration": 1.0,
        }

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_faster_whisper
    )
    try:
        transcribed = run_action_spec(
            project,
            _transcribe_action(sheet_id),
            project_id=PROJECT_ID,
            deps=ExecutorDeps(),
        )
        assert transcribed.status == "completed", transcribed.errors
        assert transcribed.run_id is not None
        assert transcribed.receipt_id is not None
        assert ("column", "transcript") in [
            (output.kind, output.name) for output in transcribed.outputs
        ]
        named_output = next(
            output
            for output in transcribed.outputs
            if output.kind == "named_result" and output.name == "transcript_segments"
        )
        assert named_output.ref["source_action_kind"] == "media.transcribe"
        assert named_output.ref["row_ids"] == source_row_ids
        actual_fields = named_output.ref["item_schema"]["properties"]
        for name, expected in _segment_item_schema()["properties"].items():
            assert actual_fields[name]["type"] == expected["type"]
        column_id = int(named_output.column_id or named_output.ref["column_id"])

        derived = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=column_id,
                run_id=transcribed.run_id,
                item_schema=named_output.ref["item_schema"],
            ),
            project_id=PROJECT_ID,
        )
        assert derived.status == "completed", derived.errors
        child_sheet_id = next(
            output.sheet_id for output in derived.outputs if output.kind == "sheet"
        )
        assert child_sheet_id is not None
        child_rows = project.db.execute(
            "SELECT * FROM rows WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [int(row["parent_row_id"]) for row in child_rows] == [
            source_row_ids[0],
            source_row_ids[0],
            source_row_ids[1],
            source_row_ids[1],
        ]
        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
                (child_sheet_id,),
            ).fetchall()
        }
        assert list(columns) == ["segment_index", "start", "end", "text"]
        assert project.get_values(
            child_sheet_id, int(columns["segment_index"]["id"])
        ) == {
            int(child_rows[0]["id"]): 0,
            int(child_rows[1]["id"]): 1,
            int(child_rows[2]["id"]): 0,
            int(child_rows[3]["id"]): 1,
        }
        texts = project.get_values(child_sheet_id, int(columns["text"]["id"]))
        assert list(texts.values()) == [
            "episode one",
            "closing",
            "episode two",
            "closing",
        ]

        receipt = _receipt(project, derived.receipt_id or "")
        source_ref = next(
            ref.ref for ref in receipt.inputs if ref.ref["kind"] == "list_table_read"
        )
        assert (
            _receipt(project, source_ref["source_receipt_id"]).action_kind
            == "media.transcribe"
        )
        assert source_ref["source_receipt_id"] == transcribed.receipt_id
        before_replay = _counts(project)
        replay = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=column_id,
                run_id=transcribed.run_id,
                item_schema=named_output.ref["item_schema"],
            ),
            project_id=PROJECT_ID,
        )
        assert replay.status == "completed"
        assert replay.receipt_id == derived.receipt_id
        assert _counts(project) == before_replay
    finally:
        project.close()

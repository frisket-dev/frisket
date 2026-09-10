from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer
from runner_test_helpers import run_action_with_exact_confirmation
from tests.engine.extract_typed_chain_helpers import typed_extract_request


PROJECT_ID = "project-extract-scalar-temporal-anchors"


# --------------------------------------------------------------------------- #
# Shared stub router (a single map.extract call returning a canned reply)
# --------------------------------------------------------------------------- #


class _StubAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=json.loads(json.dumps(self.reply)),
            tokens_in=91,
            tokens_out=37,
            cost=0.006,
            model=req.model,
        )


def _stub_router(reply: dict[str, Any]) -> tuple[ModelRouter, _StubAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _StubAdapter(reply)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _verdict_field() -> dict[str, Any]:
    return {
        "name": "verdict",
        "type": "text",
        "description": "The single strongest claim questioning election integrity.",
    }


def _scalar_column_id(project: Project, sheet_id: int) -> int:
    return int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='verdict'",
            (sheet_id,),
        ).fetchone()["id"]
    )


def _verdict_viewer(project: Project, *, sheet_id: int, row_id: int) -> dict[str, Any]:
    column_id = _scalar_column_id(project, sheet_id)
    evidence = list_cell_evidence(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        project_id=PROJECT_ID,
    )
    assert len(evidence["links"]) == 1, evidence["links"]
    return resolve_evidence_viewer(
        project, evidence["links"][0]["stable_id"], project_id=PROJECT_ID
    )


def _verdict_link_spans(
    project: Project, *, sheet_id: int, row_id: int
) -> list[dict[str, Any]]:
    viewer = _verdict_viewer(project, sheet_id=sheet_id, row_id=row_id)
    return viewer["artifacts"][0]["spans"]


def _assert_player_renderable(artifact: dict[str, Any]) -> None:
    """The EvidenceViewer.tsx contract (web/src/components/EvidenceViewer.tsx:433):
    a player renders only when ``blob && media_type.startsWith('audio/'|'video/')``.
    A temporal span whose artifact fails this -- e.g. the row-json artifact
    (``application/vnd.frisket.row+json``, no blob) -- hits the 'no first-party
    renderer' fallback despite carrying start_ms/end_ms (the regression this
    test guards against: THE GAP was a green span-shape assertion that never
    checked the artifact the span actually landed on)."""

    media_type = artifact["media_type"]
    assert media_type.startswith("audio/") or media_type.startswith("video/"), artifact
    assert artifact["artifact_ref"]["blob"] is not None, artifact


# --------------------------------------------------------------------------- #
# Case 1: scalar grounded on a TRANSCRIPT row -> temporal spans
# --------------------------------------------------------------------------- #


_TRANSCRIPT_SEGMENTS = [
    {"start": 0.0, "end": 2.0, "text": "welcome to the show"},
    {"start": 2.0, "end": 4.0, "text": "today we discuss the election"},
    {"start": 4.0, "end": 6.0, "text": "some say the vote was rigged"},
    {"start": 6.0, "end": 8.0, "text": "others question the integrity of the count"},
    {"start": 8.0, "end": 10.0, "text": "officials deny any wrongdoing"},
    {"start": 10.0, "end": 12.0, "text": "thanks for listening"},
]


def _seed_transcript_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(
        tmp_path / "scalar-transcript.frisket", name="Scalar Transcript"
    )
    sheet_id = project.add_sheet("Episodes")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    blob = project.add_blob(
        b"RIFF0000WAVEfmt election-integrity",
        filename="episode.wav",
        mime="audio/wav",
        source_url="https://cdn.example/episode.wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 12.0, "kind": "audio"}
        ),
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Episode 1",
                "media": media_cell(blob, mime="audio/wav", filename="episode.wav"),
            }
        ],
        cols,
    )
    return {"project": project, "sheet_id": sheet_id, "row_ids": row_ids, "blob": blob}


def _fake_faster_whisper(blob_hash: str):
    async def fake(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, spec
        assert Path(path).name == blob_hash
        return {
            "text": " ".join(seg["text"] for seg in _TRANSCRIPT_SEGMENTS),
            "segments": _TRANSCRIPT_SEGMENTS,
            "language": "en",
            "duration": _TRANSCRIPT_SEGMENTS[-1]["end"],
        }

    return fake


def _run_transcribe(seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    project: Project = seeded["project"]
    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter,
        "transcribe",
        _fake_faster_whisper(seeded["blob"]),
    )
    result = run_action_with_exact_confirmation(
        project,
        {
            "action_id": "media.transcribe",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": seeded["sheet_id"],
                "row_ids": seeded["row_ids"],
            },
            "output_names": {"text": "transcript", "segments": "transcript_segments"},
            "params": {
                "source": "media",
                "engine": "faster_whisper",
            },
            "idempotency_key": "media_transcribe@sha256:scalar-temporal",
        },
        project_id=PROJECT_ID,
    )
    assert result.status == "completed", result.errors


def _transcript_scalar_extract_action(sheet_id: int) -> dict[str, Any]:
    return typed_extract_request(
        sheet_id,
        source=["transcript"],
        instruction="State the strongest claim questioning election integrity.",
        fields=[_verdict_field()],
        grounding={
            "enabled": True,
            "allowed_methods": ["exact_quote"],
        },
        source_document_columns=["transcript"],
        evidence_policy={"citation_required": False},
        idempotency_key="map_extract@sha256:scalar-transcript",
    )


def test_scalar_on_transcript_row_with_segment_index_gets_temporal_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strategy B (segment_indices): a scalar citation resolves BY LOOKUP to the
    segment's temporal span -- the seekable-player shape, not the row-json text
    anchor."""

    seeded = _seed_transcript_project(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_transcribe(seeded, monkeypatch)
        row_id = seeded["row_ids"][0]
        reply = {
            "verdict": {
                "value": "some say the vote was rigged",
                "evidence": [{"segment_indices": [2]}],
                "warnings": [],
            }
        }
        router, adapter = _stub_router(reply)
        result = run_action_with_exact_confirmation(
            project,
            _transcript_scalar_extract_action(seeded["sheet_id"]),
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        assert len(adapter.requests) == 1

        viewer = _verdict_viewer(project, sheet_id=seeded["sheet_id"], row_id=row_id)
        artifact = viewer["artifacts"][0]
        spans = artifact["spans"]
        assert len(spans) == 1
        span = spans[0]
        assert span["span_kind"] == "temporal"
        assert span["selector"]["start_ms"] == 4000
        assert span["selector"]["end_ms"] == 6000
        assert span["quote"] == "some say the vote was rigged"
        # The gap this test now pins down: the span must land on the transcribe
        # writer's `av` artifact (real audio/video mime, blob-backed) -- the
        # EvidenceViewer.tsx contract for rendering the seekable player, NOT the
        # row-json artifact `_source_artifact` falls back to.
        _assert_player_renderable(artifact)
    finally:
        project.close()


def test_scalar_on_transcript_row_with_quote_falls_back_to_temporal_align(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strategy A (quote) fallback: a scalar citation that returned a free-text
    quote instead of a segment index still aligns to the transcript segment
    stream and emits a temporal span (aligned_temporal)."""

    seeded = _seed_transcript_project(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_transcribe(seeded, monkeypatch)
        row_id = seeded["row_ids"][0]
        reply = {
            "verdict": {
                "value": "the count's integrity was questioned",
                "evidence": [
                    {
                        "quote": "others question the integrity of the count",
                        "grounding_method": "quote",
                    }
                ],
                "warnings": [],
            }
        }
        router, _adapter = _stub_router(reply)
        result = run_action_with_exact_confirmation(
            project,
            _transcript_scalar_extract_action(seeded["sheet_id"]),
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors

        viewer = _verdict_viewer(project, sheet_id=seeded["sheet_id"], row_id=row_id)
        artifact = viewer["artifacts"][0]
        spans = artifact["spans"]
        assert len(spans) == 1
        span = spans[0]
        assert span["span_kind"] == "temporal"
        assert span["selector"]["start_ms"] == 6000
        assert span["selector"]["end_ms"] == 8000
        _assert_player_renderable(artifact)
    finally:
        project.close()


# --------------------------------------------------------------------------- #
# Case 2: scalar grounded on a NON-transcript text row -> unchanged text anchor
# --------------------------------------------------------------------------- #


def _seed_text_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(tmp_path / "scalar-text.frisket", name="Scalar Text")
    sheet_id = project.add_sheet("Docs")
    cols = {
        "body": project.add_column(sheet_id, "body", type="text"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [{"body": "The mayor said the budget passed after a long debate."}],
        cols,
    )
    return {"project": project, "sheet_id": sheet_id, "row_ids": row_ids}


def _text_scalar_extract_action(sheet_id: int) -> dict[str, Any]:
    return typed_extract_request(
        sheet_id,
        source=["body"],
        instruction="State what the mayor announced.",
        fields=[_verdict_field()],
        grounding={
            "enabled": True,
            "allowed_methods": ["exact_quote"],
        },
        source_document_columns=["body"],
        evidence_policy={"citation_required": False},
        idempotency_key="map_extract@sha256:scalar-text",
    )


def test_scalar_on_non_transcript_text_row_keeps_text_anchor(
    tmp_path: Path,
) -> None:
    """Degradation Law: no transcript stream and no OCR word stream for a plain
    text row -> the citation keeps today's coarse text anchor (no fabricated
    temporal span)."""

    seeded = _seed_text_project(tmp_path)
    project: Project = seeded["project"]
    try:
        row_id = seeded["row_ids"][0]
        reply = {
            "verdict": {
                "value": "the budget passed",
                "evidence": [
                    {"quote": "the budget passed", "grounding_method": "quote"}
                ],
                "warnings": [],
            }
        }
        router, _adapter = _stub_router(reply)
        result = run_action_with_exact_confirmation(
            project,
            _text_scalar_extract_action(seeded["sheet_id"]),
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors

        spans = _verdict_link_spans(project, sheet_id=seeded["sheet_id"], row_id=row_id)
        assert len(spans) == 1
        span = spans[0]
        assert span["span_kind"] == "text"
        assert span.get("start_ms") is None
        assert span["selector"].get("start_ms") is None
        assert span["quote"] == "the budget passed"
    finally:
        project.close()


# --------------------------------------------------------------------------- #
# citation-span-runs-v1: a multi-segment_indices citation marks EVERY span
# required (root cause of "only span 1 highlights" -- required used to be
# wired to the field's citation_required WITHHOLD policy, so a
# citation_required=False field like this one's _transcript_scalar_extract_
# action wrote required=0 on every span; the viewer's cited-highlight set was
# then empty and the only visible highlight was the always-on-span-0 active
# marker) and groups into contiguous runs (CITATION_RUN_GAP_TOLERANCE_MS).
# --------------------------------------------------------------------------- #


def test_scalar_multi_segment_citation_marks_all_spans_required_and_groups_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_transcript_project(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_transcribe(seeded, monkeypatch)
        row_id = seeded["row_ids"][0]
        # Segments 0-1 (0-4000ms) and 4-5 (8000-12000ms) -- skipping 2-3 opens
        # a 4000ms gap, well over the 1500ms run-gap tolerance, so this cites
        # TWO disjoint runs from ONE evidence item.
        reply = {
            "verdict": {
                "value": "some say the vote was rigged, others question the count",
                "evidence": [{"segment_indices": [0, 1, 4, 5]}],
                "warnings": [],
            }
        }
        router, adapter = _stub_router(reply)
        result = run_action_with_exact_confirmation(
            project,
            _transcript_scalar_extract_action(seeded["sheet_id"]),
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        assert len(adapter.requests) == 1

        viewer = _verdict_viewer(project, sheet_id=seeded["sheet_id"], row_id=row_id)
        artifact = viewer["artifacts"][0]
        spans = artifact["spans"]
        assert len(spans) == 4
        # ALL 4 spans are cited -- the actual writer-side fix.
        assert all(span["required"] is True for span in spans), spans

        by_start = {span["selector"]["start_ms"]: span for span in spans}
        assert by_start[0]["run_index"] == 0
        assert by_start[2000]["run_index"] == 0
        assert by_start[8000]["run_index"] == 1
        assert by_start[10000]["run_index"] == 1

        runs = artifact["runs"]
        assert len(runs) == 2
        assert runs[0]["start_ms"] == 0
        assert runs[0]["end_ms"] == 4000
        assert runs[0]["span_ids"] == [
            by_start[0]["stable_id"],
            by_start[2000]["stable_id"],
        ]
        assert runs[1]["start_ms"] == 8000
        assert runs[1]["end_ms"] == 12000
        # Both runs resolve a clip affordance (the artifact carries a
        # locally-resolvable blob).
        assert runs[0]["clip_url"] and runs[0]["clip_url"] != runs[1]["clip_url"]
        assert (
            f"/evidence/links/{viewer['link']['stable_id']}/runs/0/clip"
            in runs[0]["clip_url"]
        )
        assert (
            f"/evidence/links/{viewer['link']['stable_id']}/runs/1/clip"
            in runs[1]["clip_url"]
        )
    finally:
        project.close()

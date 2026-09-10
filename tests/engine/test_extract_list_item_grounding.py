from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store.media_blobs import media_cell
from frisket.ops.ocr_engines import OcrEngines
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer
from runner_test_helpers import run_action_with_exact_confirmation
from tests.engine.extract_typed_chain_helpers import typed_extract_request

from helpers import stub_rapidocr_run_scope


PROJECT_ID = "project-extract-list-item-grounding"


def _assert_player_renderable(artifact: dict[str, Any]) -> None:
    """The EvidenceViewer.tsx contract (web/src/components/EvidenceViewer.tsx:433):
    a player renders only when ``blob && media_type.startsWith('audio/'|'video/')``.
    List-item temporal spans go through the SAME shared resolution chain as the
    scalar path (``_resolve_evidence_entry_spans`` ->
    ``_transcript_segment_indices_spans``/``_transcript_quote_spans``,
    sdk/ops/extract.py) -- extract-scalar-temporal-anchors-v1's fix (landing on
    the transcribe writer's ``av`` artifact, not the row-json artifact
    ``_source_artifact`` falls back to when the input column is transcript
    TEXT) covers this path too. Pinned here so a future edit to either path
    can't silently regress the other."""

    media_type = artifact["media_type"]
    assert media_type.startswith("audio/") or media_type.startswith("video/"), artifact
    assert artifact["artifact_ref"]["blob"] is not None, artifact


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)
FAKE_PDF = b"%PDF-1.4\n% extract list item grounding fixture\n"


# --------------------------------------------------------------------------- #
# Typed extract prompt/schema shape (unit-level: no LLM call)
# --------------------------------------------------------------------------- #


def _typed_extract_prompt(row_values: dict[str, Any], **params: Any):
    from frisket.actions.extract import ExtractParams, extract
    from frisket.actions.types import Row

    return extract(
        ExtractParams(
            source=list(row_values),
            model="anthropic/claude-haiku-4-5",
            fields=[_moments_field()],
            grounding={"enabled": True},
            **params,
        ),
        Row(row_values),
    )


def test_list_field_evidence_schema_is_index_aligned_list_of_lists() -> None:
    schema = _typed_extract_prompt({"transcript": "hello"}).response_schema
    moments = schema["properties"]["moments"]
    # Values are nullable at the typed boundary; the grounded object rides
    # either alternative.
    grounded = next(
        alt
        for alt in moments.get("anyOf", [moments])
        if isinstance(alt, dict) and "properties" in alt
    )
    evidence_schema = grounded["properties"]["evidence"]
    assert evidence_schema["type"] == "array"
    assert evidence_schema["items"]["type"] == "array"


def test_transcript_segments_column_renders_numbered_units_in_prompt() -> None:
    row_values = {
        "transcript_segments": [
            {"segment_index": 0, "start": 0.0, "end": 1.0, "text": "hello there"},
            {
                "segment_index": 1,
                "start": 1.0,
                "end": 2.0,
                "text": "the vote was rigged",
            },
        ]
    }
    call = _typed_extract_prompt(
        row_values,
        instruction="Find every moment questioning election integrity.",
    )
    user_text = "\n".join(
        part["text"]
        for part in call.messages[1]["content"]
        if part.get("type") == "text"
    )
    assert "[0]" in user_text and "[1]" in user_text
    assert "the vote was rigged" in user_text
    assert "segment_indices" in user_text


# --------------------------------------------------------------------------- #
# Shared stub router
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
            tokens_in=97,
            tokens_out=43,
            cost=0.008,
            model=req.model,
        )


def _stub_router(reply: dict[str, Any]) -> tuple[ModelRouter, _StubAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _StubAdapter(reply)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _moments_field() -> dict[str, Any]:
    return {
        "name": "moments",
        "type": "list",
        "items": {"type": "string"},
        "description": "Every moment questioning election integrity.",
    }


# --------------------------------------------------------------------------- #
# OCR/PDF-sourced list: per-item quote alignment (unchanged path, new per-item
# link write)
# --------------------------------------------------------------------------- #


def _seed_pdf_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(tmp_path / "extract-list-ocr.frisket", name="List OCR")
    sheet_id = project.add_sheet("Docs")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="file"),
    }
    blob = project.add_blob(
        FAKE_PDF,
        filename="doc.pdf",
        mime="application/pdf",
        source_url="https://cdn.example/doc.pdf",
        metadata=owned_media_metadata_document(probe={"pages": 1, "kind": "pdf"}),
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Doc 1",
                "media": media_cell(blob, mime="application/pdf", filename="doc.pdf"),
            }
        ],
        cols,
    )
    return {"project": project, "sheet_id": sheet_id, "row_ids": row_ids, "blob": blob}


def _fake_three_block_engine():
    async def fake_page_images(self, path, media, spec, scratch):
        del self, media, spec, path
        page1 = scratch / "page-1.png"
        Image.new("RGB", (1000, 1000), "white").save(page1)
        return [page1]

    async def fake_rapidocr(self, page_paths, scratch, language):
        del self, scratch, language
        blocks = [
            {
                "text": "alpha event occurred",
                "bbox": [[100, 100], [500, 100], [500, 150], [100, 150]],
                "score": 0.98,
            },
            {
                "text": "beta event happened",
                "bbox": [[100, 200], [500, 200], [500, 250], [100, 250]],
                "score": 0.97,
            },
            {
                "text": "gamma event took place",
                "bbox": [[100, 300], [500, 300], [500, 350], [100, 350]],
                "score": 0.96,
            },
        ]
        return [{"text": " ".join(b["text"] for b in blocks), "blocks": blocks}]

    return fake_page_images, fake_rapidocr


def _ocr_action(*, sheet_id: int, row_ids: list[int]) -> dict[str, Any]:
    return {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
        "params": {
            "source": "media",
            "engine": "rapidocr",
            "dpi": 200,
        },
        "idempotency_key": "media_ocr@sha256:extract-list-item",
    }


def _run_ocr(seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    project: Project = seeded["project"]
    fake_pages, fake_ocr = _fake_three_block_engine()
    monkeypatch.setattr(OcrEngines, "_page_images", fake_pages)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_ocr)
    stub_rapidocr_run_scope(monkeypatch)

    result = run_action_with_exact_confirmation(
        project,
        _ocr_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]),
        project_id=PROJECT_ID,
    )
    assert result.status == "completed", result.errors


def _ocr_list_extract_action(sheet_id: int) -> dict[str, Any]:
    return typed_extract_request(
        sheet_id,
        # The OCR step's text column is the model input; the file column is
        # only the grounding document (model FILE inputs need explicit OCR).
        source=["title", "ocr_text"],
        instruction="List every moment questioning election integrity.",
        fields=[_moments_field()],
        grounding={
            "enabled": True,
            "allowed_methods": ["exact_quote"],
        },
        source_document_columns=["media"],
        evidence_policy={"citation_required": False},
        idempotency_key="map_extract@sha256:list-item-ocr",
    )


def _ocr_list_reply() -> dict[str, Any]:
    return {
        "moments": {
            "value": [
                "alpha event occurred",
                "beta event happened",
                "gamma event took place",
            ],
            "evidence": [
                [{"quote": "alpha event occurred", "grounding_method": "quote"}],
                [{"quote": "beta event happened", "grounding_method": "quote"}],
                [{"quote": "gamma event took place", "grounding_method": "quote"}],
            ],
            "warnings": [],
        }
    }


def test_ocr_sourced_three_item_list_writes_three_item_scoped_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_pdf_project(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_ocr(seeded, monkeypatch)
        row_id = seeded["row_ids"][0]
        router, adapter = _stub_router(_ocr_list_reply())
        result = run_action_with_exact_confirmation(
            project,
            _ocr_list_extract_action(seeded["sheet_id"]),
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        assert len(adapter.requests) == 1

        moments_column_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='moments'",
                (seeded["sheet_id"],),
            ).fetchone()["id"]
        )
        evidence = list_cell_evidence(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=row_id,
            column_id=moments_column_id,
            project_id=PROJECT_ID,
        )
        # RED CHECK: 3-item list -> 3 evidence links, each item-scoped.
        assert len(evidence["links"]) == 3

        quotes_by_link = []
        for link_summary in evidence["links"]:
            viewer = resolve_evidence_viewer(
                project, link_summary["stable_id"], project_id=PROJECT_ID
            )
            spans = viewer["artifacts"][0]["spans"]
            assert spans, "each item's link must carry its own span(s)"
            assert all(span["span_kind"] == "region" for span in spans)
            quotes_by_link.append({span["quote"] for span in spans})

        assert {"alpha event occurred"} in quotes_by_link
        assert {"beta event happened"} in quotes_by_link
        assert {"gamma event took place"} in quotes_by_link
    finally:
        project.close()


# --------------------------------------------------------------------------- #
# Transcript-sourced list: segment_indices DEFAULT (strategy anchor_id/B)
# --------------------------------------------------------------------------- #


def _seed_transcript_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(
        tmp_path / "extract-list-transcript.frisket", name="List Transcript"
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


_TRANSCRIPT_SEGMENTS = [
    {"start": 0.0, "end": 2.0, "text": "welcome to the show"},
    {"start": 2.0, "end": 4.0, "text": "today we discuss the election"},
    {"start": 4.0, "end": 6.0, "text": "some say the vote was rigged"},
    {"start": 6.0, "end": 8.0, "text": "others question the integrity of the count"},
    {"start": 8.0, "end": 10.0, "text": "officials deny any wrongdoing"},
    {"start": 10.0, "end": 12.0, "text": "thanks for listening"},
]


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


def _transcribe_action(*, sheet_id: int, row_ids: list[int]) -> dict[str, Any]:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"text": "transcript", "segments": "transcript_segments"},
        "params": {
            "source": "media",
            "engine": "faster_whisper",
        },
        "idempotency_key": "media_transcribe@sha256:list-item",
    }


def _run_transcribe(seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    project: Project = seeded["project"]
    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter,
        "transcribe",
        _fake_faster_whisper(seeded["blob"]),
    )
    result = run_action_with_exact_confirmation(
        project,
        _transcribe_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]),
        project_id=PROJECT_ID,
    )
    assert result.status == "completed", result.errors


def _transcript_list_extract_action(sheet_id: int) -> dict[str, Any]:
    return typed_extract_request(
        sheet_id,
        source=["transcript"],
        instruction="List every moment questioning election integrity.",
        fields=[_moments_field()],
        grounding={
            "enabled": True,
            "allowed_methods": ["exact_quote"],
        },
        source_document_columns=["transcript"],
        evidence_policy={"citation_required": False},
        idempotency_key="map_extract@sha256:list-item-transcript",
    )


def _transcript_list_reply() -> dict[str, Any]:
    return {
        "moments": {
            "value": [
                "some say the vote was rigged",
                "others question the integrity of the count",
                "officials deny any wrongdoing",
            ],
            "evidence": [
                [{"segment_indices": [2]}],
                [{"segment_indices": [3]}],
                [{"segment_indices": [4]}],
            ],
            "warnings": [],
        }
    }


def test_transcript_sourced_three_item_list_writes_three_segment_scoped_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_transcript_project(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_transcribe(seeded, monkeypatch)
        row_id = seeded["row_ids"][0]
        router, adapter = _stub_router(_transcript_list_reply())
        result = run_action_with_exact_confirmation(
            project,
            _transcript_list_extract_action(seeded["sheet_id"]),
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        assert len(adapter.requests) == 1

        moments_column_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='moments'",
                (seeded["sheet_id"],),
            ).fetchone()["id"]
        )
        evidence = list_cell_evidence(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=row_id,
            column_id=moments_column_id,
            project_id=PROJECT_ID,
        )
        # RED CHECK: 3-item list -> 3 evidence links, each item-scoped.
        assert len(evidence["links"]) == 3

        ranges_by_link = []
        for link_summary in evidence["links"]:
            viewer = resolve_evidence_viewer(
                project, link_summary["stable_id"], project_id=PROJECT_ID
            )
            artifact = viewer["artifacts"][0]
            spans = artifact["spans"]
            assert len(spans) == 1
            span = spans[0]
            assert span["span_kind"] == "temporal"
            # extract-scalar-temporal-anchors-v1's viewer contract, shared by
            # this list path: the span's artifact must be the seekable-player
            # shape (audio/video mime + blob), not the row-json artifact.
            _assert_player_renderable(artifact)
            ranges_by_link.append(
                (
                    span["selector"]["start_ms"],
                    span["selector"]["end_ms"],
                    span["quote"],
                )
            )

        # Segment 2: 4.0s-6.0s "some say the vote was rigged"
        assert (4000, 6000, "some say the vote was rigged") in ranges_by_link
        # Segment 3: 6.0s-8.0s "others question the integrity of the count"
        assert (
            6000,
            8000,
            "others question the integrity of the count",
        ) in ranges_by_link
        # Segment 4: 8.0s-10.0s "officials deny any wrongdoing"
        assert (8000, 10000, "officials deny any wrongdoing") in ranges_by_link
    finally:
        project.close()


def test_transcript_item_with_invalid_segment_index_is_unsupported_not_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degradation Law: an out-of-range segment index resolves to nothing (the
    anchor_id lookup drops it) -- the item gets no link, never a guessed span."""

    seeded = _seed_transcript_project(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_transcribe(seeded, monkeypatch)
        row_id = seeded["row_ids"][0]
        reply = {
            "moments": {
                "value": ["a nonexistent claim"],
                "evidence": [[{"segment_indices": [999]}]],
                "warnings": [],
            }
        }
        router, _adapter = _stub_router(reply)
        result = run_action_with_exact_confirmation(
            project,
            _transcript_list_extract_action(seeded["sheet_id"]),
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        moments_column_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='moments'",
                (seeded["sheet_id"],),
            ).fetchone()["id"]
        )
        evidence = list_cell_evidence(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=row_id,
            column_id=moments_column_id,
            project_id=PROJECT_ID,
        )
        assert evidence["links"] == []
    finally:
        project.close()

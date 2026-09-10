from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from frisket.engine.executor import run_action_spec
from frisket.engine.store.media_blobs import media_cell
from frisket.ops.ocr_engines import OcrEngines
from frisket.engine.executor.extract_evidence import _source_spans
from frisket.engine.store import Project
from frisket.engine.store.evidence import record_source_artifact

from helpers import stub_rapidocr_run_scope


PROJECT_ID = "project-extract-model-bbox-verification"

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)
FAKE_PDF = b"%PDF-1.4\n% model bbox verification fixture\n"


def _seed(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(tmp_path / "model-bbox.frisket", name="ModelBbox")
    sheet_id = project.add_sheet("Docs")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="file"),
    }
    blob = project.add_blob(
        FAKE_PDF,
        filename="doc.pdf",
        mime="application/pdf",
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


def _fake_engine():
    async def fake_page_images(self, path, media, spec, scratch):
        del self, media, spec, path
        page1 = scratch / "page-1.png"
        Image.new("RGB", (1000, 1000), "white").save(page1)
        return [page1]

    async def fake_rapidocr(self, page_paths, scratch, language):
        del self, scratch, language
        # One block near the top of the page (normalized ~0.1,0.1 - 0.9,0.15).
        return [
            {
                "text": "the quick brown fox",
                "blocks": [
                    {
                        "text": "the quick brown fox",
                        "bbox": [[100, 100], [900, 100], [900, 150], [100, 150]],
                        "score": 0.98,
                    }
                ],
            }
        ]

    return fake_page_images, fake_rapidocr


def _run_ocr(seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    project: Project = seeded["project"]
    fake_pages, fake_ocr = _fake_engine()
    monkeypatch.setattr(OcrEngines, "_page_images", fake_pages)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_ocr)
    stub_rapidocr_run_scope(monkeypatch)
    result = run_action_spec(
        project,
        {
            "action_id": "media.ocr",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": seeded["sheet_id"],
                "row_ids": seeded["row_ids"],
            },
            "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
            "params": {
                "source": "media",
                "engine": "rapidocr",
                "dpi": 200,
            },
            "idempotency_key": "media_ocr@sha256:model-bbox",
        },
        project_id=PROJECT_ID,
    )
    assert result.status == "completed", result.errors


def _extract_artifact(seeded: dict[str, Any]) -> dict[str, Any]:
    project: Project = seeded["project"]
    return record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=seeded["blob"],
        filename="doc.pdf",
        source_sheet_id=seeded["sheet_id"],
        source_row_id=seeded["row_ids"][0],
        metadata={},
    )


def test_non_overlapping_model_bbox_downgrades_to_page_range_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_ocr(seeded, monkeypatch)
        artifact = _extract_artifact(seeded)
        # The model guessed a box at the BOTTOM of the page — no OCR block there.
        spans = _source_spans(
            project,
            artifact=artifact,
            item={
                "grounding_method": "model_bbox",
                "page": 1,
                "quote": "the quick brown fox",
                "bbox": {"x0": 0.1, "y0": 0.8, "x1": 0.9, "y1": 0.9},
            },
            rank=0,
        )
        assert len(spans) == 1
        span = spans[0]
        assert span["span_kind"] == "page_range"
        assert "model_bbox_unverified" in span["metadata"]["warnings"]
        assert not span["bbox"]
    finally:
        project.close()


def test_no_word_stream_fails_closed_to_page_range(tmp_path: Path) -> None:
    seeded = _seed(tmp_path)
    project: Project = seeded["project"]
    try:
        artifact = _extract_artifact(seeded)  # no OCR run -> nothing to verify against
        spans = _source_spans(
            project,
            artifact=artifact,
            item={
                "grounding_method": "model_bbox",
                "page": 1,
                "quote": "the quick brown fox",
                "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.15},
            },
            rank=0,
        )
        assert len(spans) == 1
        assert spans[0]["span_kind"] == "page_range"
        assert "model_bbox_unverified" in spans[0]["metadata"]["warnings"]
    finally:
        project.close()


def test_corroborated_model_bbox_renders_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_ocr(seeded, monkeypatch)
        artifact = _extract_artifact(seeded)
        # A model box overlapping the real OCR block, with a quote that aligns.
        spans = _source_spans(
            project,
            artifact=artifact,
            item={
                "grounding_method": "model_bbox",
                "page": 1,
                "quote": "the quick brown",
                "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.15},
            },
            rank=0,
        )
        assert spans
        assert all(s["span_kind"] == "region" for s in spans)
        assert spans[0]["metadata"]["alignment"] == "model_bbox_verified"
        assert spans[0]["bbox"]
    finally:
        project.close()

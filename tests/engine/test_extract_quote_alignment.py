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
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    resolve_evidence_viewer,
)

from helpers import stub_rapidocr_run_scope


PROJECT_ID = "project-extract-quote-alignment"

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)
FAKE_PDF = b"%PDF-1.4\n% extract quote alignment fixture\n"


def _seed_pdf_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(tmp_path / "extract-align.frisket", name="Align")
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
    return {
        "project": project,
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "blob": blob,
    }


def _fake_hyphenated_engine():
    async def fake_page_images(self, path, media, spec, scratch):
        del self, media, spec
        page1 = scratch / "page-1.png"
        Image.new("RGB", (1000, 1000), "white").save(page1)
        del path
        return [page1]

    async def fake_rapidocr(self, page_paths, scratch, language):
        del self, scratch, language
        # Two visual lines on page 1; the first ends with a hyphenated wrap.
        return [
            {
                "text": "the quick brown infor- mation flows onward",
                "blocks": [
                    {
                        "text": "the quick brown infor-",
                        "bbox": [[100, 100], [900, 100], [900, 150], [100, 150]],
                        "score": 0.98,
                    },
                    {
                        "text": "mation flows onward",
                        "bbox": [[100, 200], [900, 200], [900, 250], [100, 250]],
                        "score": 0.97,
                    },
                ],
            }
        ]

    return fake_page_images, fake_rapidocr


def _fake_multi_page_engine(pages: list[list[dict[str, Any]]]):
    """Build a fake (page_images, rapidocr) pair for ``pages`` — one entry per
    page, each a list of block dicts ``{text, bbox(pixel 4-corner), score}``.
    Used to seed repeated phrases (same page / different pages) for the scope
    tests."""

    async def fake_page_images(self, path, media, spec, scratch):
        del self, media, spec, path
        out = []
        for i in range(len(pages)):
            page = scratch / f"page-{i + 1}.png"
            Image.new("RGB", (1000, 1000), "white").save(page)
            out.append(page)
        return out

    async def fake_rapidocr(self, page_paths, scratch, language):
        del self, scratch, language
        return [
            {
                "text": " ".join(b["text"] for b in blocks),
                "blocks": blocks,
            }
            for blocks in pages
        ]

    return fake_page_images, fake_rapidocr


def _run_ocr_pages(
    seeded: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    pages: list[list[dict[str, Any]]],
) -> None:
    project: Project = seeded["project"]
    fake_pages, fake_ocr = _fake_multi_page_engine(pages)
    monkeypatch.setattr(OcrEngines, "_page_images", fake_pages)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_ocr)
    stub_rapidocr_run_scope(monkeypatch)

    result = run_action_spec(
        project,
        _ocr_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]),
        project_id=PROJECT_ID,
    )
    assert result.status == "completed", result.errors


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
        "idempotency_key": "media_ocr@sha256:extract-align",
    }


def _run_ocr(seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    project: Project = seeded["project"]
    fake_pages, fake_ocr = _fake_hyphenated_engine()
    monkeypatch.setattr(OcrEngines, "_page_images", fake_pages)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_ocr)
    stub_rapidocr_run_scope(monkeypatch)

    result = run_action_spec(
        project,
        _ocr_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]),
        project_id=PROJECT_ID,
    )
    assert result.status == "completed", result.errors


def _extract_artifact(seeded: dict[str, Any]) -> dict[str, Any]:
    """Mimic map.extract's source artifact: keyed by the input blob hash, with
    the empty blob metadata an imported PDF carries (no page_images)."""

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


def _link_and_resolve(
    seeded: dict[str, Any], spans: list[dict[str, Any]]
) -> dict[str, Any]:
    project: Project = seeded["project"]
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref={"kind": "run_result", "row_id": seeded["row_ids"][0]},
        spans=[
            {"span_id": span["id"], "rank": i, "span_role": "support"}
            for i, span in enumerate(spans)
        ],
        sheet_id=seeded["sheet_id"],
        row_id=seeded["row_ids"][0],
        link_role="primary_support",
    )
    return resolve_evidence_viewer(project, link["stable_id"], project_id=PROJECT_ID)


def test_hyphenated_two_line_quote_aligns_to_region_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_pdf_project(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_ocr(seeded, monkeypatch)
        artifact = _extract_artifact(seeded)

        # A split-across-lines quote de-hyphenates "infor-"+"mation".
        spans = _source_spans(
            project,
            artifact=artifact,
            item={"quote": "information flows", "grounding_method": "quote"},
            rank=0,
        )
        # The two lines are both covered -> per-line region spans (not one whole
        # page span, not the positionless text span).
        kinds = [s["span_kind"] for s in spans]
        assert kinds and all(k == "region" for k in kinds), kinds
        assert len(spans) >= 1

        viewer = _link_and_resolve(seeded, spans)
        artifact_payload = viewer["artifacts"][0]
        region_spans = [
            s for s in artifact_payload["spans"] if s["span_kind"] == "region"
        ]
        assert len(region_spans) >= 1
        # The borrowed OCR page_images make the region drawable on page 1.
        pages = artifact_payload["pages"]
        page1 = next(p for p in pages if p["page"] == 1)
        assert page1["image"] is not None
        assert len(page1["regions"]) >= 1
        # The region is INSIDE the answer's lines, not the whole page.
        for region in page1["regions"]:
            box = region["bbox"][0]
            assert box["space"] == "page_normalized"
            assert box["y1"] <= 0.3  # both answer lines sit in the top band
    finally:
        project.close()


def test_no_word_stream_degrades_to_text_span_with_reason(
    tmp_path: Path,
) -> None:
    """Degradation Law: a quote on a blob with NO OCR sibling keeps the coarse
    text anchor and records ``alignment=no_word_stream`` (the legible state that
    drives the viewer's degraded copy) — never a guessed box."""

    seeded = _seed_pdf_project(tmp_path)
    project: Project = seeded["project"]
    try:
        artifact = _extract_artifact(seeded)  # no OCR run -> no sibling stream
        spans = _source_spans(
            project,
            artifact=artifact,
            item={"quote": "information flows", "grounding_method": "quote"},
            rank=0,
        )
        assert len(spans) == 1
        assert spans[0]["span_kind"] == "text"
        assert spans[0]["metadata"]["alignment"] == "no_word_stream"
        assert not spans[0]["bbox"]
    finally:
        project.close()


def _block(text: str, box_px: tuple[int, int, int, int], score: float = 0.98):
    x0, y0, x1, y1 = box_px
    return {
        "text": text,
        "bbox": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "score": score,
    }


def test_repeated_quote_without_scope_degrades_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER 2: the same phrase appears twice on the page with nothing to
    disambiguate -> we must NOT scatter region boxes over both occurrences. The
    span degrades to the coarse text anchor + a ``repeated_quote_ambiguous``
    note (Degradation Law), never all candidates."""

    seeded = _seed_pdf_project(tmp_path)
    project: Project = seeded["project"]
    try:
        # Two identical phrases in two columns of one page; no page/context hint.
        _run_ocr_pages(
            seeded,
            monkeypatch,
            pages=[
                [
                    _block("the budget passed", (100, 100, 400, 150)),
                    _block("other left text", (100, 200, 400, 250)),
                    _block("the budget passed", (600, 100, 900, 150)),
                    _block("other right text", (600, 200, 900, 250)),
                ]
            ],
        )
        artifact = _extract_artifact(seeded)
        spans = _source_spans(
            project,
            artifact=artifact,
            item={"quote": "the budget passed", "grounding_method": "quote"},
            rank=0,
        )
        assert len(spans) == 1
        assert spans[0]["span_kind"] == "text"
        assert spans[0]["metadata"]["alignment"] == "repeated_quote_ambiguous"
        assert "repeated_quote_ambiguous" in spans[0]["metadata"]["warnings"]
        assert not spans[0]["bbox"]
    finally:
        project.close()


def test_page_scoped_repeated_quote_picks_the_right_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER 2 scope invariant: the same phrase appears on page 1 AND page 2;
    the item carries ``page=2``, so the stream is filtered to page 2 before
    aligning and the page-2 occurrence wins as ONE region span on page 2 (never
    both pages)."""

    seeded = _seed_pdf_project(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_ocr_pages(
            seeded,
            monkeypatch,
            pages=[
                [_block("the budget passed", (100, 100, 400, 150))],
                [_block("the budget passed", (100, 100, 400, 150))],
            ],
        )
        artifact = _extract_artifact(seeded)
        spans = _source_spans(
            project,
            artifact=artifact,
            item={
                "quote": "the budget passed",
                "grounding_method": "quote",
                "page": 2,
            },
            rank=0,
        )
        assert spans, spans
        assert all(s["span_kind"] == "region" for s in spans), spans
        assert all(s["page_start"] == 2 for s in spans), spans
    finally:
        project.close()


def test_below_threshold_quote_degrades_not_fabricates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_pdf_project(tmp_path)
    project: Project = seeded["project"]
    try:
        _run_ocr(seeded, monkeypatch)
        artifact = _extract_artifact(seeded)
        spans = _source_spans(
            project,
            artifact=artifact,
            item={
                "quote": "zzzz totally absent phrase qqqq",
                "grounding_method": "quote",
            },
            rank=0,
        )
        assert len(spans) == 1
        assert spans[0]["span_kind"] == "text"
        assert spans[0]["metadata"]["alignment"] == "below_threshold"
    finally:
        project.close()

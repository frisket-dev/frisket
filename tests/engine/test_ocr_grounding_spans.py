from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.ocr_word_stream import resolve_current_ocr_evidence
from frisket.ops.ocr_engines import OcrEngines
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer
from frisket.engine.store.grounding import normalize_bbox

from helpers import stub_rapidocr_run_scope


PROJECT_ID = "project-ocr-grounding-spans"


# --------------------------------------------------------------------------- #
# (1) shared normalizer — one shape, explicit space per input genus
# --------------------------------------------------------------------------- #


def test_normalize_bbox_pixel_polygon_to_page_normalized() -> None:
    # OCR blocks emit pixel 4-corner polygons in page-image pixel space.
    polygon = [[120, 240], [600, 240], [600, 300], [120, 300]]
    box = normalize_bbox(polygon, frame="page", width=1200, height=1200)
    assert box is not None
    assert box["space"] == "page_normalized"
    assert box["x0"] == 0.1
    assert box["y0"] == 0.2
    assert box["x1"] == 0.5
    assert box["y1"] == 0.25
    # the raw polygon is preserved for later fidelity.
    assert box["raw"] == polygon


def test_normalize_bbox_pixel_xywh_to_frame_normalized() -> None:
    # extract_faces emits pixel {x,y,w,h} top-left origin in image-pixel space.
    face = {"x": 100, "y": 50, "w": 200, "h": 100}
    box = normalize_bbox(face, frame="frame", width=400, height=200)
    assert box is not None
    # frame family stays frame_normalized — never guessed as a page.
    assert box["space"] == "frame_normalized"
    assert box["x0"] == 0.25
    assert box["y0"] == 0.25
    assert box["x1"] == 0.75
    assert box["y1"] == 0.75
    assert box["raw"] == face


def test_normalize_bbox_already_normalized_passthrough() -> None:
    box = normalize_bbox(
        {"space": "page_normalized", "x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4}
    )
    assert box is not None
    assert box["space"] == "page_normalized"
    assert (box["x0"], box["y0"], box["x1"], box["y1"]) == (0.1, 0.2, 0.3, 0.4)


def test_normalize_bbox_preserves_map_extract_provider_spaces() -> None:
    # map.extract keeps working: pixel-with-page-dims and page_1000 both
    # collapse to page_normalized (the spec-lane correction — no regression).
    pixel = normalize_bbox(
        {
            "space": "pixel",
            "x0": 684,
            "y0": 480,
            "x1": 876,
            "y1": 540,
            "page_width": 1200,
            "page_height": 1200,
        }
    )
    assert pixel is not None
    assert pixel["space"] == "page_normalized"
    assert pixel["x0"] == 0.57
    assert pixel["y0"] == 0.4
    assert pixel["x1"] == 0.73
    assert pixel["y1"] == 0.45
    assert pixel["raw"]["space"] == "pixel"

    thousandths = normalize_bbox(
        {"space": "page_1000", "x0": 120, "y0": 210, "x1": 430, "y1": 260}
    )
    assert thousandths is not None
    assert thousandths["space"] == "page_normalized"
    assert (thousandths["x0"], thousandths["y0"]) == (0.12, 0.21)
    assert (thousandths["x1"], thousandths["y1"]) == (0.43, 0.26)


def test_normalize_bbox_rejects_degenerate_and_unknown() -> None:
    # x0 >= x1 after normalization is not a drawable box.
    assert (
        normalize_bbox([[10, 10], [10, 10]], frame="page", width=100, height=100)
        is None
    )
    # pixel polygon with no page dims cannot be normalized.
    assert normalize_bbox([[10, 10], [50, 40]], frame="page") is None
    assert normalize_bbox(None) is None


# --------------------------------------------------------------------------- #
# OCR executor integration harness
# --------------------------------------------------------------------------- #


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)
FAKE_PDF = b"%PDF-1.4\n% ocr grounding fixture\n"


def _ocr_action(
    *,
    sheet_id: int,
    row_ids: list[int],
    input_columns: list[str],
    idempotency_key: str = "media_ocr@sha256:grounding-spans",
) -> dict[str, Any]:
    return {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
        "params": {
            "source": input_columns[0],
            "engine": "rapidocr",
            "dpi": 200,
        },
        "idempotency_key": idempotency_key,
    }


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "source_artifacts",
            "source_spans",
            "evidence_links",
            "evidence_link_spans",
        )
    }


def _text_column_id(project: Project, sheet_id: int) -> int:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='ocr_text'",
        (sheet_id,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


# --------------------------------------------------------------------------- #
# (2a) image input — page image references the source blob in place
# --------------------------------------------------------------------------- #


def _seed_image_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(tmp_path / "ocr-image.frisket", name="OCR grounding")
    sheet_id = project.add_sheet("Scans")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="image"),
    }
    blob = project.add_blob(
        PNG_1X1,
        filename="scan.png",
        mime="image/png",
        source_url="https://cdn.example/scan.png",
        metadata=owned_media_metadata_document(
            probe={"width": 1, "height": 1, "kind": "image"}
        ),
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Scan 1",
                "media": media_cell(
                    blob,
                    mime="image/png",
                    filename="scan.png",
                ),
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


def _fake_image_engine():
    async def fake_page_images(self, path, media, spec, scratch):
        del self, media, spec, scratch
        return [path]

    async def fake_rapidocr(self, page_paths, scratch, language):
        del self, scratch, language
        return [
            {
                "text": "invoice total 42",
                "blocks": [
                    {
                        "text": "invoice total 42",
                        "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "score": 0.98,
                    }
                ],
            }
        ]

    return fake_page_images, fake_rapidocr


def test_image_row_writes_page_and_region_spans_with_source_page_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_image_project(tmp_path)
    project: Project = seeded["project"]
    fake_pages, fake_ocr = _fake_image_engine()
    monkeypatch.setattr(OcrEngines, "_page_images", fake_pages)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_ocr)
    stub_rapidocr_run_scope(monkeypatch)
    try:
        result = run_action_spec(
            project,
            _ocr_action(
                sheet_id=seeded["sheet_id"],
                row_ids=seeded["row_ids"],
                input_columns=["media"],
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors

        sheet_id = seeded["sheet_id"]
        text_column_id = _text_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=seeded["row_ids"][0],
            column_id=text_column_id,
            project_id=PROJECT_ID,
        )
        assert len(cell_evidence["links"]) == 1
        viewer = resolve_evidence_viewer(
            project, cell_evidence["links"][0]["stable_id"], project_id=PROJECT_ID
        )
        assert len(viewer["artifacts"]) == 1
        artifact = viewer["artifacts"][0]

        # ONE artifact whose metadata carries the persisted page_images map, in
        # the exact shape the viewer's _page_payloads reader expects.
        page_images = artifact["metadata"]["page_images"]
        assert set(page_images) == {"1"}
        # an image input references its own source blob as the page image.
        assert page_images["1"]["blob_hash"] == seeded["blob"]
        assert page_images["1"]["width"] == 1
        assert page_images["1"]["height"] == 1

        kinds = [span["span_kind"] for span in artifact["spans"]]
        assert kinds.count("page_range") == 1
        assert kinds.count("region") == 1

        region = next(s for s in artifact["spans"] if s["span_kind"] == "region")
        assert region["quote"] == "invoice total 42"
        bbox = region["selector"]["bbox"][0]
        assert bbox["space"] == "page_normalized"
        assert region["selector"]["page_start"] == 1

        # the viewer resolves the page + its region overlay off the artifact.
        pages = artifact["pages"]
        assert len(pages) == 1
        assert pages[0]["page"] == 1
        assert pages[0]["image"]["blob_hash"] == seeded["blob"]
        assert pages[0]["image"]["url"].endswith(f"/blobs/{seeded['blob']}")
        assert len(pages[0]["regions"]) == 1
    finally:
        project.close()


@pytest.mark.parametrize(
    ("blocks", "expected_addressed"),
    [
        (
            [
                {"text": "page only one", "bbox": None, "score": 0.92},
                {"text": "page only two", "bbox": None, "score": 0.91},
            ],
            [
                ("page_range", "page only one"),
                ("page_range", "page only two"),
            ],
        ),
        (
            [
                {
                    "text": "positioned first",
                    "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "score": 0.98,
                },
                {"text": "page only middle", "bbox": None, "score": 0.90},
                {
                    "text": "positioned last",
                    "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "score": 0.97,
                },
            ],
            [
                ("region", "positioned first"),
                ("page_range", "page only middle"),
                ("region", "positioned last"),
            ],
        ),
    ],
)
def test_exhaustive_ocr_evidence_scans_each_block_once_in_source_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocks: list[dict[str, Any]],
    expected_addressed: list[tuple[str, str]],
) -> None:
    seeded = _seed_image_project(tmp_path)
    project: Project = seeded["project"]
    fake_pages, _fake_ocr = _fake_image_engine()

    async def fake_ocr(self, page_paths, scratch, language):
        del self, page_paths, scratch, language
        return [{"text": " ".join(block["text"] for block in blocks), "blocks": blocks}]

    monkeypatch.setattr(OcrEngines, "_page_images", fake_pages)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_ocr)
    stub_rapidocr_run_scope(monkeypatch)
    try:
        result = run_action_spec(
            project,
            _ocr_action(
                sheet_id=seeded["sheet_id"],
                row_ids=seeded["row_ids"],
                input_columns=["media"],
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors

        text_column_id = _text_column_id(project, seeded["sheet_id"])
        resolved = resolve_current_ocr_evidence(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_ids"][0],
            column_id=text_column_id,
        )
        assert len(resolved) == 1
        assert [
            (span["span_kind"], span["quote"]) for span in resolved[0].spans
        ] == expected_addressed

        all_spans = project.db.execute(
            "SELECT sp.span_kind, sp.quote, sp.snippet FROM evidence_link_spans els "
            "JOIN source_spans sp ON sp.id=els.span_id "
            "WHERE els.link_id=? ORDER BY els.rank, sp.id",
            (resolved[0].evidence_link_id,),
        ).fetchall()
        assert len(all_spans) == len(expected_addressed) + 1
        assert all_spans[0]["span_kind"] == "page_range"
        assert all_spans[0]["quote"] is None
        assert all_spans[0]["snippet"] == " ".join(block["text"] for block in blocks)
    finally:
        project.close()


# --------------------------------------------------------------------------- #
# (2b) PDF input — rasterized pages persisted as downscaled blobs; multi-page
# with a zero-block page.
# --------------------------------------------------------------------------- #


def _seed_pdf_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(tmp_path / "ocr-pdf.frisket", name="OCR grounding pdf")
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
        metadata=owned_media_metadata_document(probe={"pages": 2, "kind": "pdf"}),
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


def _fake_pdf_engine():
    async def fake_page_images(self, path, media, spec, scratch):
        del self, media, spec
        # Two synthetic page files (never actually OCR'd; the recipe engine is
        # faked too).
        page1 = scratch / "page-1.png"
        page2 = scratch / "page-2.png"
        page1.write_bytes(PNG_1X1)
        page2.write_bytes(PNG_1X1)
        del path
        return [page1, page2]

    async def fake_rapidocr(self, page_paths, scratch, language):
        del self, scratch, language
        return [
            {
                "text": "page one heading",
                "blocks": [
                    {
                        "text": "page one heading",
                        "bbox": [[400, 500], [3600, 500], [3600, 800], [400, 800]],
                        "score": 0.97,
                    }
                ],
            },
            {"text": "", "blocks": []},  # page 2: nothing recognized
        ]

    return fake_page_images, fake_rapidocr


def test_pdf_row_persists_downscaled_page_images_and_page_only_for_empty_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_pdf_project(tmp_path)
    project: Project = seeded["project"]
    fake_pages, fake_ocr = _fake_pdf_engine()
    monkeypatch.setattr(OcrEngines, "_page_images", fake_pages)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_ocr)
    stub_rapidocr_run_scope(monkeypatch)

    # The actual OCR raster is also the evidence source; render exactly once.
    async def full_size_pages(self, path, media, spec, scratch):
        from PIL import Image

        pages = []
        for number in (1, 2):
            page = scratch / f"page-{number}.png"
            Image.new("RGB", (4000, 5000), "white").save(page)
            pages.append(page)
        return pages

    monkeypatch.setattr(OcrEngines, "_page_images", full_size_pages)

    try:
        result = run_action_spec(
            project,
            _ocr_action(
                sheet_id=seeded["sheet_id"],
                row_ids=seeded["row_ids"],
                input_columns=["media"],
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors

        sheet_id = seeded["sheet_id"]
        text_column_id = _text_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=seeded["row_ids"][0],
            column_id=text_column_id,
            project_id=PROJECT_ID,
        )
        viewer = resolve_evidence_viewer(
            project, cell_evidence["links"][0]["stable_id"], project_id=PROJECT_ID
        )
        artifact = viewer["artifacts"][0]

        page_images = artifact["metadata"]["page_images"]
        assert set(page_images) == {"1", "2"}
        # size honesty: a 4000x5000 render is downscaled so its longest edge is
        # capped; the persisted blob is NOT the source PDF blob.
        cap = 2000
        for meta in page_images.values():
            assert max(meta["width"], meta["height"]) <= cap
            assert meta["blob_hash"] != seeded["blob"]
            # the downscaled page image is a real, servable PNG blob.
            with project.materialize_blob(meta["blob_hash"]) as path:
                assert path.exists()
        # honest bookkeeping of the downscale decision.
        assert page_images["1"]["downscaled"] is True
        assert page_images["1"]["source_width"] == 4000
        assert page_images["1"]["source_height"] == 5000

        by_page: dict[int, list[str]] = {1: [], 2: []}
        for span in artifact["spans"]:
            page = span["selector"].get("page_start")
            by_page[int(page)].append(span["span_kind"])
        # page 1: a page_range + a region for the block.
        assert sorted(by_page[1]) == ["page_range", "region"]
        # page 2 recognized nothing: page_range only, no region.
        assert by_page[2] == ["page_range"]

        # the region's normalized bbox is scale-invariant (block was in the
        # ORIGINAL 4000x5000 page pixel space; the writer normalizes against the
        # render dims, not the downscaled blob).
        region = next(
            s
            for s in artifact["spans"]
            if s["span_kind"] == "region" and s["selector"]["page_start"] == 1
        )
        bbox = region["selector"]["bbox"][0]
        assert bbox["space"] == "page_normalized"
        assert bbox["x0"] == 0.1
        assert bbox["x1"] == 0.9
    finally:
        project.close()


def test_ocr_evidence_replay_does_not_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_image_project(tmp_path)
    project: Project = seeded["project"]
    fake_pages, fake_ocr = _fake_image_engine()
    monkeypatch.setattr(OcrEngines, "_page_images", fake_pages)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_ocr)
    stub_rapidocr_run_scope(monkeypatch)
    try:
        action = _ocr_action(
            sheet_id=seeded["sheet_id"],
            row_ids=seeded["row_ids"],
            input_columns=["media"],
        )
        first = run_action_spec(project, action, project_id=PROJECT_ID)
        assert first.status == "completed"
        after_first = _counts(project)

        replay = run_action_spec(project, action, project_id=PROJECT_ID)
        assert replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        assert _counts(project) == after_first
    finally:
        project.close()

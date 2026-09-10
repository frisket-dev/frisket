from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pypdf import PdfWriter

from blob_store_helpers import local_blob_path
from frisket.engine.executor import run_action_spec
from frisket.engine.store.media_blobs import media_cell
from frisket.ops.ocr_engines import OcrEngines
from frisket.ops.searchable_pdf import COMPOSITOR_VERSION
from frisket.engine.store import Project

from helpers import stub_rapidocr_run_scope


DPI = 200

# --------------------------------------------------------------------------
# helpers (mirrors tests/test_ocr_searchable_pdf_mode.py + test_media_ocr_executor.py)
# --------------------------------------------------------------------------


def _source_pdf(n_pages: int = 1, w: float = 612.0, h: float = 792.0) -> bytes:
    writer = PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=w, height=h)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _line_block(text: str, x0: int, y0: int, x1: int, y1: int) -> dict[str, Any]:
    return {
        "text": text,
        "bbox": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "score": 0.99,
    }


def _stub_page(text: str) -> dict[str, Any]:
    return {"text": text, "blocks": [_line_block(text, 100, 100, 700, 170)]}


def _patch_engine(monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, Any]]) -> None:
    async def fake_page_images(self, path, media, spec, scratch):  # noqa: ANN001
        del self, media, spec, scratch
        return [path for _ in pages]

    async def fake_rapidocr(self, page_paths, scratch, language=None):  # noqa: ANN001
        del self, page_paths, scratch, language
        return [dict(p) for p in pages]

    monkeypatch.setattr(OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_rapidocr)
    stub_rapidocr_run_scope(monkeypatch)


def _seed_pdf_project(
    tmp_path: Path, *, n_rows: int = 1, filenames: list[str] | None = None
) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "action.frisket", name="OCR action")
    sheet_id = project.add_sheet("Scans")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="file"),
    }
    names = filenames or [f"scan{i}.pdf" for i in range(n_rows)]
    rows = []
    for name in names:
        digest = project.add_blob(_source_pdf(1), filename=name, mime="application/pdf")
        rows.append(
            {
                "title": name,
                "media": media_cell(digest, mime="application/pdf", filename=name),
            }
        )
    row_ids = project.add_rows(sheet_id, rows, cols)
    return project, sheet_id, row_ids


def _ocr_action(
    sheet_id: int,
    row_ids: list[int],
    *,
    idempotency_key: str = "media_ocr_searchable@sha256:one",
) -> dict[str, Any]:
    return {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {
            "text": "ocr_text",
            "blocks": "ocr_text_blocks",
            "pdf": "ocr_text_pdf",
        },
        "params": {
            "source": "media",
            "engine": "rapidocr",
            "dpi": DPI,
            "searchable_pdf": True,
        },
        "idempotency_key": idempotency_key,
    }


def _receipt(project: Project, receipt_id: str):
    import json

    from frisket.contracts.action import Receipt

    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _refs(project: Project, receipt_id: str) -> tuple[dict[str, Any], ...]:
    receipt = _receipt(project, receipt_id)
    return tuple(
        [item.ref for item in receipt.inputs]
        + [item.ref for item in receipt.outputs]
        + [item.ref for item in receipt.evidence]
    )


def _columns(project: Project, sheet_id: int) -> dict[str, Any]:
    return {
        str(c["name"]): c
        for c in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }


# --------------------------------------------------------------------------
# (a) output ref role
# --------------------------------------------------------------------------


def test_pdf_output_ref_carries_searchable_pdf_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project, sheet_id, row_ids = _seed_pdf_project(tmp_path)
    try:
        result = run_action_spec(
            project,
            _ocr_action(sheet_id, row_ids),
            project_id="proj-ocr-pdf-role",
        )
        assert result.status == "completed"
        refs = _refs(project, result.receipt_id)
        output_refs = [ref for ref in refs if ref["kind"] == "map_result_column"]
        text_ref = next(ref for ref in output_refs if ref["name"] == "ocr_text")
        blocks_ref = next(
            ref for ref in output_refs if ref["name"] == "ocr_text_blocks"
        )
        pdf_ref = next(ref for ref in output_refs if ref["name"] == "ocr_text_pdf")
        assert text_ref["type"] == "text"
        assert blocks_ref["type"] == "json"
        assert pdf_ref["type"] == "file"
        file_ref = next(ref for ref in refs if ref["kind"] == "row_file_output")
        assert file_ref["primary"]["role"] == "searchable_pdf"
        assert file_ref["primary"]["mime"] == "application/pdf"
    finally:
        project.close()


# --------------------------------------------------------------------------
# (b) receipt evidence media_ocr_searchable_pdf
# --------------------------------------------------------------------------


def test_receipt_evidence_records_blob_hash_and_page_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project, sheet_id, row_ids = _seed_pdf_project(tmp_path)
    try:
        result = run_action_spec(
            project,
            _ocr_action(sheet_id, row_ids),
            project_id="proj-ocr-pdf-evidence",
        )
        assert result.status == "completed"
        columns = _columns(project, sheet_id)
        pdf_col = int(columns["ocr_text_pdf"]["id"])
        pdf_cell = project.get_values(sheet_id, pdf_col, row_ids=[row_ids[0]])[
            row_ids[0]
        ]
        refs = _refs(project, result.receipt_id)
        evidence_refs = [ref for ref in refs if ref["kind"] == "row_file_output"]
        assert len(evidence_refs) == 1
        ref = evidence_refs[0]
        assert ref["primary"]["blob_hash"] == pdf_cell["blob"]
        assert ref["row_id"] == row_ids[0]
        assert ref["column_id"] == pdf_col
        metadata = ref["primary"]["metadata"]
        assert metadata["_media_probe_v1"]["pages"] == 1
        assert metadata["pages_with_text"] == 1
        assert metadata["pages_degraded"] == 0
        assert metadata["degraded_pages"] == []
        assert metadata["compositor_version"] == COMPOSITOR_VERSION
        assert ref["facts"]["dpi"] == DPI
        assert ref["facts"]["engine"] == "rapidocr"
    finally:
        project.close()


def test_receipt_evidence_records_degraded_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block whose text cannot be represented by the v1 Latin/Western base
    font is skipped from the invisible layer (on_unencodable='skip' default),
    which flags that page as degraded — the receipt evidence must carry that
    bookkeeping honestly (spec SS9), not just a happy-path page count."""
    _patch_engine(monkeypatch, [_stub_page("日本語")])  # CJK: unencodable
    project, sheet_id, row_ids = _seed_pdf_project(tmp_path)
    try:
        result = run_action_spec(
            project,
            _ocr_action(
                sheet_id,
                row_ids,
                idempotency_key="media_ocr_searchable@sha256:degraded",
            ),
            project_id="proj-ocr-pdf-degraded",
        )
        assert result.status == "completed"
        refs = _refs(project, result.receipt_id)
        evidence_refs = [ref for ref in refs if ref["kind"] == "row_file_output"]
        assert len(evidence_refs) == 1
        ref = evidence_refs[0]
        metadata = ref["primary"]["metadata"]
        assert metadata["_media_probe_v1"]["pages"] == 1
        assert metadata["pages_degraded"] == 1
        assert metadata["degraded_pages"] == [0]
    finally:
        project.close()


def test_receipt_evidence_skips_rows_whose_pdf_cell_failed_to_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-PDF compose hard-fail errors only the _pdf cell for that row
    (recipe-mode contract) — the evidence list must not fabricate a row entry
    for a row that never produced a blob."""
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project = Project.create(tmp_path / "action-mixed.frisket", name="OCR mixed")
    sheet_id = project.add_sheet("Scans")
    cols = {"media": project.add_column(sheet_id, "media", type="file")}
    good = project.add_blob(_source_pdf(1), filename="ok.pdf", mime="application/pdf")
    broken = project.add_blob(
        b"%PDF-1.4 broken", filename="broken.pdf", mime="application/pdf"
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {"media": media_cell(good, mime="application/pdf", filename="ok.pdf")},
            {
                "media": media_cell(
                    broken, mime="application/pdf", filename="broken.pdf"
                )
            },
        ],
        cols,
    )
    try:
        result = run_action_spec(
            project,
            _ocr_action(
                sheet_id,
                row_ids,
                idempotency_key="media_ocr_searchable@sha256:mixed-evidence",
            ),
            project_id="proj-ocr-pdf-mixed",
        )
        assert result.status == "partial"
        refs = _refs(project, result.receipt_id)
        evidence_refs = [ref for ref in refs if ref["kind"] == "row_file_output"]
        assert len(evidence_refs) == 1
        assert evidence_refs[0]["row_id"] == row_ids[0]
    finally:
        project.close()


# --------------------------------------------------------------------------
# (c) replay/stale — blob-existence check
# --------------------------------------------------------------------------


def test_replay_detects_deleted_searchable_pdf_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project, sheet_id, row_ids = _seed_pdf_project(tmp_path)
    try:
        action = _ocr_action(
            sheet_id, row_ids, idempotency_key="media_ocr_searchable@sha256:replay"
        )
        result = run_action_spec(
            project,
            action,
            project_id="proj-ocr-pdf-replay",
        )
        assert result.status == "completed"
        columns = _columns(project, sheet_id)
        pdf_col = int(columns["ocr_text_pdf"]["id"])
        pdf_cell = project.get_values(sheet_id, pdf_col, row_ids=[row_ids[0]])[
            row_ids[0]
        ]
        blob_hash = pdf_cell["blob"]

        # a plain replay (no source drift) succeeds without re-composing.
        replay = run_action_spec(
            project,
            action,
            project_id="proj-ocr-pdf-replay",
        )
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id

        # delete the composed PDF blob file (the enclosure_materialize /
        # url_capture_static precedent for simulating blob loss).
        local_blob_path(project, blob_hash).unlink()
        stale = run_action_spec(
            project,
            action,
            project_id="proj-ocr-pdf-replay",
        )
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
    finally:
        project.close()


def test_replay_detects_missing_searchable_pdf_blob_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project, sheet_id, row_ids = _seed_pdf_project(tmp_path)
    try:
        action = _ocr_action(
            sheet_id,
            row_ids,
            idempotency_key="media_ocr_searchable@sha256:replay-row",
        )
        result = run_action_spec(
            project,
            action,
            project_id="proj-ocr-pdf-replay-row",
        )
        assert result.status == "completed"
        columns = _columns(project, sheet_id)
        pdf_col = int(columns["ocr_text_pdf"]["id"])
        pdf_cell = project.get_values(sheet_id, pdf_col, row_ids=[row_ids[0]])[
            row_ids[0]
        ]
        blob_hash = pdf_cell["blob"]

        project.db.execute("DELETE FROM blobs WHERE hash=?", (blob_hash,))
        project.db.commit()
        stale = run_action_spec(
            project,
            action,
            project_id="proj-ocr-pdf-replay-row",
        )
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
    finally:
        project.close()


# --------------------------------------------------------------------------
# (d) MediaOcrOutput column id for the _pdf column
# --------------------------------------------------------------------------


def test_action_result_outputs_carry_the_pdf_column_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic outputs -> {name: column_id} plumbing (MediaOcrOutput.
    output_columns' shape) covers the _pdf column exactly like text/blocks —
    no special-casing needed since ActionOutput.column_id is already set from
    the same output_refs loop."""
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project, sheet_id, row_ids = _seed_pdf_project(tmp_path)
    try:
        result = run_action_spec(
            project,
            _ocr_action(
                sheet_id, row_ids, idempotency_key="media_ocr_searchable@sha256:cols"
            ),
            project_id="proj-ocr-pdf-cols",
        )
        assert result.status == "completed"
        output_columns = {o.name: o.column_id for o in result.outputs}
        columns = _columns(project, sheet_id)
        assert output_columns == {
            "ocr_text": int(columns["ocr_text"]["id"]),
            "ocr_text_blocks": int(columns["ocr_text_blocks"]["id"]),
            "ocr_text_pdf": int(columns["ocr_text_pdf"]["id"]),
        }
    finally:
        project.close()

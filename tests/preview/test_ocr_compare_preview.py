from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import frisket.preview.ocr as ocr_compare
from frisket.engine.store.media_blobs import media_cell
from frisket.server.app import create_app
from frisket.engine.store import Project


PDF_BYTES = b"%PDF-1.4\n% preview fixture\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\npreview"
MUTATION_TABLES = (
    "rows",
    "columns",
    "blobs",
    "runs",
    "results",
    "model_calls",
    "ops",
    "receipts",
)


def _add_pdf_row(
    project: Project,
    *,
    column_type: str = "file",
    filename: str = "docket.pdf",
    mime: str = "application/pdf",
    page_count: int = 4,
    cell_value: Any | None = None,
    data: bytes = PDF_BYTES,
) -> tuple[int, int, int, str]:
    sheet_id = project.add_sheet("Documents")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "pdf": project.add_column(sheet_id, "pdf", type=column_type),
    }
    digest = project.add_blob(
        data,
        filename=filename,
        mime=mime,
        metadata=owned_media_metadata_document(
            probe={"kind": "pdf", "pages": page_count}
        ),
    )
    if cell_value is None:
        cell_value = media_cell(
            digest,
            mime=mime,
            filename=filename,
        )
    row_id = project.add_rows(
        sheet_id,
        [{"title": "Docket", "pdf": cell_value}],
        cols,
    )[0]
    return sheet_id, row_id, cols["pdf"], digest


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in MUTATION_TABLES
    }


def _request(sheet_id: int, row_id: int, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "input_column": "pdf",
        "pages": [1, 3],
        "engines": ["rapidocr", "dots.mocr"],
        "language": "en",
        "dpi": 180,
    }
    body.update(overrides)
    return body


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
) -> None:
    async def fake_render(
        source: ocr_compare.OcrCompareSource,
        pages: list[int],
        *,
        dpi: int,
        scratch: Path,
    ) -> ocr_compare.RenderedPreviewDocument:
        calls.append(
            {
                "stage": "render",
                "blob_hash": source.blob_hash,
                "pages": pages,
                "dpi": dpi,
            }
        )
        rendered: list[ocr_compare.RenderedPreviewPage] = []
        for page in pages:
            path = scratch / f"page-{page}.png"
            path.write_bytes(b"page")
            rendered.append(
                ocr_compare.RenderedPreviewPage(
                    page=page,
                    path=path,
                    width=1000,
                    height=2000,
                )
            )
        return ocr_compare.RenderedPreviewDocument(rendered, page_count=4)

    async def fake_engine(
        engine: str,
        pages: list[ocr_compare.RenderedPreviewPage],
        *,
        project: Project,
        language: str | None,
        scratch: Path,
    ) -> ocr_compare.OcrEnginePreviewOutput:
        del project, scratch
        calls.append(
            {
                "stage": "ocr",
                "engine": engine,
                "pages": [page.page for page in pages],
                "language": language,
            }
        )
        return ocr_compare.OcrEnginePreviewOutput(
            pages=[
                {
                    "text": f"{engine} page {page.page} text",
                    "blocks": [
                        {
                            "text": f"{engine} block {page.page}",
                            "bbox": [[100, 200], [300, 200], [300, 400], [100, 400]],
                            "score": 0.77,
                        }
                    ],
                }
                for page in pages
            ],
        )

    monkeypatch.setattr(ocr_compare, "render_selected_pdf_pages", fake_render)
    monkeypatch.setattr(ocr_compare, "run_ocr_engine_preview", fake_engine)


def test_ocr_compare_preview_http_route_is_read_only_and_returns_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "OCR Preview"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id, row_id, column_id, digest = _add_pdf_row(project)
    calls: list[dict[str, Any]] = []
    _install_fakes(monkeypatch, calls)

    before = _counts(project)
    response = client.post(
        f"/api/projects/{pid}/ocr/compare-preview",
        json=_request(sheet_id, row_id),
    )
    assert response.status_code == 200
    assert _counts(project) == before

    payload = response.json()
    assert payload["schema_version"] == "frisket.ocr_compare_preview.v1"
    assert payload["source"] == {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": column_id,
        "column_name": "pdf",
        "filename": "docket.pdf",
        "mime": "application/pdf",
        "blob_hash": digest,
        "size": len(PDF_BYTES),
        "page_count": 4,
    }
    assert payload["pages"] == [1, 3]
    assert payload["engines"] == [
        {
            "id": "rapidocr",
            "label": "RapidOCR (local, default)",
            "tier": "local",
            "billable": False,
        },
        {
            "id": "dots.mocr",
            "label": "dots.mocr multilingual document parser (sidecar)",
            "tier": "sidecar",
            "billable": False,
        },
    ]
    assert payload["errors"] == []
    assert payload["warnings"] == []
    # Compare is the free surface: it emits no cost block at all, so there is
    # no second money surface to contradict the run's receipt.
    assert "cost" not in payload
    assert [
        (item["page"], item["engine"], item["text"]) for item in payload["results"]
    ] == [
        (1, "rapidocr", "rapidocr page 1 text"),
        (3, "rapidocr", "rapidocr page 3 text"),
        (1, "dots.mocr", "dots.mocr page 1 text"),
        (3, "dots.mocr", "dots.mocr page 3 text"),
    ]
    block = payload["results"][0]["blocks"][0]
    assert block["bbox"] == {
        "space": "page_normalized",
        "x0": 0.1,
        "y0": 0.1,
        "x1": 0.3,
        "y1": 0.2,
    }
    assert block["raw"]["bbox"] == [[100, 200], [300, 200], [300, 400], [100, 400]]
    assert calls == [
        {"stage": "render", "blob_hash": digest, "pages": [1, 3], "dpi": 180},
        {
            "stage": "ocr",
            "engine": "rapidocr",
            "pages": [1, 3],
            "language": "en",
        },
        {
            "stage": "ocr",
            "engine": "dots.mocr",
            "pages": [1, 3],
            "language": "en",
        },
    ]


def test_ocr_compare_preview_refuses_billable_engines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Billable -> run. A provider/model VLM id is refused while the request is
    # coerced, so nothing dispatches and no money can be spent. There is no
    # allow_remote to flip and no router parameter to pass.
    project = Project.create(tmp_path / "remote.frisket", name="Remote OCR Preview")
    try:
        sheet_id, row_id, _column_id, _digest = _add_pdf_row(project)
        calls: list[dict[str, Any]] = []
        _install_fakes(monkeypatch, calls)

        with pytest.raises(ocr_compare.OcrComparePreviewError) as blocked:
            asyncio.run(
                ocr_compare.compare_ocr_preview(
                    project,
                    _request(
                        sheet_id,
                        row_id,
                        engines=["rapidocr", "provider/model"],
                    ),
                )
            )
        assert blocked.value.code == "billable_engine_requires_run"
        assert blocked.value.details == {
            "engine": "provider/model",
            "action": "media.ocr",
        }
        assert "media.ocr" in blocked.value.message
        assert calls == []
        # Even asking nicely is unspellable: the entry point takes no router.
        assert (
            "router"
            not in inspect.signature(ocr_compare.compare_ocr_preview).parameters
        )
    finally:
        project.close()


@pytest.mark.parametrize(
    "pages, expected_field",
    [
        ([], "pages"),
        ([1, 1], "pages"),
        ([0], "pages[0]"),
        ([True], "pages[0]"),
        (list(range(1, 12)), "pages"),
        ([5], "pages"),
    ],
)
def test_ocr_compare_preview_rejects_invalid_pages(
    tmp_path: Path,
    pages: list[Any],
    expected_field: str,
) -> None:
    project = Project.create(tmp_path / "pages.frisket", name="Page Validation")
    try:
        sheet_id, row_id, _column_id, _digest = _add_pdf_row(project, page_count=4)
        with pytest.raises(ocr_compare.OcrComparePreviewError) as excinfo:
            asyncio.run(
                ocr_compare.compare_ocr_preview(
                    project,
                    _request(sheet_id, row_id, pages=pages),
                )
            )
        assert excinfo.value.code == "invalid_page_ref"
        assert excinfo.value.field == expected_field
    finally:
        project.close()


def test_ocr_compare_preview_rejects_missing_non_blob_non_pdf_and_unknown_engines(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "invalid.frisket", name="Invalid OCR Preview")
    try:
        sheet_id, row_id, _column_id, _digest = _add_pdf_row(project)
        missing_row = _request(sheet_id, row_id)
        missing_row["row_id"] = row_id + 999

        cases = [
            (
                missing_row,
                "invalid_input_ref",
                "row_id",
            ),
            (
                _request(sheet_id, row_id, engines=["rapidocr", "madeup"]),
                "invalid_ocr_engine",
                "engines[1]",
            ),
            (
                # A retired venue word is an unknown engine now,
                # not an alias-duplicate.
                _request(sheet_id, row_id, engines=["rapidocr", "light"]),
                "invalid_ocr_engine",
                "engines[1]",
            ),
            (
                # Alias-duplicate detection still fires on a LIVE alias.
                _request(sheet_id, row_id, engines=["tesseract", "tess"]),
                "invalid_ocr_engine",
                "engines",
            ),
        ]
        for body, code, field in cases:
            with pytest.raises(ocr_compare.OcrComparePreviewError) as excinfo:
                asyncio.run(ocr_compare.compare_ocr_preview(project, body))
            assert excinfo.value.code == code
            assert excinfo.value.field == field
    finally:
        project.close()

    non_blob = Project.create(tmp_path / "non-blob.frisket", name="Non Blob")
    try:
        sheet_id, row_id, _column_id, _digest = _add_pdf_row(
            non_blob,
            cell_value="not a blob",
        )
        with pytest.raises(ocr_compare.OcrComparePreviewError) as excinfo:
            asyncio.run(
                ocr_compare.compare_ocr_preview(non_blob, _request(sheet_id, row_id))
            )
        assert excinfo.value.code == "invalid_input_ref"
        assert excinfo.value.field == "input_column"
    finally:
        non_blob.close()

    non_pdf = Project.create(tmp_path / "non-pdf.frisket", name="Non PDF")
    try:
        sheet_id, row_id, _column_id, _digest = _add_pdf_row(
            non_pdf,
            filename="scan.png",
            mime="image/png",
            data=PNG_BYTES,
        )
        with pytest.raises(ocr_compare.OcrComparePreviewError) as excinfo:
            asyncio.run(
                ocr_compare.compare_ocr_preview(non_pdf, _request(sheet_id, row_id))
            )
        assert excinfo.value.code == "invalid_input_ref"
        assert excinfo.value.field == "input_column"
    finally:
        non_pdf.close()

    wrong_column_type = Project.create(
        tmp_path / "wrong-column.frisket",
        name="Wrong Column",
    )
    try:
        sheet_id, row_id, _column_id, _digest = _add_pdf_row(
            wrong_column_type,
            column_type="text",
        )
        with pytest.raises(ocr_compare.OcrComparePreviewError) as excinfo:
            asyncio.run(
                ocr_compare.compare_ocr_preview(
                    wrong_column_type,
                    _request(sheet_id, row_id),
                )
            )
        assert excinfo.value.code == "invalid_input_ref"
        assert excinfo.value.field == "input_column"
    finally:
        wrong_column_type.close()

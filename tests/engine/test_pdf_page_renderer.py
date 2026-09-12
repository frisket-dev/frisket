"""Runtime checks for the shared fenced PDFium page renderer."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image
import pytest
from pypdf import PdfWriter

from frisket.actions.types import PdfDocument, TableError
from frisket.engine import pdf_render
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.executor.pdf_page_read import AdmittedPdfPageRenderer


def _pdf(path: Path, *, rotate: bool = False, crop: bool = False) -> None:
    writer = PdfWriter()
    first = writer.add_blank_page(width=72, height=144)
    if rotate:
        first.rotate(90)
    if crop:
        first.cropbox.lower_left = (18, 36)
        first.cropbox.upper_right = (54, 108)
    writer.add_blank_page(width=144, height=72)
    with path.open("wb") as stream:
        writer.write(stream)


def _document(blobs: AdmittedImportBlobStager, path: Path):
    return blobs.stage(
        io.BytesIO(path.read_bytes()),
        filename="document.pdf",
        mime="application/pdf",
        role=PdfDocument(),
    )


def test_pdfium_renders_selected_one_based_pages_at_requested_dpi(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output"
    output.mkdir()
    _pdf(source)

    rendered = asyncio.run(
        pdf_render.render_pdf_pages(source, output, dpi=144, pages=[2, 1])
    )

    assert rendered.page_count == 2
    assert [page for page, _path in rendered.pages] == [2, 1]
    with Image.open(output / "page-1.png") as image:
        assert image.size == (144, 288)
    with Image.open(output / "page-2.png") as image:
        assert image.size == (288, 144)


def test_pdfium_keeps_intrinsic_rotation_and_honors_page_limit(tmp_path):
    source = tmp_path / "rotated.pdf"
    output = tmp_path / "output"
    output.mkdir()
    _pdf(source, rotate=True)

    rendered = asyncio.run(
        pdf_render.render_pdf_pages(source, output, dpi=72, page_limit=1)
    )

    assert rendered.page_count == 2
    assert [page for page, _path in rendered.pages] == [1]
    with Image.open(output / "page-1.png") as image:
        assert image.size == (144, 72)


def test_pdfium_page_limit_zero_renders_no_pages(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output"
    output.mkdir()
    _pdf(source)

    rendered = asyncio.run(
        pdf_render.render_pdf_pages(source, output, dpi=72, page_limit=0)
    )

    assert rendered.page_count == 2
    assert rendered.pages == ()
    assert list(output.iterdir()) == []


def test_pdfium_honors_the_pdf_crop_box(tmp_path):
    source = tmp_path / "cropped.pdf"
    output = tmp_path / "output"
    output.mkdir()
    _pdf(source, crop=True)

    asyncio.run(pdf_render.render_pdf_pages(source, output, dpi=72, pages=[1]))

    with Image.open(output / "page-1.png") as image:
        assert image.size == (36, 72)


def test_admitted_renderer_keeps_source_until_worker_settles_and_stages_pages(tmp_path):
    source = tmp_path / "source.pdf"
    _pdf(source)
    with AdmittedImportBlobStager() as blobs:
        document = _document(blobs, source)
        renderer = AdmittedPdfPageRenderer(blobs, page_limit=1)
        images = renderer.render(document, dpi=72)

        assert list(images) == [1]
        image = blobs.describe(images[1])
        assert image.filename == "document-p0001.png"
        assert image.page == 1
        assert image.path.is_file()
        renderer.close()


def test_malformed_or_encrypted_pdf_keeps_import_renderer_soft(tmp_path):
    malformed = tmp_path / "malformed.pdf"
    malformed.write_bytes(b"%PDF-not-valid")
    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    with encrypted.open("wb") as stream:
        writer.write(stream)

    with AdmittedImportBlobStager() as blobs:
        renderer = AdmittedPdfPageRenderer(blobs)
        assert renderer.render(_document(blobs, malformed), dpi=72) == {}
        assert renderer.render(_document(blobs, encrypted), dpi=72) == {}


def test_renderer_cancellation_reaches_the_owned_worker_boundary(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    _pdf(source)
    with AdmittedImportBlobStager() as blobs:
        renderer = AdmittedPdfPageRenderer(blobs)

        async def cancelled(*_args, should_cancel=None, **_kwargs):
            assert should_cancel is not None and not should_cancel()
            renderer.close()
            assert should_cancel()
            raise pdf_render.PdfRenderCancelled("cancelled")

        monkeypatch.setattr(
            "frisket.engine.executor.pdf_page_read.render_pdf_pages", cancelled
        )
        with pytest.raises(TableError, match="cancelled"):
            renderer.render(_document(blobs, source), dpi=72)


def test_renderer_rejects_child_nonregular_outputs(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    _pdf(source)
    with AdmittedImportBlobStager() as blobs:
        renderer = AdmittedPdfPageRenderer(blobs)

        async def malicious(_source, scratch, **_kwargs):
            (scratch / "page-1.png").mkdir()
            return pdf_render.PdfRenderResult(1, ((1, scratch / "page-1.png"),))

        monkeypatch.setattr(
            "frisket.engine.executor.pdf_page_read.render_pdf_pages", malicious
        )
        assert renderer.render(_document(blobs, source), dpi=72) == {}


@pytest.mark.parametrize("dpi", [49, 601, True, 72.5])
def test_renderer_rejects_invalid_dpi(tmp_path, dpi):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output"
    output.mkdir()
    _pdf(source)
    with pytest.raises(ValueError, match="dpi"):
        asyncio.run(pdf_render.render_pdf_pages(source, output, dpi=dpi, pages=[1]))


@pytest.mark.parametrize("page_limit", [-1, True, 1.5])
def test_admitted_renderer_rejects_invalid_page_limits(page_limit):
    with AdmittedImportBlobStager() as blobs:
        with pytest.raises(ValueError, match="nonnegative integer"):
            AdmittedPdfPageRenderer(blobs, page_limit=page_limit)

"""OCR's one actual rasterization is bounded and cooperatively cancellable."""

import asyncio

import pytest

from frisket.ops import ocr_engines
from frisket.engine.pdf_render import PdfRenderCancelled, PdfRenderError


@pytest.mark.parametrize("cancelled", [False, True])
def test_pdf_rasterization_does_not_continue_after_timeout_or_cancel(
    tmp_path, monkeypatch, cancelled
):
    source = tmp_path / "malformed.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    calls = []

    async def bounded_render(*_args, timeout_seconds, should_cancel, **_kwargs):
        assert timeout_seconds == 600
        assert should_cancel() is cancelled
        calls.append(True)
        if cancelled:
            raise PdfRenderCancelled("cancelled")
        raise PdfRenderError("render stopped")

    monkeypatch.setattr(ocr_engines, "render_pdf_pages", bounded_render)
    engine = ocr_engines.OcrEngines(cancelled=lambda: cancelled)
    expected = ocr_engines.OcrCancelled if cancelled else RuntimeError
    with pytest.raises(expected):
        asyncio.run(
            engine._page_images(
                source, {"mime": "application/pdf"}, {"dpi": 150}, tmp_path
            )
        )
    assert len(calls) == 1

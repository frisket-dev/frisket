"""Docling's assembled-page OCR provenance shape."""

from types import SimpleNamespace

from frisket_models.engines import _docling_page_used_ocr


def _page(*from_ocr: bool):
    cells = [SimpleNamespace(from_ocr=value) for value in from_ocr]
    cluster = SimpleNamespace(cells=cells)
    layout = SimpleNamespace(clusters=[cluster])
    # Docling 2.108's Page.cells is empty even when layout cells came from OCR.
    return SimpleNamespace(cells=[], predictions=SimpleNamespace(layout=layout))


def test_assembled_text_cells_report_no_ocr() -> None:
    assert _docling_page_used_ocr(_page(False, False)) is False


def test_assembled_scan_cells_report_ocr() -> None:
    assert _docling_page_used_ocr(_page(False, True)) is True

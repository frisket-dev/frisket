from __future__ import annotations

import io

import pytest

from pypdf import PdfReader, PdfWriter

from frisket.ops.searchable_pdf import (
    COMPOSITOR_VERSION,
    PageComposeResult,
    UnencodableTextError,
    compose_searchable_pdf,
)

DPI = 200
# a US-Letter page: 612x792 pt -> at 200 dpi the raster is 1700x2200 px.
PAGE_W_PT = 612.0
PAGE_H_PT = 792.0


def _blank_source(pages):
    """pages: list of (w_pt, h_pt, rotate_degrees). Returns PDF bytes."""
    writer = PdfWriter()
    for w, h, rot in pages:
        page = writer.add_blank_page(width=w, height=h)
        if rot:
            page.rotate(rot)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _line_block(text, x0, y0, x1, y1):
    """4-corner pixel polygon (TL, TR, BR, BL) — the ops/ocr.py block shape."""
    return {
        "text": text,
        "bbox": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "score": 0.99,
    }


def _extract(pdf_bytes, index=0):
    return PdfReader(io.BytesIO(pdf_bytes)).pages[index].extract_text()


def _is_valid_pdf(pdf_bytes):
    if not pdf_bytes.startswith(b"%PDF-"):
        return False
    PdfReader(io.BytesIO(pdf_bytes))  # raises if unparseable
    return True


# --------------------------------------------------------------------------
# core: the layer is real + searchable
# --------------------------------------------------------------------------


def test_single_line_text_is_searchable():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    blocks = [_line_block("Hello World", 100, 100, 700, 160)]
    pdf, results = compose_searchable_pdf(src, [blocks], dpi=DPI)
    assert _is_valid_pdf(pdf)
    assert "Hello World" in _extract(pdf)
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, PageComposeResult)
    assert r.painted_count == 1
    assert r.degraded is False


def test_multi_line_all_present():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    blocks = [
        _line_block("First line here", 100, 100, 900, 160),
        _line_block("Second line below", 100, 260, 950, 320),
        _line_block("Third and final", 100, 420, 800, 480),
    ]
    pdf, results = compose_searchable_pdf(src, [blocks], dpi=DPI)
    text = _extract(pdf)
    assert "First line here" in text
    assert "Second line below" in text
    assert "Third and final" in text
    assert results[0].painted_count == 3


def test_multi_page_each_page_gets_its_own_text():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0), (PAGE_W_PT, PAGE_H_PT, 0)])
    p0 = [_line_block("Page one content", 100, 100, 900, 160)]
    p1 = [_line_block("Page two content", 100, 100, 900, 160)]
    pdf, results = compose_searchable_pdf(src, [p0, p1], dpi=DPI)
    assert len(results) == 2
    assert "Page one content" in _extract(pdf, 0)
    assert "Page two content" in _extract(pdf, 1)
    assert "Page two content" not in _extract(pdf, 0)


def test_accepts_page_mapping_shape():
    """A page may arrive as the ops/ocr.py {'text':..., 'blocks':[...]} dict."""
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    page = {
        "text": "Mapped shape",
        "blocks": [_line_block("Mapped shape", 100, 100, 800, 160)],
    }
    pdf, results = compose_searchable_pdf(src, [page], dpi=DPI)
    assert "Mapped shape" in _extract(pdf)
    assert results[0].painted_count == 1


# --------------------------------------------------------------------------
# empty + degraded page semantics (SS9)
# --------------------------------------------------------------------------


def test_empty_page_is_valid_and_not_degraded():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    pdf, results = compose_searchable_pdf(src, [[]], dpi=DPI)
    assert _is_valid_pdf(pdf)
    assert results[0].block_count == 0
    assert results[0].painted_count == 0
    assert results[0].degraded is False


def test_empty_page_between_text_pages_still_valid():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)] * 3)
    p0 = [_line_block("Top page", 100, 100, 800, 160)]
    p2 = [_line_block("Bottom page", 100, 100, 800, 160)]
    pdf, results = compose_searchable_pdf(src, [p0, [], p2], dpi=DPI)
    assert len(results) == 3
    assert "Top page" in _extract(pdf, 0)
    assert results[1].degraded is False and results[1].painted_count == 0
    assert "Bottom page" in _extract(pdf, 2)


def test_degenerate_bbox_skipped_page_flagged_degraded():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    blocks = [
        _line_block("Good line", 100, 100, 800, 160),
        _line_block("Zero width", 300, 300, 300, 360),  # degenerate: x0==x1
    ]
    pdf, results = compose_searchable_pdf(src, [blocks], dpi=DPI)
    # the good line survives, the degenerate one is dropped, page flagged
    assert "Good line" in _extract(pdf)
    r = results[0]
    assert r.painted_count == 1
    assert r.skipped_count == 1
    assert r.degraded is True


def test_source_page_count_mismatch_extra_pages_get_empty_layer():
    # fewer block-lists than pages -> trailing pages simply get no text layer.
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0), (PAGE_W_PT, PAGE_H_PT, 0)])
    pdf, results = compose_searchable_pdf(
        src, [[_line_block("Only first", 100, 100, 800, 160)]], dpi=DPI
    )
    assert len(results) == 2
    assert "Only first" in _extract(pdf, 0)
    assert results[1].painted_count == 0


# --------------------------------------------------------------------------
# invisible render mode (text extracts but does not paint)
# --------------------------------------------------------------------------


def test_render_mode_is_invisible_no_visible_text_operators():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    blocks = [_line_block("Invisible ink", 100, 100, 800, 160)]
    pdf, _ = compose_searchable_pdf(src, [blocks], dpi=DPI)
    reader = PdfReader(io.BytesIO(pdf))
    raw = reader.pages[0].get_contents().get_data()
    # render mode 3 == invisible (neither fill nor stroke)
    assert b"3 Tr" in raw
    # no visible text render modes (0 fill, 1 stroke, 2 fill+stroke)
    for visible in (b"0 Tr", b"1 Tr", b"2 Tr"):
        assert visible not in raw
    # but the text is still extractable (the layer is real)
    assert "Invisible ink" in _extract(pdf)


# --------------------------------------------------------------------------
# rotated page (the genuinely fiddly case, SS4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_rotated_page_text_is_searchable(rot):
    # MediaBox stays 612x792; /Rotate uprights the render, so blocks are in the
    # upright (displayed) raster space just like a real pdftoppm render.
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, rot)])
    blocks = [_line_block("Rotated text", 100, 100, 800, 160)]
    pdf, results = compose_searchable_pdf(src, [blocks], dpi=DPI)
    assert _is_valid_pdf(pdf)
    assert "Rotated text" in _extract(pdf)
    assert results[0].painted_count == 1


# --------------------------------------------------------------------------
# CJK contract — explicit skip / error, never silent garbage (SS5.3, SS12)
# --------------------------------------------------------------------------


def test_cjk_block_skipped_by_default_no_garbage():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    blocks = [
        _line_block("Latin ok", 100, 100, 800, 160),
        _line_block("日本語テキスト", 100, 300, 800, 360),  # Japanese
    ]
    pdf, results = compose_searchable_pdf(src, [blocks], dpi=DPI)
    text = _extract(pdf)
    assert "Latin ok" in text
    # the CJK glyphs are NOT painted as mojibake '?' garbage
    assert "?" not in text
    r = results[0]
    assert r.painted_count == 1
    assert r.skipped_count == 1
    assert r.degraded is True
    assert r.reason == "unencodable_text"


def test_fully_cjk_page_degraded_empty_layer_no_garbage():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    blocks = [_line_block("한국어", 100, 100, 800, 160)]  # Korean
    pdf, results = compose_searchable_pdf(src, [blocks], dpi=DPI)
    assert _is_valid_pdf(pdf)
    assert "?" not in _extract(pdf)
    assert results[0].painted_count == 0
    assert results[0].degraded is True


def test_cjk_error_contract_raises():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    blocks = [_line_block("中文", 100, 100, 800, 160)]  # Chinese
    with pytest.raises(UnencodableTextError):
        compose_searchable_pdf(src, [blocks], dpi=DPI, on_unencodable="error")


# --------------------------------------------------------------------------
# whole-PDF hard fail (SS9) — corrupt source raises, does not silently pass
# --------------------------------------------------------------------------


def test_corrupt_source_raises():
    with pytest.raises(Exception):
        compose_searchable_pdf(b"not a pdf at all", [[]], dpi=DPI)


# --------------------------------------------------------------------------
# compositor version is exposed for cache-keying (SS7.3)
# --------------------------------------------------------------------------


def test_compositor_version_is_positive_int():
    assert isinstance(COMPOSITOR_VERSION, int)
    assert COMPOSITOR_VERSION >= 1


# ==========================================================================
# POSITION assertions via pdfminer.six (dev-group only; importorskip).
# ==========================================================================


def _pdfminer_words(pdf_bytes, index=0):
    pdfminer_high = pytest.importorskip("pdfminer.high_level")
    from pdfminer.layout import LTTextLine, LTTextContainer

    words = []
    pages = list(pdfminer_high.extract_pages(io.BytesIO(pdf_bytes)))
    page = pages[index]
    page_bbox = page.bbox

    def walk(obj):
        for child in obj:
            if isinstance(child, LTTextLine):
                words.append((child.get_text().strip(), child.bbox))
            elif isinstance(child, LTTextContainer):
                walk(child)

    walk(page)
    return words, page_bbox


def test_position_y_flip_top_of_page_lands_near_top():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    # block near the TOP of the raster (small pixel y).
    blocks = [_line_block("Near the top", 100, 40, 700, 100)]
    pdf, _ = compose_searchable_pdf(src, [blocks], dpi=DPI)
    words, page_bbox = _pdfminer_words(pdf)
    assert words, "pdfminer found no text"
    text, bbox = words[0]
    assert "Near the top" in text
    x0, y0, x1, y1 = bbox
    page_top = page_bbox[3]
    # y baseline should be within the top ~10% of the page (bottom-left origin)
    assert y1 > page_top * 0.9, f"expected near top {page_top}, got {y1}"
    # x origin maps px->pt: 100px * 72/200 = 36pt
    assert abs(x0 - 36.0) < 4.0, f"expected x0~36, got {x0}"


def test_position_tz_width_fit_matches_bbox_width():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    # bbox spans 100..1300 px -> (1300-100)*72/200 = 432 pt wide.
    blocks = [_line_block("Width fitted line of text", 100, 100, 1300, 160)]
    pdf, _ = compose_searchable_pdf(src, [blocks], dpi=DPI)
    words, _ = _pdfminer_words(pdf)
    assert words
    _text, (x0, y0, x1, y1) = words[0]
    expected_w = (1300 - 100) * 72.0 / DPI
    painted_w = x1 - x0
    # Tz should fit the advance width to the bbox within a loose tolerance.
    assert abs(painted_w - expected_w) / expected_w < 0.15, (
        f"painted width {painted_w} vs expected {expected_w}"
    )


def test_position_two_columns_land_left_and_right():
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 0)])
    # left column x ~100-700 px, right column x ~1000-1600 px, same rows.
    blocks = [
        _line_block("LEFTCOL", 100, 100, 700, 160),
        _line_block("RIGHTCOL", 1000, 100, 1600, 160),
    ]
    pdf, _ = compose_searchable_pdf(src, [blocks], dpi=DPI)
    words, _ = _pdfminer_words(pdf)
    by_text = {t: b for (t, b) in words}
    assert "LEFTCOL" in by_text and "RIGHTCOL" in by_text
    assert by_text["LEFTCOL"][0] < by_text["RIGHTCOL"][0], (
        "left column must be left of right"
    )


def test_position_rotated_lands_upright_in_displayed_space():
    # pdfminer reports coordinates in DISPLAYED space (it applies /Rotate), so a
    # top-left block must come back horizontal + top-left for a rotated page too.
    src = _blank_source([(PAGE_W_PT, PAGE_H_PT, 90)])
    blocks = [_line_block("Upright please", 100, 100, 800, 160)]
    pdf, _ = compose_searchable_pdf(src, [blocks], dpi=DPI)
    words, page_bbox = _pdfminer_words(pdf)
    assert words
    text, (x0, y0, x1, y1) = words[0]
    assert "Upright please" in text
    # displayed page for /Rotate 90 is 792 wide x 612 tall
    assert abs(page_bbox[2] - PAGE_H_PT) < 1.0
    assert abs(page_bbox[3] - PAGE_W_PT) < 1.0
    # x origin near left (36pt), baseline near top of the 612-tall displayed page
    assert abs(x0 - 36.0) < 4.0
    assert y1 > page_bbox[3] * 0.9

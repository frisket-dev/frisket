"""Native text geometry agrees with the displayed full-MediaBox PDF."""

import asyncio
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from frisket.engine.pdf_text import extract_pdf_text, PdfTextError, PdfTextCancelled


def _pdf(path: Path, *, rotation=0, origin=(0, 0)):
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=100)
    x, y = origin
    page.mediabox.lower_left = origin
    page.mediabox.upper_right = (x + 200, y + 100)
    # Deliberately exclude the text from the CropBox. Display and extraction
    # both use MediaBox rather than this author-selected crop.
    page.cropbox.lower_left = (x + 100, y + 10)
    page.cropbox.upper_right = (x + 180, y + 50)
    if rotation:
        page.rotate(rotation)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        f"BT /F1 12 Tf {x + 20} {y + 70} Td (NAME Alice) Tj 0 -20 Td (ARRESTED X) Tj ET".encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_blank_page(width=200, height=100)
    with path.open("wb") as handle:
        writer.write(handle)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("origin", [(0, 0), (20, -30)])
def test_words_rotation_crop_and_nonzero_origin(tmp_path, rotation, origin):
    source = tmp_path / "source.pdf"
    _pdf(source, rotation=rotation, origin=origin)
    pages = asyncio.run(extract_pdf_text(source))
    assert len(pages) == 2
    page = pages[0]
    assert (page.width, page.height) == (
        (100, 200) if rotation in (90, 270) else (200, 100)
    )
    # Preserve PDFium reading order, which places the leftmost vertical text
    # column first after 90-degree rotation (not original content-stream order).
    expected_words = (
        ["ARRESTED", "X", "NAME", "Alice"]
        if rotation == 90
        else ["NAME", "Alice", "ARRESTED", "X"]
    )
    assert [token.text for token in page.tokens] == expected_words
    assert all(token.granularity == "word" for token in page.tokens)
    assert pages[1].page == 2
    assert pages[1].tokens == []
    first = next(token.box for token in page.tokens if token.text == "NAME")
    # Helvetica NAME occupies x~20..55, y~70..79 on the original page.
    expected = {
        0: (0.1, 0.21, 0.28, 0.31),
        90: (0.69, 0.1, 0.79, 0.28),
        180: (0.72, 0.69, 0.9, 0.79),
        270: (0.21, 0.72, 0.31, 0.9),
    }[rotation]
    assert (first.x0, first.y0, first.x1, first.y1) == pytest.approx(
        expected, abs=0.025
    )


@pytest.mark.parametrize("kwargs", [{"max_pages": 1}, {"max_chars": 4}])
def test_limits_reject_instead_of_returning_partial_document(tmp_path, kwargs):
    source = tmp_path / "source.pdf"
    _pdf(source)
    with pytest.raises(PdfTextError, match="limit"):
        asyncio.run(extract_pdf_text(source, **kwargs))


def test_invalid_pdf_has_safe_error(tmp_path):
    source = tmp_path / "private-name.pdf"
    source.write_bytes(b"not a PDF: private contents")
    with pytest.raises(PdfTextError, match="^PDF text extraction failed$"):
        asyncio.run(extract_pdf_text(source))


def test_cancel_before_reading(tmp_path):
    with pytest.raises(PdfTextCancelled):
        asyncio.run(
            extract_pdf_text(tmp_path / "missing.pdf", should_cancel=lambda: True)
        )


@pytest.mark.parametrize(
    "kwargs", [{"max_pages": 0}, {"max_chars": True}, {"timeout_seconds": -1}]
)
def test_invalid_limits_do_not_launch_worker(tmp_path, kwargs):
    with pytest.raises(ValueError, match="positive integer"):
        asyncio.run(extract_pdf_text(tmp_path / "missing.pdf", **kwargs))

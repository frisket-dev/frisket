import pytest
from tempfile import SpooledTemporaryFile
from pypdf import PdfReader, PdfWriter

from frisket.pdf_packets.materialize import write_pdf_range


def test_write_actual_page_ranges(tmp_path):
    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    for width in (101, 202, 303):
        writer.add_blank_page(width=width, height=100)
    writer.write(source)
    reader = PdfReader(source)
    single = tmp_path / "single.pdf"
    multi = tmp_path / "multi.pdf"
    write_pdf_range(reader, single, 2, 2)
    write_pdf_range(reader, multi, 2, 3)
    assert [int(p.mediabox.width) for p in PdfReader(single).pages] == [202]
    assert [int(p.mediabox.width) for p in PdfReader(multi).pages] == [202, 303]
    with pytest.raises(ValueError):
        write_pdf_range(reader, tmp_path / "invalid.pdf", 2, 4)
    assert not (tmp_path / "invalid.pdf").exists()
    with SpooledTemporaryFile(max_size=32, mode="w+b") as stream:
        write_pdf_range(reader, stream, 3, 3)
        assert not stream.closed
        stream.seek(0)
        assert [int(p.mediabox.width) for p in PdfReader(stream).pages] == [303]

from __future__ import annotations

import io
from pathlib import Path

import pytest

from frisket.engine.executor.pdf_page_source import (
    PdfPageSourceError,
    materialize_pdf_page,
)

pypdf = pytest.importorskip("pypdf")


def _two_page_pdf() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=111, height=222)
    writer.add_blank_page(width=333, height=444)
    output = io.BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


def test_materialize_pdf_page_writes_only_selected_page(tmp_path: Path) -> None:
    raw = _two_page_pdf()
    source = tmp_path / "source.pdf"
    selected = tmp_path / "selected.pdf"
    source.write_bytes(raw)

    assert materialize_pdf_page(source, page=2, destination=selected) == selected
    result = pypdf.PdfReader(selected)
    assert len(result.pages) == 1
    assert float(result.pages[0].mediabox.width) == 333
    assert float(result.pages[0].mediabox.height) == 444
    assert source.read_bytes() == raw


@pytest.mark.parametrize("page", [0, True, 3])
def test_materialize_pdf_page_refuses_invalid_selector(
    tmp_path: Path, page: int
) -> None:
    source = tmp_path / "source.pdf"
    selected = tmp_path / "selected.pdf"
    source.write_bytes(_two_page_pdf())

    with pytest.raises(PdfPageSourceError):
        materialize_pdf_page(source, page=page, destination=selected)
    assert not selected.exists()

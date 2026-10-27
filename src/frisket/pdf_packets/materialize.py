"""Write genuine child PDFs from a borrowed source reader."""

from pathlib import Path
from collections.abc import Callable
from typing import BinaryIO

from pypdf import PdfReader, PdfWriter


def write_pdf_range(
    reader: PdfReader,
    path: Path | BinaryIO,
    start: int,
    end: int,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Write a range to a path or borrowed stream; reader and stream stay open."""
    if (
        type(start) is not int
        or type(end) is not int
        or not 1 <= start <= end <= len(reader.pages)
    ):
        raise ValueError("PDF range is outside the source")
    writer = PdfWriter()
    for page in range(start, end + 1):
        if should_cancel is not None and (page - start) % 16 == 0 and should_cancel():
            raise InterruptedError("PDF splitting was stopped")
        writer.add_page(reader.pages[page - 1])
    if should_cancel is not None and should_cancel():
        raise InterruptedError("PDF splitting was stopped")
    if isinstance(path, Path):
        with path.open("wb") as sink:
            writer.write(sink)
    else:
        writer.write(path)

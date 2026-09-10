"""RED-first adapter checks for the real natural-pdf extraction shape.

Source: github.com/jsoma/natural-pdf (natural_pdf/tables/result.py,
natural_pdf/flows/flow.py). The real `TableResult` subclasses
`collections.abc.Sequence` (iterating yields rows as lists) and exposes a
`.headers` property; multi-page tables are stitched with
`pdf.pages.to_flow().extract_table()`, which returns a single `TableResult`.
The adapter under test (src/frisket/ops/integrations/natural_pdf.py) must target
that exact API and coerce one `TableResult` into exactly one `PdfTable` rather
than probing document-level extractors or shredding a single result per row.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from frisket.ops.integrations import natural_pdf


class FakeTableResult(Sequence):
    """Mirror of natural_pdf TableResult: a Sequence of row lists + .headers."""

    def __init__(self, rows: list[list[Any]], headers: list[str] | None) -> None:
        self._rows = [list(row) for row in rows]
        self._headers = list(headers) if headers is not None else None

    def __getitem__(self, index: Any) -> Any:
        return self._rows[index]

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def headers(self) -> list[str]:
        if self._headers is not None:
            return self._headers
        return list(self._rows[0]) if self._rows else []


def _install_fake_natural_pdf(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: Any,
    captured_kwargs: dict[str, Any] | None = None,
    raises: Exception | None = None,
) -> None:
    class FakeFlow:
        def extract_table(self, **kwargs: Any) -> Any:
            if captured_kwargs is not None:
                captured_kwargs.clear()
                captured_kwargs.update(kwargs)
            if raises is not None:
                raise raises
            return result

    class FakePages:
        def to_flow(self) -> FakeFlow:
            return FakeFlow()

    class FakePdf:
        def __init__(self, path: str) -> None:
            self.path = path
            self.pages = FakePages()

    module = types.ModuleType("natural_pdf")
    module.PDF = FakePdf
    monkeypatch.setitem(sys.modules, "natural_pdf", module)


def _request(
    tmp_path: Path, *, mode: str = "extract_table", options: dict | None = None
):
    pdf_path = tmp_path / "tables.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% adapter fixture\n")
    return natural_pdf.PdfTableExtractRequest(
        path=pdf_path,
        filename="tables.pdf",
        source_row_id=1,
        source_blob_hash="sha256:test",
        mode=mode,
        options=options or {},
    )


def test_single_multi_page_flow_table_yields_one_pdf_table(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rows from several pages, stitched by to_flow() into one TableResult.
    result = FakeTableResult(
        rows=[["Acme", "10"], ["Beta", "12"], ["Gamma", "14"]],
        headers=["vendor", "amount"],
    )
    _install_fake_natural_pdf(monkeypatch, result=result)

    tables = natural_pdf.extract_pdf_tables(_request(tmp_path))

    assert len(tables) == 1, "a single TableResult must not be shredded per-row"
    table = tables[0]
    assert table.table_index == 0
    assert table.header == ["vendor", "amount"]
    assert table.rows == [["Acme", "10"], ["Beta", "12"], ["Gamma", "14"]]


def test_headers_falling_back_to_first_row_drops_header_from_body(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No explicit headers: TableResult.headers returns the first row.
    result = FakeTableResult(
        rows=[["vendor", "amount"], ["Acme", "10"]],
        headers=None,
    )
    _install_fake_natural_pdf(monkeypatch, result=result)

    tables = natural_pdf.extract_pdf_tables(_request(tmp_path))

    assert len(tables) == 1
    assert tables[0].header == ["vendor", "amount"]
    assert tables[0].rows == [["Acme", "10"]]
    assert tables[0].raw_cells == [["Acme", "10"]]


def test_adapter_preserves_null_cells_and_normalizes_text_values(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = FakeTableResult(
        rows=[[None, 10]],
        headers=["vendor", "amount"],
    )
    _install_fake_natural_pdf(monkeypatch, result=result)

    tables = natural_pdf.extract_pdf_tables(_request(tmp_path))

    assert tables[0].rows == [[None, "10"]]
    assert tables[0].raw_cells == [[None, 10]]


def test_adapter_normalizes_arbitrary_headers_without_overwriting_provenance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = FakeTableResult(
        rows=[["Acme", "Primary", "Alias", 99, "raw"]],
        headers=["", " vendor ", "vendor", "source_row_id", "raw_cells_json"],
    )
    _install_fake_natural_pdf(monkeypatch, result=result)

    tables = natural_pdf.extract_pdf_tables(_request(tmp_path))

    assert tables[0].header == [
        "column_1",
        "vendor",
        "vendor_2",
        "source_row_id_2",
        "raw_cells_json_2",
    ]
    assert tables[0].rows == [["Acme", "Primary", "Alias", "99", "raw"]]
    assert tables[0].raw_cells == [["Acme", "Primary", "Alias", 99, "raw"]]


def test_extract_table_kwargs_forwarded_from_options(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    _install_fake_natural_pdf(
        monkeypatch,
        result=FakeTableResult([["a"]], headers=["h"]),
        captured_kwargs=captured,
    )

    natural_pdf.extract_pdf_tables(
        _request(tmp_path, options={"options": {"method": "text"}})
    )

    assert captured == {"method": "text"}


def test_empty_result_yields_no_tables(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_natural_pdf(monkeypatch, result=FakeTableResult([], headers=None))
    assert natural_pdf.extract_pdf_tables(_request(tmp_path)) == []

    _install_fake_natural_pdf(monkeypatch, result=None)
    assert natural_pdf.extract_pdf_tables(_request(tmp_path)) == []


def test_unsupported_mode_raises(tmp_path) -> None:
    with pytest.raises(natural_pdf.NaturalPdfUnsupportedMode):
        natural_pdf.extract_pdf_tables(_request(tmp_path, mode="natural_pdf_snippet"))


def test_missing_module_raises_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "natural_pdf", None)
    with pytest.raises(natural_pdf.NaturalPdfUnavailable):
        natural_pdf.extract_pdf_tables(_request(tmp_path))


def test_provider_exception_raises_extraction_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_natural_pdf(
        monkeypatch,
        result=None,
        raises=ValueError("flow blew up"),
    )
    with pytest.raises(natural_pdf.NaturalPdfExtractionError):
        natural_pdf.extract_pdf_tables(_request(tmp_path))

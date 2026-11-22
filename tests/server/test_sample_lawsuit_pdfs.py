"""Tests for authentic court-filing PDFs and the upgraded Foreign registry sample sheet."""

from __future__ import annotations

import csv
import io

import pypdf
import pypdfium2 as pdfium

from frisket.sample_content.lawsuits import SAMPLE_LAWSUITS, lawsuit_pdf_bytes
from frisket.server.services.sample_project import (
    SAMPLE_FOREIGN_REGISTRY_ROWS,
    SAMPLE_FOREIGN_REGISTRY_SHEET_NAME,
    SAMPLE_MULTILINGUAL_SHEET_NAME,
    sample_foreign_registry_csv,
)


def test_court_filings_mix_four_born_digital_and_two_scanned_image_pdfs() -> None:
    digital = [lawsuit for lawsuit in SAMPLE_LAWSUITS if not lawsuit.scanned]
    scanned = [lawsuit for lawsuit in SAMPLE_LAWSUITS if lawsuit.scanned]

    assert len(SAMPLE_LAWSUITS) == 6
    assert len(digital) == 4
    assert len(scanned) == 2
    assert {lawsuit.case_number for lawsuit in scanned} == {
        "RV-CV-2026-00502",
        "RV-CV-2026-00689",
    }

    for lawsuit in digital:
        pdf_bytes = lawsuit_pdf_bytes(lawsuit)
        assert pdf_bytes.startswith(b"%PDF-1.4")
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == len(lawsuit.pages)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert "SUPERIOR COURT OF THE STATE OF FRANKLIN" in text
        assert "COUNTY OF RIVERTON" in text
        assert "FILED" in text
        assert lawsuit.case_number in text
        assert "Respectfully submitted," in text

    for lawsuit in scanned:
        pdf_bytes = lawsuit_pdf_bytes(lawsuit)
        assert pdf_bytes.startswith(b"%PDF-1.4")
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == len(lawsuit.pages)
        text = "\n".join((page.extract_text() or "").strip() for page in reader.pages)
        assert text.strip() == "", f"Scanned filing {lawsuit.filename} must have no embedded text layer"
        # Ensure rasterized image pages render cleanly via PDFium.
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            assert len(doc) == len(lawsuit.pages)
            image = doc[0].render(scale=0.5).to_pil()
            assert image.width > 100 and image.height > 100
        finally:
            doc.close()


def test_lawsuit_pdf_bytes_is_deterministic_and_cached() -> None:
    first = SAMPLE_LAWSUITS[0]
    scanned = next(lawsuit for lawsuit in SAMPLE_LAWSUITS if lawsuit.scanned)
    assert lawsuit_pdf_bytes(first) == lawsuit_pdf_bytes(first)
    assert lawsuit_pdf_bytes(scanned) == lawsuit_pdf_bytes(scanned)
    info = lawsuit_pdf_bytes.cache_info()
    assert info.hits >= 2


def test_foreign_registry_has_twelve_rows_across_three_entities_and_foreign_notes() -> None:
    assert SAMPLE_FOREIGN_REGISTRY_SHEET_NAME == "Foreign registry"
    assert SAMPLE_MULTILINGUAL_SHEET_NAME == SAMPLE_FOREIGN_REGISTRY_SHEET_NAME
    rows = list(csv.DictReader(io.StringIO(sample_foreign_registry_csv())))
    assert len(rows) == SAMPLE_FOREIGN_REGISTRY_ROWS == 12
    assert set(rows[0].keys()) == {"record_id", "name_as_written", "source", "record_note"}
    names = {row["name_as_written"] for row in rows}
    # Covers Cyrillic, Arabic, Greek, Katakana, and accented/cased Latin across 3 entities.
    assert "Александр Петров" in names
    assert "ألكسندر بيتروف" in names
    assert "アレクサンドル・ペトロフ" in names
    assert "Елена Морозова" in names
    assert "Έλενα Μορόζοβα" in names
    assert "Харбор Маритайм Холдингс" in names
    assert "Harbor Maritime Holdings" in names
    # Notes are genuinely foreign-language text (Russian, French, Spanish, German) for Translate.
    notes = " ".join(row["record_note"] for row in rows)
    assert "Директор" in notes
    assert "Consignataire" in notes
    assert "Representante" in notes
    assert "Geschäftsführer" in notes

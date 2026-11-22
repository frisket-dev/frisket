from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader

from frisket.sample_content.lawsuits import SAMPLE_LAWSUITS, lawsuit_pdf_bytes


def test_sample_lawsuit_pdfs_are_searchable_and_contain_ground_truth() -> None:
    assert SAMPLE_LAWSUITS
    digital_count = 0
    scanned_count = 0
    for lawsuit in SAMPLE_LAWSUITS:
        reader = PdfReader(BytesIO(lawsuit_pdf_bytes(lawsuit)))
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)

        assert len(reader.pages) == len(lawsuit.pages)
        if lawsuit.scanned:
            scanned_count += 1
            assert extracted.strip() == ""
        else:
            digital_count += 1
            assert lawsuit.case_number in extracted
            assert lawsuit.plaintiff.split()[0] in extracted
            assert lawsuit.defendant.split()[0] in extracted
    assert digital_count == 4
    assert scanned_count == 2

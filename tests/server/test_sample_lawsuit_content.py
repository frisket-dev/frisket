from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader

from frisket.sample_content.lawsuits import SAMPLE_LAWSUITS, lawsuit_pdf_bytes


def test_sample_lawsuit_pdfs_are_searchable_and_contain_ground_truth() -> None:
    for lawsuit in SAMPLE_LAWSUITS:
        reader = PdfReader(BytesIO(lawsuit_pdf_bytes(lawsuit)))
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)

        assert len(reader.pages) == len(lawsuit.pages)
        assert lawsuit.case_number in extracted
        assert lawsuit.plaintiff.split()[0] in extracted
        assert lawsuit.defendant.split()[0] in extracted

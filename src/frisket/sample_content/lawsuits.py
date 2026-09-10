"""Small deterministic lawsuit PDFs for the Riverton document walkthrough."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class SampleLawsuit:
    filename: str
    case_number: str
    court: str
    filed_at: str
    plaintiff: str
    defendant: str
    document_type: str
    status: str
    pages: tuple[str, ...]


SAMPLE_LAWSUITS: tuple[SampleLawsuit, ...] = (
    SampleLawsuit(
        filename="RV-CV-2026-00418_alvarez-v-northstar_complaint.pdf",
        case_number="RV-CV-2026-00418",
        court="Riverton County Superior Court",
        filed_at="2026-01-29",
        plaintiff="Daniel Alvarez",
        defendant="Northstar Civil LLC",
        document_type="Complaint",
        status="Answer due 2026-03-02",
        pages=(
            """COMPLAINT AND DEMAND FOR JURY TRIAL

Daniel Alvarez, Plaintiff, v. Northstar Civil LLC, Defendant.
Case No. RV-CV-2026-00418. Filed January 29, 2026.

Plaintiff Daniel Alvarez alleges that Northstar Civil LLC failed to pay wages and retaliated after he reported unsafe masonry work at the Riverton Transit Authority depot. Alvarez worked as a site foreman from September 2025 through January 8, 2026. Northstar was performing city work under contracts CTR-2025-118 and CTR-2026-014.

Alvarez says he warned project manager Caleb Ward on December 19 that an unsupported parapet could fall into the east maintenance bay. He alleges that Ward ordered the crew to continue demolition before an engineer arrived. The complaint does not allege that any Transit Authority employee gave that instruction.""",
            """FACTUAL ALLEGATIONS

On December 22, Alvarez sent photographs to Northstar safety director Michelle Dorsey. Three days later, Northstar removed him from the depot schedule. Payroll records attached as Exhibit A list 84 hours of unpaid overtime between October 6 and December 14.

Northstar told Alvarez in a January 5 letter that he was reassigned because his certification had expired. Alvarez alleges the certification remained valid through May 2026 and attaches a copy as Exhibit B.

COUNT I - Unpaid wages. Plaintiff seeks $38,420 in wages, statutory penalties, and attorney fees.

COUNT II - Retaliation. Plaintiff seeks reinstatement or front pay, compensatory damages, and punitive damages. The total demand is not less than $845,000.

Plaintiff requests a jury trial. Counsel: Simone Park, Park and Iqbal LLP, 44 Monroe Street, Riverton.""",
        ),
    ),
    SampleLawsuit(
        filename="RV-CV-2026-00502_water-alliance_records-petition.pdf",
        case_number="RV-CV-2026-00502",
        court="Riverton County Superior Court",
        filed_at="2026-02-06",
        plaintiff="Riverton Water Alliance",
        defendant="Riverton Water Authority",
        document_type="Verified petition",
        status="Hearing set 2026-03-18",
        pages=(
            """VERIFIED PETITION FOR DISCLOSURE OF PUBLIC RECORDS

Riverton Water Alliance v. Riverton Water Authority
Case No. RV-CV-2026-00502. Filed February 6, 2026.

The Alliance requested scoring rules, validation reports, error logs, and staff instructions related to contract CTR-2026-021 with Harbor Analytics. The Authority produced the signed contract and 312 pages of email but withheld the scoring rules as a trade secret.

Petitioner alleges that the rules determine whether household lead-test results receive immediate manual review and therefore operate as public policy rather than merely vendor software.""",
            """REQUESTS AND RESPONSE

The records request was submitted November 25, 2025. Records officer Hannah Price acknowledged it on December 1 and estimated production by December 22. The Authority produced its first batch on January 9, 2026.

On January 16, deputy director Evan Bell invoked the trade-secret exemption for 41 pages supplied by Harbor Analytics. Harbor submitted a declaration from chief scientist Nora Vale stating that disclosure would reveal proprietary matching weights.

The Alliance asks the court to review the withheld records privately, order disclosure of any instructions used by public employees, and award reasonable attorney fees. It does not seek customer names, addresses, or individual laboratory results.

Verified by Maya Okafor, executive director of Riverton Water Alliance, under penalty of perjury.""",
        ),
    ),
    SampleLawsuit(
        filename="RV-CV-2026-00611_meridian-v-riverton_injunction-motion.pdf",
        case_number="RV-CV-2026-00611",
        court="Riverton County Superior Court",
        filed_at="2026-02-24",
        plaintiff="Meridian Food Services",
        defendant="City of Riverton",
        document_type="Motion for preliminary injunction",
        status="Opposition filed",
        pages=(
            """MOTION FOR PRELIMINARY INJUNCTION

Meridian Food Services v. City of Riverton
Case No. RV-CV-2026-00611. Filed February 24, 2026.

Meridian asks the court to prevent Riverton from withholding $214,600 under shelter meal contract CTR-2026-033. The city says January invoices included 3,120 breakfasts that delivery logs do not support. Meridian says the meals were prepared and offered at a temporary kitchen during a two-day closure.

The company argues that immediate withholding would prevent it from meeting March payroll for 46 kitchen and delivery employees.""",
            """ARGUMENT

Meridian contends that the contract defines a delivered meal as one made available at an approved service location. It cites a January 12 email in which Human Services supervisor Dana Wu approved the temporary kitchen at 18 Harbor Avenue.

The city responds that residents were not transported to Harbor Avenue and that only 406 meals were collected there. Its opposition attaches shelter sign-in sheets and a declaration from commissioner Andre Cole.

Meridian requests release of the disputed payment into escrow pending arbitration. The company also seeks costs and any further relief the court considers just. A hearing is scheduled for March 6 before Judge Naomi Vega.""",
        ),
    ),
    SampleLawsuit(
        filename="RV-CV-2026-00689_patel-v-cedar-bridge_complaint.pdf",
        case_number="RV-CV-2026-00689",
        court="Riverton County Superior Court",
        filed_at="2026-03-03",
        plaintiff="Leena Patel",
        defendant="Cedar Bridge Security Inc.",
        document_type="Employment complaint",
        status="Service pending",
        pages=(
            """EMPLOYMENT DISCRIMINATION COMPLAINT

Leena Patel v. Cedar Bridge Security Inc.
Case No. RV-CV-2026-00689. Filed March 3, 2026.

Leena Patel worked for Cedar Bridge Security as a scheduling coordinator on the Riverton school account, contract CTR-2025-077. She alleges the company terminated her after she complained that supervisors assigned fewer shifts to guards who requested religious accommodations.

Patel says she raised the issue with operations director Grant Mercer on January 7 and was dismissed on January 20 for alleged mishandling of attendance records.""",
            """CLAIMS FOR RELIEF

Count I alleges religious discrimination under the Riverton Human Rights Act. Count II alleges retaliation. Count III alleges failure to preserve scheduling records after receiving notice of an administrative complaint.

Patel seeks back pay, lost benefits, compensatory damages, punitive damages, and an order requiring Cedar Bridge to preserve shift assignments, supervisor messages, and access logs. No specific dollar amount is stated.

The complaint references staffing reports submitted to the School District but does not name the district as a defendant. Counsel: Alice Mendez, Workers Justice Center.""",
        ),
    ),
    SampleLawsuit(
        filename="RV-CV-2026-00725_eastbank_change-order_subpoena-motion.pdf",
        case_number="RV-CV-2026-00725",
        court="Riverton County Superior Court",
        filed_at="2026-03-12",
        plaintiff="Eastbank Infrastructure",
        defendant="City of Riverton",
        document_type="Motion to quash subpoena",
        status="Decision reserved",
        pages=(
            """MOTION TO QUASH THIRD-PARTY SUBPOENA

Eastbank Infrastructure v. City of Riverton
Case No. RV-CV-2026-00725. Filed March 12, 2026.

Eastbank moves to quash a subpoena issued to engineering consultant Larkspur Group during a dispute over bridge contract CTR-2025-162. Eastbank seeks a $925,000 change order for corroded expansion joints and a 63-day extension.

The city subpoena requests every photograph, field note, draft report, and message concerning the Monroe Avenue bridge from January 2023 through bid opening.""",
            """The contractor argues that the request is overbroad and includes privileged material prepared after the dispute began. Riverton responds that six photographs taken before bidding may show the same corrosion Eastbank later called an unforeseen condition.

At a March 11 conference, Judge Samuel Ortiz directed Larkspur Group to preserve the records and proposed a private review of documents created after February 22, 2026.

Eastbank asks the court to limit production to pre-bid inspection material and to require the city to pay the consultant's retrieval costs. The city estimates those costs at $7,800; Eastbank estimates $31,000.""",
        ),
    ),
    SampleLawsuit(
        filename="RV-CV-2026-00804_chen-v-bright-harbor_class-action.pdf",
        case_number="RV-CV-2026-00804",
        court="Riverton County Superior Court",
        filed_at="2026-03-26",
        plaintiff="Mei Chen and proposed library patron class",
        defendant="Bright Harbor Systems and Riverton Public Library",
        document_type="Class-action complaint",
        status="New filing",
        pages=(
            """CLASS-ACTION COMPLAINT

Mei Chen, individually and on behalf of a proposed class, v. Bright Harbor Systems and Riverton Public Library
Case No. RV-CV-2026-00804. Filed March 26, 2026.

The complaint challenges collection of patron request data during software pilot CTR-2026-041. Plaintiff Mei Chen submitted a request to digitize neighborhood association newsletters. She alleges the request text, library card identifier, and device address were transmitted to Bright Harbor without adequate notice.

The library says the pilot used randomly generated patron identifiers and that no reading history was supplied.""",
            """ALLEGED DATA PRACTICES

Contract section 7.4 permits Bright Harbor to retain de-identified usage data for product improvement. The complaint alleges that device addresses and precise timestamps can be combined with other records to identify patrons.

An attached privacy notice says request text may be processed by a service provider but does not name Bright Harbor. Plaintiff alleges she first learned the vendor's identity from a February ethics filing.

The proposed class includes Riverton library patrons whose digitization requests were processed after February 10, 2026. Plaintiff asserts claims for breach of confidence, unjust enrichment, and violation of the Riverton Consumer Data Act.""",
            """RELIEF REQUESTED

Plaintiff seeks a declaration describing which fields were transmitted, deletion of data not required for library operations, improved notice, statutory damages where available, and attorney fees.

The complaint does not allege that Bright Harbor sold patron data or that any request was made public. It asks for an independent technical audit to determine whether retained records can reasonably be linked back to individuals.

Counsel: Priya Nwosu and Benjamin Lee, Digital Rights Clinic, 8 College Square, Riverton.""",
        ),
    ),
)


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page_lines(text: str) -> list[str]:
    lines: list[str] = []
    for paragraph in text.strip().splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(paragraph.strip(), width=88))
    return lines[:52]


def lawsuit_pdf_bytes(lawsuit: SampleLawsuit) -> bytes:
    """Create a small searchable multi-page PDF using only the standard library."""

    page_count = len(lawsuit.pages)
    page_object_ids = [3 + index * 2 for index in range(page_count)]
    content_object_ids = [object_id + 1 for object_id in page_object_ids]
    font_object_id = 3 + page_count * 2
    objects: dict[int, bytes] = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: (
            f"<</Type/Pages/Kids[{' '.join(f'{item} 0 R' for item in page_object_ids)}]"
            f"/Count {page_count}>>"
        ).encode(),
        font_object_id: b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    }
    for index, page_text in enumerate(lawsuit.pages):
        page_id = page_object_ids[index]
        content_id = content_object_ids[index]
        commands = ["BT", "/F1 10 Tf", "54 738 Td", "12 TL"]
        for line in _page_lines(page_text):
            commands.append(f"({_pdf_escape(line)}) Tj")
            commands.append("T*")
        commands.append(f"(Page {index + 1} of {page_count}) Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("ascii")
        objects[page_id] = (
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents {content_id} 0 R"
            f"/Resources<</Font<</F1 {font_object_id} 0 R>>>>>>"
        ).encode()
        objects[content_id] = (
            f"<</Length {len(stream)}>>stream\n".encode() + stream + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_id in range(1, font_object_id + 1):
        offsets.append(len(output))
        output += f"{object_id} 0 obj\n".encode()
        output += objects[object_id]
        output += b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {font_object_id + 1}\n".encode()
    output += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer<</Size {font_object_id + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)

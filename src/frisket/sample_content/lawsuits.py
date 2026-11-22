"""Deterministic US court-pleading lawsuit PDFs for the Riverton sample project.

Four filings are generated as born-digital vector pleadings with embedded Type 1
typography (Times-Roman, Times-Bold, Times-Italic, Courier-Bold) and pleading
paper rules. Two filings (``scanned=True``) are rasterized from the same
pleading layout into subtle skewed JPEG page images with zero embedded text
layer so users experience the real difference between To Markdown and OCR.
"""

from __future__ import annotations

import functools
import io
import math
import random
import textwrap
from dataclasses import dataclass
from datetime import date

import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageDraw

_PAGE_WIDTH = 612
_PAGE_HEIGHT = 792
_LEFT_DOUBLE_RULE_X1 = 68.0
_LEFT_DOUBLE_RULE_X2 = 72.0
_RIGHT_RULE_X = 558.0
_BODY_LEFT_X = 88.0
_BODY_RIGHT_X = 546.0
_CAPTION_DIVIDER_X = 332.0
_CAPTION_RIGHT_X = 348.0
_LINE_NUMBER_X = 42.0
_SCAN_DPI = 150


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
    scanned: bool = False
    document_title: str = ""
    plaintiff_role: str = "Plaintiff"
    defendant_role: str = "Defendant"
    counsel_name: str = ""
    counsel_firm: str = ""
    counsel_address: str = ""


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
        document_title="COMPLAINT AND DEMAND FOR JURY TRIAL",
        counsel_name="Simone Park",
        counsel_firm="Park & Iqbal LLP",
        counsel_address="44 Monroe Street, Suite 400, Riverton, FR 04102",
        pages=(
            """I. NATURE OF ACTION

1. Plaintiff Daniel Alvarez brings this civil action against Defendant Northstar Civil LLC for unpaid overtime wages and unlawful workplace retaliation arising from municipal transit construction performed under City of Riverton contracts CTR-2025-118 and CTR-2026-014.

2. Plaintiff was employed by Defendant as a site foreman at the Riverton Transit Authority east maintenance depot from September 2025 through January 8, 2026. During that period, Plaintiff regularly worked in excess of forty hours per week repairing structural roof and masonry assemblies.

II. FACTUAL ALLEGATIONS

3. On December 19, 2025, Plaintiff warned Northstar project manager Caleb Ward that an unsupported exterior parapet above the east maintenance bay posed an imminent collapse hazard to crew members working below. Plaintiff requested that demolition pause until a structural engineer inspected the wall.

4. Plaintiff alleges that Mr. Ward directed the crew to continue demolition before an engineer arrived. The Complaint does not allege that any employee of the Riverton Transit Authority issued that directive.""",
            """5. On December 22, 2025, Plaintiff transmitted site photographs documenting the unsupported masonry to Northstar safety director Michelle Dorsey. Three days later, Defendant removed Plaintiff from the active depot work schedule.

6. Payroll ledgers attached hereto as Exhibit A document 84 hours of unpaid overtime accrued by Plaintiff between October 6, 2025 and December 14, 2025.

7. In a letter dated January 5, 2026, Defendant asserted that Plaintiff was reassigned because his crane and rigging certification had lapsed. Plaintiff alleges that his certification remained valid through May 2026 and attaches a true copy thereof as Exhibit B.

III. CLAIMS FOR RELIEF

8. COUNT I (Unpaid Overtime Wages): Defendant willfully failed to compensate Plaintiff for overtime hours worked, in violation of the Franklin Wage Payment Act. Plaintiff seeks $38,420 in unpaid wages, statutory liquidated penalties, and reasonable attorney fees.

9. COUNT II (Unlawful Retaliation): Defendant terminated and demoted Plaintiff in direct retaliation for reporting workplace safety violations. Plaintiff seeks reinstatement or front pay, compensatory damages, and punitive damages.

PRAYER FOR RELIEF

10. WHEREFORE, Plaintiff Daniel Alvarez demands trial by jury and requests judgment against Defendant Northstar Civil LLC in an amount not less than $845,000, together with statutory penalties, costs, and attorney fees.""",
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
        scanned=True,
        document_title="VERIFIED PETITION FOR DISCLOSURE OF PUBLIC RECORDS",
        plaintiff_role="Petitioner",
        defendant_role="Respondent",
        counsel_name="Maya Okafor",
        counsel_firm="Riverton Open Government Project",
        counsel_address="112 Canal Street, Riverton, FR 04104",
        pages=(
            """I. NATURE OF ACTION

1. Petitioner Riverton Water Alliance petitions this Court under the Franklin Public Records Act for an order compelling Respondent Riverton Water Authority to disclose scoring rules, validation reports, error logs, and staff instructions created or used under municipal contract CTR-2026-021 with Harbor Analytics.

2. Respondent produced the executed contract and 312 pages of administrative correspondence but withheld 41 pages describing automated lead-test matching rules as an alleged vendor trade secret.

II. FACTUAL ALLEGATIONS

3. Petitioner alleges that the withheld scoring rules directly determine whether household drinking-water lead test results receive immediate manual follow-up review or are deferred, and therefore operate as binding public-health policy rather than confidential commercial software.

4. Petitioner submitted its written public records request on November 25, 2025. Records officer Hannah Price acknowledged receipt on December 1, 2025 and estimated production by December 22, 2025. Respondent produced its initial batch on January 9, 2026.""",
            """5. On January 16, 2026, deputy director Evan Bell invoked the statutory trade-secret exemption to withhold 41 pages supplied by Harbor Analytics. Harbor submitted a supporting declaration from chief scientist Nora Vale asserting that disclosure would reveal proprietary matching weights.

III. CLAIMS FOR RELIEF

6. COUNT I (Improper Withholding of Public Records): Instructions governing how public employees prioritize household lead inspections are public records subject to mandatory disclosure. Trade-secret protections cannot shield agency decision thresholds from public inspection.

PRAYER FOR RELIEF

7. WHEREFORE, Petitioner Riverton Water Alliance respectfully asks the Court to conduct an in camera review of the 41 withheld pages, order disclosure of all scoring instructions used by public employees, and award reasonable attorney fees and litigation costs. Petitioner does not seek customer names, street addresses, or individual laboratory results.

8. Verified under penalty of perjury by Maya Okafor, Executive Director of Riverton Water Alliance.""",
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
        document_title="MOTION FOR PRELIMINARY INJUNCTION",
        counsel_name="David Halvorsen",
        counsel_firm="Halvorsen & Cho PC",
        counsel_address="205 Civic Center Plaza, Riverton, FR 04101",
        pages=(
            """I. NATURE OF MOTION

1. Plaintiff Meridian Food Services moves for a preliminary injunction restraining Defendant City of Riverton from withholding $214,600 in earned contract payments under emergency shelter meal agreement CTR-2026-033.

2. The City contends that January 2026 invoices included 3,120 breakfasts unsupported by shelter delivery logs. Meridian states that those meals were prepared and made available at an approved temporary kitchen during a two-day main kitchen closure.

II. FACTUAL ALLEGATIONS

3. Immediate withholding of $214,600 would cause irreparable harm by preventing Meridian from meeting March 2026 payroll obligations for 46 kitchen and delivery employees who staff Riverton's emergency shelter meal program.

4. Meridian contends that Section 4.2 of contract CTR-2026-033 defines a delivered meal as one prepared and made available at an approved service location. Meridian cites a January 12, 2026 email in which Human Services supervisor Dana Wu approved temporary service at 18 Harbor Avenue.""",
            """5. In its opposition, the City responds that shelter residents were not transported to 18 Harbor Avenue during the closure and that only 406 breakfasts were collected at that site. The City attaches shelter sign-in sheets and a declaration from Human Services Commissioner Andre Cole.

III. ARGUMENT FOR EQUITABLE RELIEF

6. The contractual definition of service availability presents a substantial question on the merits that should be resolved through the contract's arbitration clause rather than unilateral payment withholding.

7. Placing the disputed $214,600 into court-supervised escrow protects municipal funds while preserving Meridian's workforce pending expedited arbitration.

PRAYER FOR RELIEF

8. WHEREFORE, Plaintiff Meridian Food Services requests an order directing release of the disputed $214,600 payment into escrow pending arbitration, together with costs and such further relief as the Court deems just. Hearing is scheduled for March 6, 2026 before the Honorable Naomi Vega.""",
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
        scanned=True,
        document_title="EMPLOYMENT DISCRIMINATION COMPLAINT",
        counsel_name="Alice Mendez",
        counsel_firm="Workers Justice Center",
        counsel_address="78 Market Street, Suite 210, Riverton, FR 04103",
        pages=(
            """I. NATURE OF ACTION

1. Plaintiff Leena Patel brings this employment discrimination and retaliation action against Defendant Cedar Bridge Security Inc. arising out of her work as a scheduling coordinator on the Riverton public schools security contract, CTR-2025-077.

2. Plaintiff alleges that Defendant wrongfully terminated her employment after she reported that field supervisors systematically reduced shift assignments for security guards who requested religious accommodations.

II. FACTUAL ALLEGATIONS

3. On January 7, 2026, Plaintiff escalated written scheduling comparisons to operations director Grant Mercer showing that guards requesting Friday or Saturday religious accommodations lost an average of fourteen paid hours per pay period.

4. On January 20, 2026, thirteen days after her internal complaint, Defendant dismissed Plaintiff on the pretextual ground that she had mishandled routine attendance logs.""",
            """5. The Complaint references monthly staffing compliance reports submitted by Defendant to the Riverton School District under CTR-2025-077, but does not name the School District as a defendant in this action.

III. CLAIMS FOR RELIEF

6. COUNT I (Religious Discrimination): Defendant subjected employees to adverse scheduling practices based on requests for religious accommodation, in violation of the Riverton Human Rights Act.

7. COUNT II (Unlawful Retaliation): Defendant terminated Plaintiff because she opposed and reported discriminatory shift reductions.

8. COUNT III (Failure to Preserve Records): Defendant failed to preserve shift assignment logs and supervisor messages after receiving formal notice of an administrative complaint.

PRAYER FOR RELIEF

9. WHEREFORE, Plaintiff Leena Patel seeks back pay, lost employment benefits, compensatory damages, punitive damages, and an order requiring Cedar Bridge Security Inc. to preserve all shift assignments, supervisor messages, and building access logs. No specific dollar figure is pleaded at this stage.""",
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
        document_title="MOTION TO QUASH THIRD-PARTY SUBPOENA",
        counsel_name="Marcus Delgado",
        counsel_firm="Delgado & Vance LLP",
        counsel_address="310 Riverfront Way, Riverton, FR 04105",
        pages=(
            """I. NATURE OF MOTION

1. Plaintiff Eastbank Infrastructure moves pursuant to Civil Rule 45 to quash or narrow a third-party subpoena served by Defendant City of Riverton on engineering consultant Larkspur Group during a contract dispute over Monroe Avenue bridge rehabilitation contract CTR-2025-162.

2. In the underlying dispute, Eastbank seeks a $925,000 change order and a 63-day schedule extension for severe corrosion discovered inside fourteen bridge expansion joints.

II. FACTUAL ALLEGATIONS

3. The City's subpoena demands every photograph, field notebook, draft memorandum, and electronic message concerning the Monroe Avenue bridge prepared or exchanged from January 2023 through bid opening and thereafter.

4. Eastbank argues that the subpoena is facially overbroad and sweeps in work-product and attorney-client privileged engineering analyses prepared after the change-order dispute began.""",
            """5. Defendant responds that six interior joint photographs captured by Larkspur Group prior to bidding may establish that the corrosion was documented before Eastbank submitted its bid and was not an unforeseen site condition.

6. At a March 11, 2026 discovery conference, the Honorable Samuel Ortiz directed Larkspur Group to preserve all project files and proposed in camera review of any records created after February 22, 2026.

III. RELIEF REQUESTED

7. Production should be limited strictly to non-privileged pre-bid inspection materials, and the City should bear the third-party consultant's reasonable document retrieval expenses. The City estimates retrieval costs at $7,800, whereas Eastbank estimates $31,000.

PRAYER FOR RELIEF

8. WHEREFORE, Plaintiff Eastbank Infrastructure respectfully requests an order quashing the subpoena as to post-dispute materials, limiting production to pre-bid inspection records, and shifting retrieval costs to Defendant City of Riverton.""",
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
        document_title="CLASS-ACTION COMPLAINT",
        counsel_name="Priya Nwosu & Benjamin Lee",
        counsel_firm="Digital Rights Clinic",
        counsel_address="8 College Square, Riverton, FR 04106",
        pages=(
            """I. NATURE OF ACTION

1. Plaintiff Mei Chen, individually and on behalf of a proposed class of similarly situated library patrons, brings this class-action complaint challenging the collection and retention of patron digitization request data during software pilot contract CTR-2026-041 between Bright Harbor Systems and the Riverton Public Library.

2. Plaintiff submitted an online request to digitize neighborhood association newsletters. She alleges that her request text, library card identifier, and device network address were transmitted to Bright Harbor without clear patron disclosure.

3. The Library maintains that the pilot assigned randomly generated patron identifiers and that no circulation or reading history was supplied to the vendor.""",
            """II. ALLEGED DATA PRACTICES

4. Section 7.4 of contract CTR-2026-041 authorizes Bright Harbor Systems to retain de-identified usage records for commercial product improvement. Plaintiff alleges that device network addresses combined with precise submission timestamps can reasonably be linked back to individual patrons.

5. The online privacy notice posted during the pilot stated that request text may be processed by a service provider, but omitted Bright Harbor's name. Plaintiff alleges she first learned the vendor's identity from a February 2026 municipal ethics filing.

III. CLASS ALLEGATIONS AND CLAIMS

6. The proposed class consists of all Riverton Public Library patrons whose digitization requests were processed after February 10, 2026. Plaintiff asserts claims for breach of confidence, unjust enrichment, and violations of the Riverton Consumer Data Act.""",
            """IV. PRAYER FOR RELIEF

7. Plaintiff does not allege that Bright Harbor sold patron records to third parties or published any digitization request publicly.

8. WHEREFORE, Plaintiff Mei Chen and the proposed class respectfully request: (a) a declaratory judgment identifying every data field transmitted to Bright Harbor under CTR-2026-041; (b) deletion of all retained patron data not strictly required for library operations; (c) an independent technical audit determining whether retained records can be re-identified; (d) revised patron privacy disclosures; and (e) statutory damages where available and reasonable attorney fees.""",
        ),
    ),
)


def _pdf_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .encode("latin-1", errors="replace")
        .decode("latin-1")
    )


def _human_date(iso_date: str) -> str:
    parsed = date.fromisoformat(iso_date)
    return parsed.strftime("%B %d, %Y").replace(" 0", " ")


def _stamp_date(iso_date: str) -> str:
    parsed = date.fromisoformat(iso_date)
    return parsed.strftime("%b %d %Y").upper()


def _centered_x(
    text: str, font_size: float, *, left: float, right: float, bold: bool = False
) -> float:
    avg_char = font_size * (0.52 if bold else 0.47)
    width = len(text) * avg_char
    return max(left, left + ((right - left) - width) / 2.0)


def _pleading_paper_chrome(
    lawsuit: SampleLawsuit, page_number: int, page_count: int
) -> list[str]:
    """Draw pleading paper margin rules, 1-28 line numbers, and page footer."""
    commands: list[str] = [
        # Left double vertical margin rule
        "q",
        "0.6 w",
        "0.45 0.48 0.54 RG",
        f"{_LEFT_DOUBLE_RULE_X1:.1f} 52 m {_LEFT_DOUBLE_RULE_X1:.1f} 754 l S",
        f"{_LEFT_DOUBLE_RULE_X2:.1f} 52 m {_LEFT_DOUBLE_RULE_X2:.1f} 754 l S",
        # Faint right margin rule
        "0.45 w",
        "0.78 0.80 0.84 RG",
        f"{_RIGHT_RULE_X:.1f} 52 m {_RIGHT_RULE_X:.1f} 754 l S",
        # Subtle footer separator rule
        "0.4 w",
        "0.74 0.76 0.80 RG",
        f"{_BODY_LEFT_X:.1f} 56 m {_BODY_RIGHT_X:.1f} 56 l S",
        "Q",
    ]
    # 1..28 pleading line numbers along the left margin
    commands.extend(["BT", "/F1 8.5 Tf", "0.56 0.59 0.64 rg"])
    top_y = 730.0
    step = 24.0
    for number in range(1, 29):
        y = top_y - (number - 1) * step
        label = f"{number:>2}"
        commands.append(f"1 0 0 1 {_LINE_NUMBER_X:.1f} {y:.1f} Tm ({label}) Tj")
    commands.append("ET")

    # Page footer
    footer_center = f"- {page_number} -"
    center_x = _centered_x(footer_center, 9.5, left=_BODY_LEFT_X, right=_BODY_RIGHT_X)
    commands.extend(
        [
            "BT",
            "0.28 0.31 0.36 rg",
            "/F1 8.5 Tf",
            f"1 0 0 1 {_BODY_LEFT_X:.1f} 42.0 Tm (Case No. {_pdf_escape(lawsuit.case_number)}) Tj",
            "/F1 9.5 Tf",
            f"1 0 0 1 {center_x:.1f} 42.0 Tm ({_pdf_escape(footer_center)}) Tj",
            "/F3 8.5 Tf",
            f"1 0 0 1 402.0 42.0 Tm ({_pdf_escape(lawsuit.document_type)} ({page_number}/{page_count})) Tj",
            "ET",
        ]
    )
    return commands


def _clerk_filed_stamp(lawsuit: SampleLawsuit) -> list[str]:
    """Draw a realistic tilted dark-crimson clerk FILED stamp in top-right."""
    angle = math.radians(-5.5)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    cx, cy = 486.0, 720.0
    box_w, box_h = 116.0, 48.0
    x0, y0 = -box_w / 2.0, -box_h / 2.0
    return [
        "q",
        f"{cos_a:.5f} {sin_a:.5f} {-sin_a:.5f} {cos_a:.5f} {cx:.1f} {cy:.1f} cm",
        # Subtle tinted paper fill behind stamp
        "0.99 0.96 0.96 rg",
        f"{x0:.1f} {y0:.1f} {box_w:.1f} {box_h:.1f} re f",
        # Outer and inner stamp borders
        "0.64 0.14 0.14 RG",
        "1.3 w",
        f"{x0:.1f} {y0:.1f} {box_w:.1f} {box_h:.1f} re S",
        "0.5 w",
        f"{x0 + 2.5:.1f} {y0 + 2.5:.1f} {box_w - 5.0:.1f} {box_h - 5.0:.1f} re S",
        # Stamp text in Courier-Bold
        "BT",
        "0.64 0.14 0.14 rg",
        "/F4 11 Tf",
        "1 0 0 1 -18.0 9.0 Tm (FILED) Tj",
        "/F4 6.3 Tf",
        "1 0 0 1 -47.0 -1.5 Tm (RIVERTON SUPERIOR COURT) Tj",
        "/F4 7.5 Tf",
        f"1 0 0 1 -26.5 -11.0 Tm ({_pdf_escape(_stamp_date(lawsuit.filed_at))}) Tj",
        "/F4 6.2 Tf",
        "1 0 0 1 -31.0 -19.5 Tm (BY: DEPUTY CLERK) Tj",
        "ET",
        "Q",
    ]


def _page_one_header_and_caption(lawsuit: SampleLawsuit) -> tuple[list[str], float]:
    """Render counsel block, clerk stamp, court title, and boxed caption table."""
    commands: list[str] = []
    # Top-left counsel identification block
    counsel_lines = [
        lawsuit.counsel_name or "Counsel of Record",
        lawsuit.counsel_firm or "Riverton Bar Association",
        lawsuit.counsel_address or "Riverton, State of Franklin",
        f"Attorney for {lawsuit.plaintiff_role}",
    ]
    commands.extend(["BT", "0.10 0.12 0.15 rg"])
    y = 742.0
    for idx, line in enumerate(counsel_lines):
        font = "/F2 9 Tf" if idx == 0 else ("/F3 8.5 Tf" if idx == 3 else "/F1 8.5 Tf")
        commands.append(font)
        commands.append(
            f"1 0 0 1 {_BODY_LEFT_X:.1f} {y:.1f} Tm ({_pdf_escape(line)}) Tj"
        )
        y -= 11.5
    commands.append("ET")

    # Top-right clerk FILED stamp
    commands.extend(_clerk_filed_stamp(lawsuit))

    # Centered court title
    court_line_1 = "SUPERIOR COURT OF THE STATE OF FRANKLIN"
    court_line_2 = "COUNTY OF RIVERTON"
    c1_x = _centered_x(
        court_line_1, 11.5, left=_BODY_LEFT_X, right=_BODY_RIGHT_X, bold=True
    )
    c2_x = _centered_x(
        court_line_2, 11.0, left=_BODY_LEFT_X, right=_BODY_RIGHT_X, bold=True
    )
    commands.extend(
        [
            "BT",
            "0.05 0.06 0.08 rg",
            "/F2 11.5 Tf",
            f"1 0 0 1 {c1_x:.1f} 670.0 Tm ({court_line_1}) Tj",
            "/F2 11 Tf",
            f"1 0 0 1 {c2_x:.1f} 654.0 Tm ({court_line_2}) Tj",
            "ET",
        ]
    )

    # Boxed caption layout
    caption_top = 636.0
    left_lines: list[tuple[str, str]] = []
    for part in textwrap.wrap(f"{lawsuit.plaintiff.upper()},", width=34):
        left_lines.append(("/F2 9.5 Tf", part))
    left_lines.append(("/F3 9.5 Tf", f"        {lawsuit.plaintiff_role},"))
    left_lines.append(("/F1 9.5 Tf", "    v."))
    for part in textwrap.wrap(f"{lawsuit.defendant.upper()},", width=34):
        left_lines.append(("/F2 9.5 Tf", part))
    left_lines.append(("/F3 9.5 Tf", f"        {lawsuit.defendant_role}."))

    doc_title = lawsuit.document_title or lawsuit.document_type.upper()
    right_lines: list[tuple[str, str]] = [
        ("/F2 9.8 Tf", f"Case No. {lawsuit.case_number}"),
        ("/F1 8.5 Tf", ""),
    ]
    for part in textwrap.wrap(doc_title, width=30):
        right_lines.append(("/F2 9.5 Tf", part))
    right_lines.append(("/F1 8.5 Tf", ""))
    right_lines.append(("/F1 9 Tf", f"Filed: {_human_date(lawsuit.filed_at)}"))
    right_lines.append(("/F3 8.8 Tf", f"Status: {lawsuit.status}"))

    row_count = max(len(left_lines), len(right_lines), 7)
    line_step = 14.0
    caption_bottom = caption_top - (row_count * line_step + 12.0)

    # Caption top and bottom horizontal lines on the party side
    commands.extend(
        [
            "q",
            "0.7 w",
            "0.22 0.25 0.30 RG",
            f"{_BODY_LEFT_X:.1f} {caption_top:.1f} m {_CAPTION_DIVIDER_X:.1f} {caption_top:.1f} l S",
            f"{_BODY_LEFT_X:.1f} {caption_bottom:.1f} m {_CAPTION_DIVIDER_X:.1f} {caption_bottom:.1f} l S",
            "Q",
        ]
    )

    commands.extend(["BT", "0.08 0.10 0.12 rg"])
    for row_idx in range(row_count):
        row_y = caption_top - 14.0 - row_idx * line_step
        if row_idx < len(left_lines):
            font, text = left_lines[row_idx]
            commands.append(font)
            commands.append(
                f"1 0 0 1 {_BODY_LEFT_X + 2.0:.1f} {row_y:.1f} Tm ({_pdf_escape(text)}) Tj"
            )
        # Classic closing parenthesis column divider
        commands.append("/F1 11 Tf")
        commands.append(f"1 0 0 1 {_CAPTION_DIVIDER_X:.1f} {row_y:.1f} Tm (\\)) Tj")
        if row_idx < len(right_lines):
            font, text = right_lines[row_idx]
            if text:
                commands.append(font)
                commands.append(
                    f"1 0 0 1 {_CAPTION_RIGHT_X:.1f} {row_y:.1f} Tm ({_pdf_escape(text)}) Tj"
                )
    commands.append("ET")
    return commands, caption_bottom - 24.0


def _is_section_heading(paragraph: str) -> bool:
    stripped = paragraph.strip()
    if not stripped or len(stripped) > 68:
        return False
    return stripped == stripped.upper() and any(ch.isalpha() for ch in stripped)


def _body_commands(
    text: str, *, start_y: float, include_signature: bool, lawsuit: SampleLawsuit
) -> list[str]:
    commands: list[str] = ["BT", "0.08 0.10 0.12 rg"]
    y = start_y
    min_y = 76.0
    paragraphs = [
        block.strip() for block in text.strip().split("\n\n") if block.strip()
    ]
    for block in paragraphs:
        if y < min_y:
            break
        if _is_section_heading(block):
            y -= 4.0
            x = _centered_x(
                block, 10.2, left=_BODY_LEFT_X, right=_BODY_RIGHT_X, bold=True
            )
            commands.append("/F2 10.2 Tf")
            commands.append(f"1 0 0 1 {x:.1f} {y:.1f} Tm ({_pdf_escape(block)}) Tj")
            y -= 18.0
            continue
        wrapped = textwrap.wrap(block, width=84)
        commands.append("/F1 10.2 Tf")
        for line_idx, line in enumerate(wrapped):
            if y < min_y:
                break
            indent = 18.0 if line_idx == 0 else 0.0
            commands.append(
                f"1 0 0 1 {_BODY_LEFT_X + indent:.1f} {y:.1f} Tm ({_pdf_escape(line)}) Tj"
            )
            y -= 14.5
        y -= 8.0

    if include_signature and y >= 132.0:
        sig_x = 320.0
        y = min(y - 8.0, 210.0)
        counsel = lawsuit.counsel_name or "Counsel of Record"
        firm = lawsuit.counsel_firm or "Riverton Bar Association"
        commands.extend(
            [
                "/F1 9.8 Tf",
                f"1 0 0 1 {_BODY_LEFT_X:.1f} {y:.1f} Tm (Dated: {_pdf_escape(_human_date(lawsuit.filed_at))}) Tj",
                f"1 0 0 1 {sig_x:.1f} {y:.1f} Tm (Respectfully submitted,) Tj",
                "/F3 10.5 Tf",
                f"1 0 0 1 {sig_x:.1f} {y - 24.0:.1f} Tm (/s/ {_pdf_escape(counsel)}) Tj",
                "/F1 9.5 Tf",
                f"1 0 0 1 {sig_x:.1f} {y - 38.0:.1f} Tm ({_pdf_escape(counsel)}) Tj",
                f"1 0 0 1 {sig_x:.1f} {y - 50.0:.1f} Tm ({_pdf_escape(firm)}) Tj",
                "/F3 9.2 Tf",
                f"1 0 0 1 {sig_x:.1f} {y - 62.0:.1f} Tm (Attorney for {_pdf_escape(lawsuit.plaintiff_role)}) Tj",
            ]
        )
        commands.append("ET")
        # Crisp signature rule line
        commands.extend(
            [
                "q",
                "0.6 w",
                "0.25 0.28 0.32 RG",
                f"{sig_x:.1f} {y - 28.0:.1f} m {sig_x + 185.0:.1f} {y - 28.0:.1f} l S",
                "Q",
            ]
        )
        return commands

    commands.append("ET")
    return commands


def _assemble_pdf(objects: dict[int, bytes]) -> bytes:
    max_id = max(objects)
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, max_id + 1):
        offsets.append(len(output))
        output += f"{object_id} 0 obj\n".encode("ascii")
        output += objects[object_id]
        output += b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {max_id + 1}\n".encode("ascii")
    output += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        output += f"{offset:010d} 00000 n \n".encode("ascii")
    output += (
        f"trailer<</Size {max_id + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(output)


def _build_vector_pleading_pdf(lawsuit: SampleLawsuit) -> bytes:
    """Build an authentic multi-page born-digital US court pleading PDF."""
    page_count = len(lawsuit.pages)
    # 1: Catalog, 2: Pages, 3..6: Type 1 standard fonts
    f1_id, f2_id, f3_id, f4_id = 3, 4, 5, 6
    page_ids = [7 + idx * 2 for idx in range(page_count)]
    content_ids = [pid + 1 for pid in page_ids]
    objects: dict[int, bytes] = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: (
            f"<</Type/Pages/Kids[{' '.join(f'{pid} 0 R' for pid in page_ids)}]"
            f"/Count {page_count}>>"
        ).encode("ascii"),
        f1_id: b"<</Type/Font/Subtype/Type1/BaseFont/Times-Roman/Encoding/WinAnsiEncoding>>",
        f2_id: b"<</Type/Font/Subtype/Type1/BaseFont/Times-Bold/Encoding/WinAnsiEncoding>>",
        f3_id: b"<</Type/Font/Subtype/Type1/BaseFont/Times-Italic/Encoding/WinAnsiEncoding>>",
        f4_id: b"<</Type/Font/Subtype/Type1/BaseFont/Courier-Bold/Encoding/WinAnsiEncoding>>",
    }
    font_resources = (
        f"/Font<</F1 {f1_id} 0 R/F2 {f2_id} 0 R/F3 {f3_id} 0 R/F4 {f4_id} 0 R>>"
    )
    for idx, page_text in enumerate(lawsuit.pages):
        commands = _pleading_paper_chrome(lawsuit, idx + 1, page_count)
        if idx == 0:
            header_cmds, body_start_y = _page_one_header_and_caption(lawsuit)
            commands.extend(header_cmds)
        else:
            body_start_y = 724.0
        commands.extend(
            _body_commands(
                page_text,
                start_y=body_start_y,
                include_signature=(idx == page_count - 1),
                lawsuit=lawsuit,
            )
        )
        stream = "\n".join(commands).encode("latin-1")
        page_id = page_ids[idx]
        content_id = content_ids[idx]
        objects[page_id] = (
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}]"
            f"/Contents {content_id} 0 R/Resources<<{font_resources}>>>>"
        ).encode("ascii")
        objects[content_id] = (
            f"<</Length {len(stream)}>>stream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
    return _assemble_pdf(objects)


def _apply_scan_look(image: Image.Image, *, seed: str) -> Image.Image:
    """Apply a subtle, deterministic flatbed-scanner look to a rasterized page."""
    rng = random.Random(seed)
    rgb = image.convert("RGB")
    # Warm off-white archival paper tint
    paper = Image.new("RGB", rgb.size, (249, 246, 238))
    tinted = ImageChops.multiply(rgb, paper)
    # Subtle deterministic tilt (~0.4 to 0.6 degrees)
    angle = rng.choice((-1.0, 1.0)) * rng.uniform(0.42, 0.58)
    tilted = tinted.rotate(
        angle,
        resample=Image.Resampling.BICUBIC,
        fillcolor=(235, 231, 222),
    )
    # Faint outer scanner bezel border and subtle speckle
    draw = ImageDraw.Draw(tilted)
    width, height = tilted.size
    draw.rectangle((0, 0, width - 1, height - 1), outline=(214, 210, 200), width=4)
    draw.rectangle((4, 4, width - 5, height - 5), outline=(232, 228, 218), width=2)
    for _ in range(180):
        px = rng.randrange(8, width - 8)
        py = rng.randrange(8, height - 8)
        shade = rng.randrange(188, 224)
        draw.point((px, py), fill=(shade, shade - 2, shade - 5))
    return tilted


def _build_scanned_image_pdf(lawsuit: SampleLawsuit, vector_pdf: bytes) -> bytes:
    """Rasterize vector pleading pages to JPEG XObjects with zero embedded text."""
    jpeg_pages: list[tuple[bytes, int, int]] = []
    doc = pdfium.PdfDocument(vector_pdf)
    try:
        scale = _SCAN_DPI / 72.0
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            scanned = _apply_scan_look(
                pil_image, seed=f"{lawsuit.case_number}:page:{page_idx}"
            )
            buf = io.BytesIO()
            scanned.save(buf, format="JPEG", quality=78, optimize=True)
            jpeg_pages.append((buf.getvalue(), scanned.width, scanned.height))
    finally:
        doc.close()

    page_count = len(jpeg_pages)
    # Per page: Page obj, Content obj, Image XObject
    page_ids = [3 + idx * 3 for idx in range(page_count)]
    objects: dict[int, bytes] = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: (
            f"<</Type/Pages/Kids[{' '.join(f'{pid} 0 R' for pid in page_ids)}]"
            f"/Count {page_count}>>"
        ).encode("ascii"),
    }
    for idx, (jpeg_bytes, img_w, img_h) in enumerate(jpeg_pages):
        page_id = page_ids[idx]
        content_id = page_id + 1
        image_id = page_id + 2
        stream = f"q\n{_PAGE_WIDTH} 0 0 {_PAGE_HEIGHT} 0 0 cm\n/Im1 Do\nQ".encode(
            "ascii"
        )
        objects[page_id] = (
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}]"
            f"/Contents {content_id} 0 R/Resources<</XObject<</Im1 {image_id} 0 R>>>>>>"
        ).encode("ascii")
        objects[content_id] = (
            f"<</Length {len(stream)}>>stream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
        objects[image_id] = (
            f"<</Type/XObject/Subtype/Image/Width {img_w}/Height {img_h}"
            f"/ColorSpace/DeviceRGB/BitsPerComponent 8/Filter/DCTDecode"
            f"/Length {len(jpeg_bytes)}>>stream\n".encode("ascii")
            + jpeg_bytes
            + b"\nendstream"
        )
    return _assemble_pdf(objects)


@functools.lru_cache(maxsize=16)
def lawsuit_pdf_bytes(lawsuit: SampleLawsuit) -> bytes:
    """Create a deterministic court-filing PDF (born-digital or scanned image-only)."""
    vector_pdf = _build_vector_pleading_pdf(lawsuit)
    if lawsuit.scanned:
        return _build_scanned_image_pdf(lawsuit, vector_pdf)
    return vector_pdf

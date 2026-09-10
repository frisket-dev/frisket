"""Server-side content for the one-click sample investigation.

The sample is a small fictional reporting universe, not a finished analysis.
It deliberately ships raw stories and contract records without summary,
category, relevance, or word-count columns so the useful outputs are created by
the person following a walkthrough.
"""

from __future__ import annotations

import csv
import hashlib
import io
from contextlib import ExitStack
from typing import Any

from frisket.sample_content.audio import SAMPLE_COUNCIL_AUDIO, council_audio_bytes
from frisket.sample_content.lawsuits import SAMPLE_LAWSUITS, lawsuit_pdf_bytes
from frisket.sample_content.local_model_lab import LOCAL_MODEL_LAB_ROWS
from frisket.server.services.import_csv import ImportCsvUploadService
from frisket.server.services.import_files import ImportFilesUploadService
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.workspace import Workspace

SAMPLE_SHEET_NAME = "Dispatches"
SAMPLE_CONTRACTS_SHEET_NAME = "Contracts"
SAMPLE_DOCKET_SHEET_NAME = "Court docket"
SAMPLE_FILINGS_SHEET_NAME = "Court filings"
SAMPLE_COUNCIL_MEETINGS_SHEET_NAME = "Council meetings"
SAMPLE_COUNCIL_AUDIO_SHEET_NAME = "Council audio"
SAMPLE_MULTILINGUAL_SHEET_NAME = "Multilingual names"
SAMPLE_AGENCY_PAYMENTS_SHEET_NAME = "Agency payments"
SAMPLE_SMALL_MODEL_LAB_SHEET_NAME = "Small model lab"
# Kept in the response for the existing HTTP contract. There is intentionally
# no blank output column in the sheet.
SAMPLE_BLANK_COLUMN = ""

_SITE_ROOT = "https://frisket-dev.github.io/frisket-samples/riverton"


def _owned_upload(
    stack: ExitStack, *, filename: str, mime: str, data: bytes
) -> AdmittedUpload:
    source = stack.enter_context(io.BytesIO(data))
    return AdmittedUpload(
        filename=filename,
        mime=mime,
        source=source,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


_MULTILINGUAL_NAMES: tuple[dict[str, str], ...] = (
    {
        "record_id": "NAME-001",
        "name_as_written": "Александр Петров",
        "source": "Bulgarian company register",
        "record_note": "Director listed on the 2024 annual return",
    },
    {
        "record_id": "NAME-002",
        "name_as_written": "Александр П. Петров",
        "source": "Ukrainian court index",
        "record_note": "Respondent named with a middle initial",
    },
    {
        "record_id": "NAME-003",
        "name_as_written": "ألكسندر بيتروف",
        "source": "Arabic-language shipping notice",
        "record_note": "Consignee named in a translated port filing",
    },
    {
        "record_id": "NAME-004",
        "name_as_written": "Αλεξάντερ Πετρόφ",
        "source": "Greek procurement notice",
        "record_note": "Representative listed for the winning bidder",
    },
    {
        "record_id": "NAME-005",
        "name_as_written": "アレクサンドル・ペトロフ",
        "source": "Japanese corporate directory",
        "record_note": "Overseas officer rendered in katakana",
    },
    {
        "record_id": "NAME-006",
        "name_as_written": "Aleksandr Petrov",
        "source": "English-language sanctions notice",
        "record_note": "Alternate Latin transliteration",
    },
    {
        "record_id": "NAME-007",
        "name_as_written": "Alexander Petrov",
        "source": "Riverton vendor disclosure",
        "record_note": "Name supplied directly in English",
    },
    {
        "record_id": "NAME-008",
        "name_as_written": "ALEXANDER PETROV",
        "source": "Customs spreadsheet",
        "record_note": "Name exported in all caps",
    },
    {
        "record_id": "NAME-009",
        "name_as_written": "Alexánder Petróv",
        "source": "Spanish-language trade bulletin",
        "record_note": "Editor-added diacritics in a profile",
    },
    {
        "record_id": "NAME-010",
        "name_as_written": "Alexander  Petrov",
        "source": "Leaked address book",
        "record_note": "Duplicate whitespace from OCR cleanup",
    },
)

_AGENCY_PAYMENTS: tuple[dict[str, str], ...] = (
    {
        "payment_id": "PAY-2026-001",
        "agency": "Public Works Department",
        "vendor": "Eastbank Infrastructure",
        "paid_at": "2026-01-09",
        "amount_usd": "186400",
        "memo": "Monroe Avenue bridge mobilization",
    },
    {
        "payment_id": "PAY-2026-002",
        "agency": "Public Works Dept.",
        "vendor": "Stonebridge Materials",
        "paid_at": "2026-01-16",
        "amount_usd": "74250",
        "memo": "Winter asphalt stockpile",
    },
    {
        "payment_id": "PAY-2026-003",
        "agency": "PUBLIC WORKS DEPARTMENT",
        "vendor": "Signal Ridge Electric",
        "paid_at": "2026-01-28",
        "amount_usd": "39200",
        "memo": "Traffic signal cabinet repairs",
    },
    {
        "payment_id": "PAY-2026-004",
        "agency": " Public Works Department",
        "vendor": "Eastbank Infrastructure",
        "paid_at": "2026-02-06",
        "amount_usd": "248900",
        "memo": "Bridge joint fabrication",
    },
    {
        "payment_id": "PAY-2026-005",
        "agency": "Public Works Department ",
        "vendor": "Northbank Surveying",
        "paid_at": "2026-02-19",
        "amount_usd": "31800",
        "memo": "Drainage survey",
    },
    {
        "payment_id": "PAY-2026-006",
        "agency": "Public Works Department",
        "vendor": "Civic Salt Cooperative",
        "paid_at": "2026-03-03",
        "amount_usd": "121600",
        "memo": "Road salt delivery",
    },
    {
        "payment_id": "PAY-2026-007",
        "agency": "Water Authority",
        "vendor": "Harbor Analytics",
        "paid_at": "2026-01-12",
        "amount_usd": "95000",
        "memo": "Address matching milestone",
    },
    {
        "payment_id": "PAY-2026-008",
        "agency": "Riverton Water Authority",
        "vendor": "AquaLab Services",
        "paid_at": "2026-01-30",
        "amount_usd": "43700",
        "memo": "Lead sample analysis",
    },
    {
        "payment_id": "PAY-2026-009",
        "agency": "water authority",
        "vendor": "Harbor Analytics",
        "paid_at": "2026-02-15",
        "amount_usd": "112000",
        "memo": "Workflow design milestone",
    },
    {
        "payment_id": "PAY-2026-010",
        "agency": "Water Authority",
        "vendor": "MeterWorks LLC",
        "paid_at": "2026-03-05",
        "amount_usd": "58750",
        "memo": "Meter calibration services",
    },
    {
        "payment_id": "PAY-2026-011",
        "agency": "Transit Authority",
        "vendor": "Northstar Civil LLC",
        "paid_at": "2026-01-14",
        "amount_usd": "286000",
        "memo": "Depot stabilization advance",
    },
    {
        "payment_id": "PAY-2026-012",
        "agency": "Riverton Transit Authority",
        "vendor": "Metro Fleet Supply",
        "paid_at": "2026-02-02",
        "amount_usd": "68400",
        "memo": "Bus brake assemblies",
    },
    {
        "payment_id": "PAY-2026-013",
        "agency": "Transit authority",
        "vendor": "Northstar Civil LLC",
        "paid_at": "2026-02-21",
        "amount_usd": "341500",
        "memo": "Depot masonry work",
    },
    {
        "payment_id": "PAY-2026-014",
        "agency": "Transit Authority",
        "vendor": "Platform Safety Group",
        "paid_at": "2026-03-10",
        "amount_usd": "52900",
        "memo": "Platform edge inspection",
    },
    {
        "payment_id": "PAY-2026-015",
        "agency": "Parks Department",
        "vendor": "Greenline Civic Data",
        "paid_at": "2026-02-11",
        "amount_usd": "79600",
        "memo": "Tree inventory kickoff",
    },
    {
        "payment_id": "PAY-2026-016",
        "agency": "Parks Department",
        "vendor": "Riverton Nursery",
        "paid_at": "2026-03-18",
        "amount_usd": "44500",
        "memo": "Spring planting stock",
    },
)

_DISPATCHES: tuple[dict[str, str], ...] = (
    {
        "record_id": "RVN-2026-001",
        "headline": "Bus depot roof contract amended after second leak",
        "published_at": "2026-01-08T09:20:00Z",
        "source_kind": "news_story",
        "publisher": "Riverton Ledger",
        "story": (
            "Transit officials added $480,000 to contract CTR-2025-118 with "
            "Northstar Civil LLC after rain entered the east maintenance bay for "
            "the second time in four months. Agency director Mina Shah said the "
            "amendment covers drainage work that was not included in the original "
            "$1.1 million roof replacement."
        ),
    },
    {
        "record_id": "RVN-2026-002",
        "headline": "Audit committee questions emergency depot procurement",
        "published_at": "2026-01-12T18:05:00Z",
        "source_kind": "meeting_note",
        "publisher": "Civic Records Desk",
        "story": (
            "Members of Riverton's audit committee questioned why the Transit "
            "Authority used an emergency exception for CTR-2026-014, a $2.4 million "
            "depot stabilization contract awarded to Northstar Civil LLC. Chief "
            "financial officer Lena Ortiz told the committee that engineers found a "
            "risk of falling masonry on December 18 and that regular bidding would "
            "have delayed work until March.\n\nCommittee member Paul Nwosu pointed "
            "to a December 10 email in which purchasing staff had already asked "
            "Northstar for a price. Ortiz said the earlier request concerned a "
            "smaller inspection and was unrelated to the later emergency. Northstar "
            "president Caleb Ward said in a statement that the company did not know "
            "it would receive the stabilization work when it priced the inspection.\n\n"
            "The committee requested the engineering report, the earlier quote, and "
            "a written timeline before its February 3 meeting. It did not allege that "
            "the award violated city rules."
        ),
    },
    {
        "record_id": "RVN-2026-003",
        "headline": "What changed in the water authority's testing account",
        "published_at": "2026-01-19T07:30:00Z",
        "source_kind": "records_analysis",
        "publisher": "Riverton Ledger",
        "story": (
            "For six months, the Riverton Water Authority described CTR-2026-021 as "
            "a routine data-services purchase. Records released Friday show the "
            "$760,000 agreement with Harbor Analytics also pays the firm to design "
            "the rules that decide which household lead tests receive follow-up.\n\n"
            "The chronology begins in August, when laboratory manager Imani Brooks "
            "warned that 1,840 test results could not be matched to a service-line "
            "address. Brooks recommended hiring temporary clerks. In September, "
            "deputy director Evan Bell instead invited Harbor Analytics to demonstrate "
            "its matching software. The authority signed a letter of intent on "
            "October 4, although the board did not approve the contract until "
            "November 17.\n\nHarbor's proposal promised a confidence score for every "
            "address. An internal memo later instructed staff to defer follow-up when "
            "the score fell below 0.65. Two neighborhood clinics said they were never "
            "told that an automated score could delay a new test. Bell said the score "
            "only prioritizes manual review and never cancels one. Brooks, who left "
            "the authority in December, said reviewers were reassigned after the tool "
            "launched.\n\nThe contract identifies data matching as the main deliverable "
            "but also includes policy design, staff training, and a possible $190,000 "
            "extension. Board chair Sofia Alvarez said she had understood the work to "
            "be technical. She asked counsel to determine whether the broader scope "
            "should return for a public vote. Harbor Analytics said the authority, not "
            "the company, sets all public-health policy."
        ),
    },
    {
        "record_id": "RVN-2026-004",
        "headline": "Early-morning odor complaints logged near Atlas Plating",
        "published_at": "2026-01-20T11:12:00Z",
        "source_kind": "public_log",
        "publisher": "Riverton Environment Desk",
        "story": (
            "Three residents reported a sharp odor near Atlas Plating between 5:50 "
            "and 7:10 a.m. Tuesday. The city log says an inspector arrived after the "
            "odor had dispersed. No violation was issued, and the cause remains open."
        ),
    },
    {
        "record_id": "RVN-2026-005",
        "headline": "Council approves shelter meals deal after split vote",
        "published_at": "2026-01-22T22:40:00Z",
        "source_kind": "meeting_note",
        "publisher": "Civic Records Desk",
        "story": (
            "The City Council voted 5-4 to approve CTR-2026-033 with Meridian Food "
            "Services, which will supply meals to three emergency shelters for up to "
            "$1.85 million over two years. Council member June Park objected that the "
            "winning proposal budgeted 11 percent less for fresh produce than the "
            "other finalist. Human Services commissioner Andre Cole said Meridian's "
            "new local kitchen would reduce delivery failures. The agreement requires "
            "monthly temperature and nutrition reports, but those reports will not be "
            "posted automatically."
        ),
    },
    {
        "record_id": "RVN-2026-006",
        "headline": "Anonymous tip alleges shelter invoices list undelivered meals",
        "published_at": "2026-01-27T15:15:00Z",
        "source_kind": "tip",
        "publisher": "Riverton Ledger Tip Line",
        "story": (
            "An anonymous shelter employee says two January invoices under "
            "CTR-2026-033 billed the city for breakfasts that were not delivered "
            "during a kitchen closure. The tip included invoice numbers but no copies. "
            "Meridian Food Services and the Human Services Department have not yet "
            "responded. The allegation has not been independently verified."
        ),
    },
    {
        "record_id": "RVN-2026-007",
        "headline": "Former security subcontractor sues Cedar Bridge",
        "published_at": "2026-02-02T10:45:00Z",
        "source_kind": "court_report",
        "publisher": "Riverton Ledger",
        "story": (
            "A former subcontractor sued Cedar Bridge Security on Monday, alleging "
            "that the company withheld $146,000 for work at Riverton public schools. "
            "The complaint names city contract CTR-2025-077 but does not accuse the "
            "school district of wrongdoing. Cedar Bridge said the subcontractor missed "
            "staffing requirements and that the retained money covers penalties. A "
            "case-management conference is set for April 9 before Judge Naomi Vega."
        ),
    },
    {
        "record_id": "RVN-2026-008",
        "headline": "School board seeks records after guard vacancies",
        "published_at": "2026-02-05T20:02:00Z",
        "source_kind": "meeting_note",
        "publisher": "Civic Records Desk",
        "story": (
            "School board members ordered a review of CTR-2025-077 after principals "
            "reported 38 unfilled security shifts in January. Cedar Bridge Security "
            "was paid $312,400 for the month, according to the district's check "
            "register. Superintendent Malik Reed said payments are reconciled each "
            "quarter and may be reduced if vacancies are confirmed. Board member "
            "Theresa Kim asked for shift logs, invoices, and penalty calculations by "
            "the next public meeting."
        ),
    },
    {
        "record_id": "RVN-2026-009",
        "headline": "Ethics filing discloses family tie to library software vendor",
        "published_at": "2026-02-08T08:10:00Z",
        "source_kind": "records_brief",
        "publisher": "Riverton Ledger",
        "story": (
            "Library trustee Marta Singh disclosed that her brother works for Bright "
            "Harbor Systems, the vendor selected for software pilot CTR-2026-041. "
            "Minutes show Singh left the room before the vote. The $184,000 pilot was "
            "approved unanimously by the remaining trustees. City ethics counsel "
            "Ramon Ellis said the disclosure and recusal satisfy the conflict policy; "
            "he has not reviewed whether Singh participated in earlier demonstrations."
        ),
    },
    {
        "record_id": "RVN-2026-010",
        "headline": "Library pilot will rank patron requests for digitization",
        "published_at": "2026-02-10T13:25:00Z",
        "source_kind": "news_story",
        "publisher": "Riverton Ledger",
        "story": (
            "Under CTR-2026-041, Bright Harbor Systems will build a tool that groups "
            "and ranks requests to digitize photographs, newspapers, and oral-history "
            "tapes. Library director Celia Grant said staff will make every final "
            "decision. The contract lets the vendor retain de-identified usage data "
            "for product improvement, a provision the local history association wants "
            "clarified before uploads begin in May."
        ),
    },
    {
        "record_id": "RVN-2026-011",
        "headline": "Ambulance response extension carries new staffing targets",
        "published_at": "2026-02-14T06:50:00Z",
        "source_kind": "contract_brief",
        "publisher": "Riverton Health Desk",
        "story": (
            "Riverton extended ambulance contract CTR-2024-205 with Valley Response "
            "Cooperative through June 2027. The $6.2 million amendment requires two "
            "additional overnight crews and an eight-minute median response time in "
            "the north district. The previous agreement measured performance citywide. "
            "Health commissioner Asha Franklin said neighborhood-level figures will be "
            "published quarterly."
        ),
    },
    {
        "record_id": "RVN-2026-012",
        "headline": "Union grievance says ambulance breaks remain understaffed",
        "published_at": "2026-02-18T17:30:00Z",
        "source_kind": "labor_filing",
        "publisher": "Riverton Health Desk",
        "story": (
            "Paramedics Local 44 filed a grievance saying Valley Response Cooperative "
            "counts supervisors as available crew members while they are assigned to "
            "dispatch duties. The filing cites CTR-2024-205 and asks the city to audit "
            "three months of schedules. Valley Response said supervisors are licensed "
            "to answer calls and are counted only when an ambulance is available."
        ),
    },
    {
        "record_id": "RVN-2026-013",
        "headline": "Bridge change order follows discovery of corroded joints",
        "published_at": "2026-02-22T12:00:00Z",
        "source_kind": "news_story",
        "publisher": "Riverton Ledger",
        "story": (
            "The Public Works Department proposed a $925,000 change order to "
            "CTR-2025-162 after crews found corrosion inside 14 expansion joints on "
            "the Monroe Avenue bridge. Project manager Victor Chen said the condition "
            "could not be seen during surface inspections. The contractor, Eastbank "
            "Infrastructure, has requested a 63-day extension. A 2023 consultant's "
            "photograph appears to show rust near one of the affected joints, though "
            "the consultant's report rated it serviceable."
        ),
    },
    {
        "record_id": "RVN-2026-014",
        "headline": "Public works releases bridge inspection photographs",
        "published_at": "2026-02-24T16:35:00Z",
        "source_kind": "records_release",
        "publisher": "Civic Records Desk",
        "story": (
            "The city released 217 photographs and four inspection memos connected to "
            "the Monroe Avenue bridge project. File names indicate that six interior "
            "joints were photographed before bids were opened for CTR-2025-162. The "
            "records do not say whether Eastbank Infrastructure received those images. "
            "Public Works says it is searching archived procurement folders."
        ),
    },
    {
        "record_id": "RVN-2026-015",
        "headline": "Mayor announces neighborhood tree program",
        "published_at": "2026-03-01T14:00:00Z",
        "source_kind": "press_release",
        "publisher": "Office of Mayor Elena Ruiz",
        "story": (
            "Mayor Elena Ruiz announced a plan to plant 3,000 trees over four years, "
            "with the first work focused on the heat-vulnerable East Ward. The release "
            "does not identify a contractor or funding account. A detailed proposal is "
            "expected with the mayor's capital budget next month."
        ),
    },
    {
        "record_id": "RVN-2026-016",
        "headline": "Tree inventory award goes to firm that advised campaign",
        "published_at": "2026-03-04T09:55:00Z",
        "source_kind": "records_analysis",
        "publisher": "Riverton Ledger",
        "story": (
            "The Parks Department awarded CTR-2026-052, a $398,000 tree inventory, to "
            "Greenline Civic Data. State filings show Greenline founder Owen Price was "
            "paid $12,500 by Mayor Elena Ruiz's campaign for mapping advice in 2024. "
            "The mayor's office said Ruiz had no role in scoring the five bids. Parks "
            "procurement chair Felicia Moore said Greenline received the highest "
            "technical score but was not the lowest bidder. The scoring sheets list "
            "experience, data portability, and community engagement as the deciding "
            "factors. Price said the campaign work was disclosed in Greenline's bid."
        ),
    },
    {
        "record_id": "RVN-2026-017",
        "headline": "Correction: tree contract amount",
        "published_at": "2026-03-04T18:30:00Z",
        "source_kind": "correction",
        "publisher": "Riverton Ledger",
        "story": (
            "An earlier version of our report misstated the ceiling for CTR-2026-052. "
            "The contract is worth up to $398,000, not $389,000."
        ),
    },
    {
        "record_id": "RVN-2026-018",
        "headline": "Housing testimony asks council to delay Foundry rezoning",
        "published_at": "2026-03-07T21:05:00Z",
        "source_kind": "public_comment",
        "publisher": "Civic Records Desk",
        "story": (
            "Residents at a four-hour hearing asked the council to postpone the "
            "Foundry District rezoning until a traffic study includes weekend games at "
            "Riverton Stadium. Supporters said the proposed 640 apartments would add "
            "needed homes near two bus lines. The council closed public testimony but "
            "scheduled no vote."
        ),
    },
    {
        "record_id": "RVN-2026-019",
        "headline": "Finance office flags duplicate software invoice",
        "published_at": "2026-03-11T08:45:00Z",
        "source_kind": "internal_memo",
        "publisher": "Civic Records Desk",
        "story": (
            "A Finance Department memo says Bright Harbor Systems submitted two "
            "invoices with the same service dates under CTR-2026-041. One invoice was "
            "stopped before payment; the other, for $28,400, had already cleared. "
            "Library director Celia Grant called the duplicate a clerical mistake. The "
            "vendor said it has issued a credit and is reviewing its billing system."
        ),
    },
    {
        "record_id": "RVN-2026-020",
        "headline": "City cancels parking-sensor purchase before installation",
        "published_at": "2026-03-14T11:40:00Z",
        "source_kind": "contract_brief",
        "publisher": "Riverton Ledger",
        "story": (
            "The Transportation Department canceled CTR-2026-060 with Kerbside Labs "
            "after a pilot sensor failed the city's winter water-resistance test. No "
            "devices were installed and the city paid $22,000 for design work. "
            "Kerbside said its revised enclosure passed an independent test, but the "
            "result arrived after Riverton's cancellation deadline."
        ),
    },
    {
        "record_id": "RVN-2026-021",
        "headline": "Vendor protests cancellation of parking sensor deal",
        "published_at": "2026-03-16T15:05:00Z",
        "source_kind": "legal_letter",
        "publisher": "Civic Records Desk",
        "story": (
            "Kerbside Labs asked Riverton to reverse the cancellation of CTR-2026-060, "
            "arguing that the city's water test used a pressure setting not listed in "
            "the solicitation. Transportation purchasing officer Gabriel Stone said "
            "the setting reproduces snowplow spray and was discussed at a bidders' "
            "conference. The city has paused a replacement solicitation while its law "
            "department reviews the protest."
        ),
    },
    {
        "record_id": "RVN-2026-022",
        "headline": "How Riverton's emergency purchasing exception works",
        "published_at": "2026-03-19T07:00:00Z",
        "source_kind": "explainer",
        "publisher": "Riverton Ledger",
        "story": (
            "Riverton agencies may bypass ordinary bidding when delay threatens life, "
            "property, or an essential public service. The agency head must describe "
            "the emergency in writing, purchasing staff must document why the chosen "
            "price is reasonable, and the audit committee reviews awards above "
            "$250,000.\n\nThose rules are central to questions about CTR-2026-014, the "
            "Transit Authority's depot stabilization award. The written declaration "
            "cites falling masonry and winter weather. It does not mention the earlier "
            "inspection quote sought from Northstar Civil LLC. Procurement specialists "
            "interviewed by the Ledger disagreed on whether that omission matters: one "
            "said the earlier contact shows useful preparation, while another said it "
            "could narrow competition before an emergency is formally declared.\n\n"
            "Emergency status does not eliminate all competition. Riverton's manual "
            "asks agencies to seek three informal quotes when time permits. Transit "
            "officials contacted two companies; the second declined because its crews "
            "were committed through February. The authority has not identified a third "
            "company. Audit committee members have requested the complete contact log."
        ),
    },
    {
        "record_id": "RVN-2026-023",
        "headline": "Clinic coalition requests lead-test appeal process",
        "published_at": "2026-03-23T12:25:00Z",
        "source_kind": "open_letter",
        "publisher": "Riverton Health Coalition",
        "story": (
            "Seven neighborhood clinics asked the Water Authority to create an appeal "
            "process for addresses assigned low confidence by the system purchased "
            "under CTR-2026-021. Their letter says residents should be told when a "
            "matching score delays review and should be able to submit a lease or "
            "utility bill as proof. Deputy director Evan Bell said the authority will "
            "respond after its April board meeting."
        ),
    },
    {
        "record_id": "RVN-2026-024",
        "headline": "Weekend calendar: hearings, cleanups and a bridge closure",
        "published_at": "2026-03-27T16:00:00Z",
        "source_kind": "community_calendar",
        "publisher": "Riverton Ledger",
        "story": (
            "The Foundry rezoning hearing resumes Saturday at 10 a.m. East Ward "
            "volunteers meet at Carver Park at 9 a.m. for a creek cleanup. The Monroe "
            "Avenue bridge closes from midnight Friday until 5 a.m. Monday for joint "
            "repairs; buses 4 and 11 will use Harbor Street."
        ),
    },
)

_CONTRACTS: tuple[dict[str, str], ...] = (
    {
        "contract_id": "CTR-2024-205",
        "vendor": "Valley Response Cooperative",
        "agency": "Health Department",
        "award_date": "2024-06-18",
        "ceiling_usd": "6200000",
        "status": "extended",
        "description": "Emergency ambulance response and overnight crews",
    },
    {
        "contract_id": "CTR-2025-077",
        "vendor": "Cedar Bridge Security",
        "agency": "School District",
        "award_date": "2025-05-09",
        "ceiling_usd": "3740000",
        "status": "active",
        "description": "School security staffing",
    },
    {
        "contract_id": "CTR-2025-118",
        "vendor": "Northstar Civil LLC",
        "agency": "Transit Authority",
        "award_date": "2025-08-21",
        "ceiling_usd": "1580000",
        "status": "amended",
        "description": "Bus depot roof and drainage replacement",
    },
    {
        "contract_id": "CTR-2025-162",
        "vendor": "Eastbank Infrastructure",
        "agency": "Public Works",
        "award_date": "2025-11-03",
        "ceiling_usd": "8125000",
        "status": "change_order_pending",
        "description": "Monroe Avenue bridge rehabilitation",
    },
    {
        "contract_id": "CTR-2026-014",
        "vendor": "Northstar Civil LLC",
        "agency": "Transit Authority",
        "award_date": "2026-01-04",
        "ceiling_usd": "2400000",
        "status": "active",
        "description": "Emergency bus depot stabilization",
    },
    {
        "contract_id": "CTR-2026-021",
        "vendor": "Harbor Analytics",
        "agency": "Water Authority",
        "award_date": "2026-01-17",
        "ceiling_usd": "950000",
        "status": "under_review",
        "description": "Lead-test address matching and workflow design",
    },
    {
        "contract_id": "CTR-2026-033",
        "vendor": "Meridian Food Services",
        "agency": "Human Services",
        "award_date": "2026-01-22",
        "ceiling_usd": "1850000",
        "status": "active",
        "description": "Emergency shelter meal service",
    },
    {
        "contract_id": "CTR-2026-041",
        "vendor": "Bright Harbor Systems",
        "agency": "Public Library",
        "award_date": "2026-02-08",
        "ceiling_usd": "184000",
        "status": "active",
        "description": "Digitization request software pilot",
    },
    {
        "contract_id": "CTR-2026-052",
        "vendor": "Greenline Civic Data",
        "agency": "Parks Department",
        "award_date": "2026-03-02",
        "ceiling_usd": "398000",
        "status": "active",
        "description": "Citywide tree inventory and mapping",
    },
    {
        "contract_id": "CTR-2026-060",
        "vendor": "Kerbside Labs",
        "agency": "Transportation Department",
        "award_date": "2026-03-05",
        "ceiling_usd": "610000",
        "status": "canceled",
        "description": "Smart parking sensor purchase",
    },
    {
        "contract_id": "CTR-2026-071",
        "vendor": "Orchard Language Access",
        "agency": "City Clerk",
        "award_date": "2026-03-20",
        "ceiling_usd": "275000",
        "status": "active",
        "description": "Interpretation services for public meetings",
    },
    {
        "contract_id": "CTR-2026-084",
        "vendor": "Marrow Street Design",
        "agency": "Housing Department",
        "award_date": "2026-03-25",
        "ceiling_usd": "440000",
        "status": "active",
        "description": "Foundry District public-realm design",
    },
)

SAMPLE_DISPATCH_ROWS = len(_DISPATCHES)
SAMPLE_CONTRACT_ROWS = len(_CONTRACTS)
SAMPLE_LAWSUIT_ROWS = len(SAMPLE_LAWSUITS)
SAMPLE_COUNCIL_AUDIO_ROWS = len(SAMPLE_COUNCIL_AUDIO)
SAMPLE_MULTILINGUAL_ROWS = len(_MULTILINGUAL_NAMES)
SAMPLE_AGENCY_PAYMENT_ROWS = len(_AGENCY_PAYMENTS)
SAMPLE_LOCAL_MODEL_LAB_ROWS = len(LOCAL_MODEL_LAB_ROWS)
# Compatibility name used by existing callers and tests.
SAMPLE_ARTICLE_ROWS = SAMPLE_DISPATCH_ROWS


def _csv(rows: tuple[dict[str, str], ...], fieldnames: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def sample_dispatches_csv() -> str:
    rows = tuple(
        {
            "record_id": row["record_id"],
            "headline": row["headline"],
            "published_at": row["published_at"],
            "source_kind": row["source_kind"],
            "publisher": row["publisher"],
            "canonical_url": f"{_SITE_ROOT}/dispatches/{row['record_id']}.html",
            "story": row["story"],
        }
        for row in _DISPATCHES
    )
    return _csv(
        rows,
        [
            "record_id",
            "headline",
            "published_at",
            "source_kind",
            "publisher",
            "canonical_url",
            "story",
        ],
    )


def sample_contracts_csv() -> str:
    return _csv(
        _CONTRACTS,
        [
            "contract_id",
            "vendor",
            "agency",
            "award_date",
            "ceiling_usd",
            "status",
            "description",
        ],
    )


def sample_docket_csv() -> str:
    rows = tuple(
        {
            "pdf_filename": lawsuit.filename,
            "case_number": lawsuit.case_number,
            "court": lawsuit.court,
            "filed_at": lawsuit.filed_at,
            "plaintiff": lawsuit.plaintiff,
            "defendant_as_filed": lawsuit.defendant,
            "document_type": lawsuit.document_type,
            "status": lawsuit.status,
        }
        for lawsuit in SAMPLE_LAWSUITS
    )
    return _csv(
        rows,
        [
            "pdf_filename",
            "case_number",
            "court",
            "filed_at",
            "plaintiff",
            "defendant_as_filed",
            "document_type",
            "status",
        ],
    )


def sample_council_meetings_csv() -> str:
    rows = tuple(
        {
            "audio_filename": item.filename,
            "meeting": item.meeting,
            "meeting_date": item.meeting_date,
            "duration_seconds": str(item.clip_end_seconds - item.clip_start_seconds),
            "source_url": item.source_url,
            "license": item.license,
            "author": item.author,
        }
        for item in SAMPLE_COUNCIL_AUDIO
    )
    return _csv(
        rows,
        [
            "audio_filename",
            "meeting",
            "meeting_date",
            "duration_seconds",
            "source_url",
            "license",
            "author",
        ],
    )


def sample_multilingual_names_csv() -> str:
    return _csv(
        _MULTILINGUAL_NAMES,
        ["record_id", "name_as_written", "source", "record_note"],
    )


def sample_agency_payments_csv() -> str:
    return _csv(
        _AGENCY_PAYMENTS,
        ["payment_id", "agency", "vendor", "paid_at", "amount_usd", "memo"],
    )


def sample_small_model_lab_csv() -> str:
    return _csv(
        LOCAL_MODEL_LAB_ROWS,
        [
            "record_id",
            "easy_notice",
            "expected_notice_type",
            "challenge_text",
            "challenge_answer",
        ],
    )


# Old import name retained for compatibility; the content is now Dispatches.
def sample_articles_csv() -> str:
    return sample_dispatches_csv()


class SampleProjectSeedError(RuntimeError):
    """Raised when the sample import fails to produce a sheet."""


class SampleProjectSeedService:
    """Populate an existing project with the ready-to-use sample sheets."""

    def __init__(self, workspace: Workspace):
        self._workspace = workspace
        self._import = ImportCsvUploadService(workspace)
        self._files = ImportFilesUploadService(workspace)

    def _ensure_sheet(
        self,
        project_id: str,
        *,
        name: str,
        filename: str,
        content: str,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        existing = next(
            (sheet for sheet in project.sheets() if sheet["name"] == name), None
        )
        if existing is not None:
            return {
                "sheet_id": existing["id"],
                "rows": project.row_count(existing["id"]),
            }
        data = content.encode("utf-8")
        with ExitStack() as stack:
            response = self._import.upload_csv(
                project_id,
                upload=_owned_upload(
                    stack,
                    filename=filename,
                    mime="text/csv",
                    data=data,
                ),
                sheet_name=name,
            )
        if response.status_code != 200 or "sheet_id" not in response.payload:
            raise SampleProjectSeedError(f"sample seed import did not produce {name}")
        return response.payload

    def _ensure_file_sheet(self, project_id: str) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        existing = next(
            (
                sheet
                for sheet in project.sheets()
                if sheet["name"] == SAMPLE_FILINGS_SHEET_NAME
            ),
            None,
        )
        if existing is not None:
            return {
                "sheet_id": existing["id"],
                "rows": project.row_count(existing["id"]),
            }
        with ExitStack() as stack:
            response = self._files.upload_files(
                project_id,
                files=[
                    _owned_upload(
                        stack,
                        filename=lawsuit.filename,
                        mime="application/pdf",
                        data=lawsuit_pdf_bytes(lawsuit),
                    )
                    for lawsuit in SAMPLE_LAWSUITS
                ],
                sheet_name=SAMPLE_FILINGS_SHEET_NAME,
            )
        if response.status_code != 200 or "sheet_id" not in response.payload:
            raise SampleProjectSeedError(
                f"sample seed import did not produce {SAMPLE_FILINGS_SHEET_NAME}"
            )
        return response.payload

    def _ensure_audio_sheet(self, project_id: str) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        existing = next(
            (
                sheet
                for sheet in project.sheets()
                if sheet["name"] == SAMPLE_COUNCIL_AUDIO_SHEET_NAME
            ),
            None,
        )
        if existing is not None:
            return {
                "sheet_id": existing["id"],
                "rows": project.row_count(existing["id"]),
            }
        with ExitStack() as stack:
            response = self._files.upload_files(
                project_id,
                files=[
                    _owned_upload(
                        stack,
                        filename=item.filename,
                        mime="audio/mpeg",
                        data=council_audio_bytes(item.filename),
                    )
                    for item in SAMPLE_COUNCIL_AUDIO
                ],
                sheet_name=SAMPLE_COUNCIL_AUDIO_SHEET_NAME,
            )
        if response.status_code != 200 or "sheet_id" not in response.payload:
            raise SampleProjectSeedError(
                f"sample seed import did not produce {SAMPLE_COUNCIL_AUDIO_SHEET_NAME}"
            )
        return response.payload

    def seed(self, project_id: str) -> dict[str, Any]:
        # Validate the project before beginning the ordinary imports.
        self._workspace.get(project_id)
        dispatches = self._ensure_sheet(
            project_id,
            name=SAMPLE_SHEET_NAME,
            filename="riverton_dispatches.csv",
            content=sample_dispatches_csv(),
        )
        self._ensure_sheet(
            project_id,
            name=SAMPLE_CONTRACTS_SHEET_NAME,
            filename="riverton_contracts.csv",
            content=sample_contracts_csv(),
        )
        self._ensure_sheet(
            project_id,
            name=SAMPLE_DOCKET_SHEET_NAME,
            filename="riverton_court_docket.csv",
            content=sample_docket_csv(),
        )
        self._ensure_file_sheet(project_id)
        self._ensure_sheet(
            project_id,
            name=SAMPLE_COUNCIL_MEETINGS_SHEET_NAME,
            filename="ann_arbor_council_meetings.csv",
            content=sample_council_meetings_csv(),
        )
        self._ensure_audio_sheet(project_id)
        self._ensure_sheet(
            project_id,
            name=SAMPLE_MULTILINGUAL_SHEET_NAME,
            filename="multilingual_names.csv",
            content=sample_multilingual_names_csv(),
        )
        self._ensure_sheet(
            project_id,
            name=SAMPLE_AGENCY_PAYMENTS_SHEET_NAME,
            filename="agency_payments.csv",
            content=sample_agency_payments_csv(),
        )
        self._ensure_sheet(
            project_id,
            name=SAMPLE_SMALL_MODEL_LAB_SHEET_NAME,
            filename="small_model_lab.csv",
            content=sample_small_model_lab_csv(),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "sheet_id": dispatches["sheet_id"],
            "sheet_name": SAMPLE_SHEET_NAME,
            "blank_column": SAMPLE_BLANK_COLUMN,
            "rows": dispatches.get("rows", SAMPLE_DISPATCH_ROWS),
        }

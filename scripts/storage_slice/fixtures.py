"""Deterministic, repository-owned documents for the storage slice."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import hashlib

from frisket.sample_content.lawsuits import SAMPLE_LAWSUITS, lawsuit_pdf_bytes
from scripts.storage_slice.store import Document, Extraction


_RAW_AMOUNTS: tuple[str | None, ...] = (
    "$845,000",
    "not-stated",
    "$214,600",
    None,
    "$925,000",
    "not-stated",
)
_AMOUNT_CENTS = {1: 84_500_000, 3: 21_460_000, 5: 92_500_000}
_QUOTES = {
    1: "The total demand is not less than $845,000.",
    3: "Meridian asks the court to prevent Riverton from withholding $214,600 "
    "under shelter meal contract CTR-2026-033.",
    5: "Eastbank seeks a $925,000 change order for corroded expansion joints "
    "and a 63-day extension.",
}

# ``not-stated`` is deliberately an invalid parser input, not an assertion that
# the source uses that phrase.  Keeping this metadata public makes that fixture
# choice reviewable while real source facts retain their literal dollar strings.
FIXTURE_INPUT_STATE = {
    1: "source_amount",
    2: "fixture_invalid_token",
    3: "source_amount",
    4: "source_missing",
    5: "source_amount",
    6: "fixture_invalid_token",
}


def _category(document_type: str) -> str:
    lowered = document_type.lower()
    if "petition" in lowered:
        return "petition"
    if "motion" in lowered:
        return "motion"
    return "complaint"


def sample_documents() -> list[Document]:
    """Return the six searchable lawsuit PDFs with stable 1-based row IDs."""

    return [
        Document(
            row_id=row_id,
            title=f"{lawsuit.case_number}: {lawsuit.document_type}",
            category=_category(lawsuit.document_type),
            raw_amount=_RAW_AMOUNTS[row_id - 1],
            text="\n\n".join(lawsuit.pages),
            asset=lawsuit_pdf_bytes(lawsuit),
        )
        for row_id, lawsuit in enumerate(SAMPLE_LAWSUITS, start=1)
    ]


def sample_extractions(rows: list[dict]) -> list[Extraction]:
    """Label known source amounts; this is deterministic fixture data, not LLM proof."""

    outputs: list[Extraction] = []
    for row in rows:
        row_id = int(row["row_id"])
        if row_id not in range(1, len(SAMPLE_LAWSUITS) + 1):
            raise ValueError(f"unknown sample row {row_id}")
        quote = _QUOTES.get(row_id)
        outputs.append(
            Extraction(
                row_id=row_id,
                source_version=str(row["source_version"]),
                amount_cents=_AMOUNT_CENTS.get(row_id),
                quotes=(quote,) if quote else (),
            )
        )
    return outputs


def _synthetic_amount_cents(row_id: int) -> int:
    return ((row_id % 97) + 1) * 10_000


def _synthetic_document(row_id: int) -> Document:
    categories = ("complaint", "petition", "motion")
    category = categories[row_id % len(categories)]
    amount_cents = _synthetic_amount_cents(row_id)
    raw_amount = f"${amount_cents // 100:,}"
    text = (
        f"SYNTHETIC {category.upper()} {row_id}\n\n"
        f"This repository-owned document is a deterministic storage benchmark "
        f"record for docket {row_id}. It describes a municipal contract review, "
        f"a public hearing, and a request for durable citation evidence. The "
        f"facts vary by record number so full-text values are not repeated.\n\n"
        f"The requested amount is {raw_amount}. The amount is included solely "
        f"to exercise typed extraction, edits, filtering, and grouped aggregates. "
        f"Reviewers should preserve the exact quoted sentence with its source "
        f"version when publishing a result.\n\n"
        f"Record {row_id} uses the {category} category. Its fictional chronology "
        f"includes notice number {row_id * 17}, a hearing after {row_id % 28 + 1} "
        f"days, and a written response. None of these statements describes a real "
        f"person, organization, case, or financial event."
    )
    return Document(
        row_id=row_id,
        title=f"Synthetic {category} {row_id}",
        category=category,
        raw_amount=raw_amount,
        text=text,
    )


def synthetic_documents(start: int, count: int) -> Iterator[Document]:
    """Yield a bounded, no-PDF load corpus without retaining all rows in memory."""

    if start < 1 or count < 0:
        raise ValueError("start must be positive and count must be non-negative")
    for row_id in range(start, start + count):
        yield _synthetic_document(row_id)


def synthetic_extractions(rows: Iterable[dict]) -> list[Extraction]:
    """Build one source-backed numeric extraction per paged ``read_rows`` batch."""

    outputs: list[Extraction] = []
    for row in rows:
        row_id = int(row["row_id"])
        amount_cents = _synthetic_amount_cents(row_id)
        quote = f"The requested amount is ${amount_cents // 100:,}."
        text = row.get("text")
        if isinstance(text, str) and quote not in text:
            raise ValueError(f"synthetic quote missing from row {row_id}")
        outputs.append(
            Extraction(
                row_id=row_id,
                source_version=str(row["source_version"]),
                amount_cents=amount_cents,
                quotes=(quote,),
            )
        )
    return outputs


_STRESS_CATEGORIES = (
    "contracts",
    "courts",
    "education",
    "environment",
    "health",
    "housing",
    "labor",
    "transport",
)
_STRESS_WORDS = (
    "agency",
    "analysis",
    "appeal",
    "archive",
    "budget",
    "committee",
    "contract",
    "county",
    "docket",
    "evidence",
    "filing",
    "hearing",
    "inspection",
    "invoice",
    "meeting",
    "notice",
    "ordinance",
    "permit",
    "project",
    "proposal",
    "record",
    "report",
    "request",
    "review",
    "schedule",
    "statement",
    "transcript",
    "vendor",
)


def _stress_category(row_id: int) -> str:
    # Deliberately skewed rather than round-robin categories.
    bucket = row_id % 100
    thresholds = (38, 59, 73, 83, 90, 95, 98, 100)
    return next(
        category
        for category, threshold in zip(_STRESS_CATEGORIES, thresholds, strict=True)
        if bucket < threshold
    )


def _stress_text_length(row_id: int) -> int:
    digest = int.from_bytes(
        hashlib.blake2b(str(row_id).encode(), digest_size=8).digest(), "big"
    )
    bucket = row_id % 200
    if bucket < 150:
        return 512 + digest % 1025
    if bucket < 190:
        return 3 * 1024 + digest % (5 * 1024 + 1)
    if bucket < 199:
        return 16 * 1024 + digest % (32 * 1024 + 1)
    return 96 * 1024 + digest % (96 * 1024 + 1)


def _stress_amount_cents(row_id: int, generation: int = 0) -> int:
    return ((row_id * 7919 + generation * 104729) % 9_000_000) + 10_000


def _stress_quote(row_id: int) -> str:
    return (
        f"The recorded request for item {row_id} is "
        f"${_stress_amount_cents(row_id) // 100:,}."
    )


def _stress_text(row_id: int, category: str) -> str:
    target = _stress_text_length(row_id)
    quote = _stress_quote(row_id)
    blocks: list[str] = []
    filler_length = 0
    block = 0
    while filler_length < target + len(quote):
        digest = hashlib.blake2b(f"{row_id}:{block}".encode(), digest_size=24).digest()
        words = " ".join(_STRESS_WORDS[value % len(_STRESS_WORDS)] for value in digest)
        value = (
            f"{category.title()} record {row_id}, section {block}: {words}. "
            f"Reference {digest.hex()[:12]} was reviewed on day "
            f"{(row_id + block) % 28 + 1}.\n\n"
        )
        blocks.append(value)
        filler_length += len(value)
        block += 1
    filler = "".join(blocks)
    prefix_length = min(
        len(filler), (target - len(quote) - 2) * ((row_id % 5) + 1) // 6
    )
    text = filler[:prefix_length] + quote + "\n\n" + filler[prefix_length:]
    return text[: max(target, len(quote) + 2)]


def stress_documents(start: int, count: int) -> Iterator[Document]:
    """Stream a deterministic mixed-length, skewed investigative corpus."""

    if start < 1 or count < 0:
        raise ValueError("start must be positive and count must be non-negative")
    for row_id in range(start, start + count):
        category = stress_project_category(row_id)
        state = row_id % 10
        amount_cents = _stress_amount_cents(row_id)
        raw_amount = (
            None
            if state == 0
            else "not-stated"
            if state == 1
            else f"${amount_cents // 100:,}"
        )
        yield Document(
            row_id=row_id,
            title=f"Synthetic {category} record {row_id}",
            category=category,
            raw_amount=raw_amount,
            text=_stress_text(row_id, category),
        )


def stress_project_marker(row_id: int) -> str:
    """A stable late-document token used to prove bounded FTS snippets."""

    return f"frisketlate{row_id:08d}"


def stress_project_search_tokens(row_id: int) -> dict[str, str]:
    """Stable position-specific tokens for selected long documents."""

    return {
        "early": f"frisketearly{row_id:08d}",
        "middle": f"frisketmiddle{row_id:08d}",
        "late": stress_project_marker(row_id),
    }


STRESS_BOUNDED_SEARCH_ROWS = (199, 399, 599, 799, 999)
STRESS_BOUNDED_SEARCH_TOKEN = "frisketboundedfive"


def stress_project_amount(row_id: int) -> int | str | None:
    state = row_id % 10
    if state == 0:
        return None
    if state == 1:
        return "not-stated"
    return _stress_amount_cents(row_id)


def stress_project_category(row_id: int) -> str:
    return _stress_category(row_id)


def stress_project_date(row_id: int) -> str:
    return f"2026-{row_id % 12 + 1:02d}-{row_id % 28 + 1:02d}"


def stress_project_records(start: int, count: int) -> Iterator[dict]:
    """Stream mixed values through Frisket's real Project import path."""

    if start < 1 or count < 0:
        raise ValueError("start must be positive and count must be non-negative")
    for row_id in range(start, start + count):
        category = stress_project_category(row_id)
        body = _stress_text(row_id, category)
        # Every 200th record is a long document. Position-specific tokens prove
        # the index and snippet path did not retain only one document prefix.
        if row_id % 200 == 199:
            tokens = stress_project_search_tokens(row_id)
            middle = len(body) // 2
            bounded = (
                f" {STRESS_BOUNDED_SEARCH_TOKEN}"
                if row_id in STRESS_BOUNDED_SEARCH_ROWS
                else ""
            )
            body = (
                f"{tokens['early']}\n{body[:middle]}\n"
                f"{tokens['middle']}{bounded}\n{body[middle:]}\n{tokens['late']}"
            )
        amount = stress_project_amount(row_id)
        yield {
            "record_id": row_id,
            "title": f"{category.title()} record {row_id}",
            "body": body,
            "category": category,
            "amount": amount,
            "published_at": stress_project_date(row_id),
            "status": ("open", "review", "closed")[row_id % 3],
        }


def stress_extractions(
    rows: Iterable[dict], *, generation: int = 0
) -> list[Extraction]:
    """Create one generation of source-backed values for a bounded row page."""

    return [
        Extraction(
            row_id=int(row["row_id"]),
            source_version=str(row["source_version"]),
            amount_cents=_stress_amount_cents(int(row["row_id"])),
            quotes=(_stress_quote(int(row["row_id"])),),
        )
        for row in rows
    ]

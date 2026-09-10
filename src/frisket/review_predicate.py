"""Shared predicate for "this column is a primary AI result subject to
review" vs. a support column (confidence/justification/citations/etc.) that
merely annotates one. Both the store layer (Project.refresh_pending_review_summary
in src/frisket/engine/store/project.py, which maintains the cached per-project pending
count) and the application-level review queue (src/frisket/engine/runner/review.py)
import this so "pending review" means the same thing everywhere."""

from __future__ import annotations

SUPPORT_COLUMN_NAMES = (
    "confidence",
    "justification",
    "source",
    "sources",
    "citation",
    "citations",
    "evidence",
    "rationale",
)
SUPPORT_COLUMN_SUFFIXES = (
    "_confidence",
    "_justification",
    "_source",
    "_sources",
    "_citation",
    "_citations",
    "_evidence",
    "_rationale",
)


def is_support_column(column_name: str) -> bool:
    name = column_name.lower()
    return name in SUPPORT_COLUMN_NAMES or name.endswith(SUPPORT_COLUMN_SUFFIXES)


def primary_where(alias: str = "c") -> str:
    name = f"lower({alias}.name)"
    clauses = [f"{name} NOT IN ({','.join('?' for _ in SUPPORT_COLUMN_NAMES)})"]
    for suffix in SUPPORT_COLUMN_SUFFIXES:
        clauses.append(f"{name} NOT LIKE ? ESCAPE '\\'")
    return " AND ".join(clauses)


def primary_params() -> tuple[str, ...]:
    escaped_suffixes = tuple(s.replace("_", "\\_") for s in SUPPORT_COLUMN_SUFFIXES)
    return (*SUPPORT_COLUMN_NAMES, *(f"%{s}" for s in escaped_suffixes))

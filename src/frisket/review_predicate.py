"""Shared predicate for "this column is a primary AI result subject to
review" vs. a support column (confidence/justification/citations/etc.) that
merely annotates one. Both the store layer (Project.refresh_pending_review_summary
in src/frisket/engine/store/project.py, which maintains the cached per-project pending
count) and the application-level review queue (src/frisket/engine/runner/review.py)
import this so "pending review" means the same thing everywhere."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

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


def is_support_column(column_name: str, *, action_kind: str | None = None) -> bool:
    # Every declared map.find output is authored result data.  A caller may
    # legitimately name a detail "source" or "confidence"; the generic
    # name convention must not silently remove it from Review.
    if action_kind == "map.find":
        return False
    name = column_name.lower()
    return name in SUPPORT_COLUMN_NAMES or name.endswith(SUPPORT_COLUMN_SUFFIXES)


def is_exact_review_correction(
    *,
    run_id: int,
    row_id: int,
    column_id: int,
    ref: Mapping[str, Any] | None,
    op: Mapping[str, Any] | None,
) -> bool:
    """Whether the current edit was authored by this exact review target."""

    if (
        not isinstance(ref, Mapping)
        or ref.get("kind") != "manual_edit"
        or not isinstance(op, Mapping)
        or op.get("status") != "applied"
        or op.get("kind") != "review.decision"
    ):
        return False
    raw_spec = op.get("spec")
    if isinstance(raw_spec, str):
        try:
            spec = json.loads(raw_spec)
        except (TypeError, ValueError):
            return False
    else:
        spec = raw_spec
    params = spec.get("params") if isinstance(spec, Mapping) else None
    return (
        isinstance(params, Mapping)
        and spec.get("action_id") == "review.decision"
        and params.get("run_id") == run_id
        and params.get("row_id") == row_id
        and params.get("column_id") == column_id
        and params.get("decision") in {"edit", "reject_clear"}
    )


def primary_where(alias: str = "c", *, run_alias: str | None = None) -> str:
    name = f"lower({alias}.name)"
    clauses = [f"{name} NOT IN ({','.join('?' for _ in SUPPORT_COLUMN_NAMES)})"]
    for suffix in SUPPORT_COLUMN_SUFFIXES:
        clauses.append(f"{name} NOT LIKE ? ESCAPE '\\'")
    conventional = " AND ".join(clauses)
    if run_alias is None:
        return conventional
    return f"({run_alias}.action_kind='map.find' OR ({conventional}))"


def visible_result_where(row_alias: str = "rr", column_alias: str = "c") -> str:
    """SQL predicate shared by Review reads and mutations."""
    return (
        f"{row_alias}.hidden = 0 AND EXISTS ("
        "SELECT 1 FROM sheets review_sheet "
        f"WHERE review_sheet.id={column_alias}.sheet_id "
        "AND review_sheet.hidden=0)"
    )


def primary_params() -> tuple[str, ...]:
    escaped_suffixes = tuple(s.replace("_", "\\_") for s in SUPPORT_COLUMN_SUFFIXES)
    return (*SUPPORT_COLUMN_NAMES, *(f"%{s}" for s in escaped_suffixes))

"""Shared predicate for "this column is a primary AI result subject to
review" vs. a support column (confidence/justification/citations/etc.) that
merely annotates one. Both the store layer (Project.refresh_pending_review_summary
in src/frisket/engine/store/project.py, which maintains the cached per-project pending
count) and the application-level review queue (src/frisket/engine/runner/review.py)
import this so "pending review" means the same thing everywhere."""

from __future__ import annotations

import json
from collections.abc import Mapping
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

# Logical output keys are authored by the action and survive user-chosen column
# names in runs.params.output_names.  Keep this narrow: these are companion
# artifacts used to present or ground the primary result, not separate review
# chores.  Name conventions remain the fallback for older/unmanaged writers.
PRIMARY_OUTPUT_ROLES = {
    "media.ocr": frozenset({"text"}),
    "research.answer": frozenset({"answer"}),
    "research.web_search": frozenset({"answer"}),
}
SUPPORT_OUTPUT_ROLES = {
    "media.ocr": frozenset({"blocks", "pdf"}),
    "research.answer": frozenset({"sources"}),
    "research.extract": frozenset({"sources"}),
    "research.web_search": frozenset({"search_results"}),
}


def authored_output_roles(raw_params: object) -> dict[str, str]:
    """Map materialized column names to stable action output keys."""

    try:
        params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
    except (TypeError, ValueError):
        return {}
    output_names = params.get("output_names") if isinstance(params, Mapping) else None
    if not isinstance(output_names, Mapping):
        return {}
    return {
        materialized: logical
        for logical, materialized in output_names.items()
        if isinstance(logical, str)
        and logical
        and isinstance(materialized, str)
        and materialized
    }


def has_explicit_review_role(action_kind: str | None, output_role: str | None) -> bool:
    return bool(
        output_role
        and (
            output_role in PRIMARY_OUTPUT_ROLES.get(action_kind or "", ())
            or output_role in SUPPORT_OUTPUT_ROLES.get(action_kind or "", ())
        )
    )


def is_support_column(
    column_name: str,
    *,
    action_kind: str | None = None,
    output_role: str | None = None,
) -> bool:
    if output_role in PRIMARY_OUTPUT_ROLES.get(action_kind or "", ()):
        return False
    if output_role in SUPPORT_OUTPUT_ROLES.get(action_kind or "", ()):
        return True
    if action_kind == "research.extract" and output_role:
        # Authored fields remain results even when named "source" or "evidence".
        # The field validator reserves this suffix for generated confidence.
        return output_role.endswith("_confidence")
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

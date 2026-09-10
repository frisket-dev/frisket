"""Producer-owned result publication effects shared by row normalizers."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from frisket.redaction import safe_error

PUBLISH_VALUE = "publish_value"
PUBLISH_NULL = "publish_null"
PUBLISH_ERROR = "publish_error"


@dataclass(frozen=True)
class PreparedRowPublication:
    """Typed cells which bypass the legacy recipe dictionary convention."""

    trace_data: dict[str, Any]
    cells: dict[str, dict[str, Any]]


def finalize_prepared_row_publication(
    publication: PreparedRowPublication,
    field_names: Collection[str],
    meta: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Attach host facts and publication effects to typed result cells."""

    names = tuple(field_names)
    if set(publication.cells) != set(names):
        raise ValueError("prepared row publication does not match declared outputs")
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        cell = {**publication.cells[name], **meta}
        if cell.get("error") is not None:
            error = safe_error(
                cell.get("error_code") or "model_error",
                cell["error"],
                # Outcome constrains code/message to 64/500 characters. Give
                # the sanitizer room for its ``code: `` envelope so a safe
                # authored message is not shortened merely by having a code.
                max_chars=500 + 64 + 2,
            )
            cell["value"] = None
            cell["error"] = error.detail
            cell["error_code"] = error.code
            cell["publication_effect"] = PUBLISH_ERROR
        elif cell.get("value") is None:
            cell["publication_effect"] = PUBLISH_NULL
        else:
            cell["publication_effect"] = PUBLISH_VALUE
        out[name] = cell
    # Host accounting belongs to the row, not independently to every output.
    for cell in tuple(out.values())[1:]:
        cell["cost"] = 0.0
        cell["tokens_in"] = None
        cell["tokens_out"] = None
        cell.pop("model_calls", None)
    return out


def required_publication_fields(
    recipe: Any,
    output_fields: Collection[dict[str, Any]],
) -> frozenset[str]:
    """Return fields whose omission is a producer contract violation.

    A sole declared output is total by default. Atomic output families declare
    sibling attemptedness as a group. Other multi-output recipes opt fields in
    explicitly; an absent independent field remains unattempted and therefore
    must not overwrite an older head.
    """

    fields = tuple(output_fields)
    sole_output = len(fields) == 1
    return frozenset(
        str(field["name"])
        for field in fields
        if sole_output
        or bool(getattr(recipe, "atomic_output_columns", False))
        or field.get("publication_required") is True
    )

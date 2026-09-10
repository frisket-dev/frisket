"""Pure raw-to-cells transform for batch-recipe output, extracted from
map_runner.py so leaf callers need not import the engine.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from frisket.redaction import redact_text

from frisket.engine.runner.publication import (
    PUBLISH_ERROR,
    PUBLISH_NULL,
    PUBLISH_VALUE,
    PreparedRowPublication,
    finalize_prepared_row_publication,
)


def normalize_batch_row(
    raw: Any,
    field_names: list[str],
    *,
    missing_is_error: bool = True,
    required_field_names: Collection[str] = (),
    managed_publication: bool = False,
) -> tuple[dict[str, dict[str, Any]], bool, float]:
    """Pure raw-to-cells transform: the shared
    normalizer for batch-recipe output, used by both ``_run_batch_recipe``
    (persisted) and ``preview()`` (in-memory). Returns ``{field: cell}``
    WITHOUT ``row_id``/``column_id`` (callers add those), a ``failed`` flag,
    and the row's cost. No side effects — the ``pending``/``flush``/progress
    bookkeeping stays with each caller.

    ``missing_is_error`` mirrors ``_run_batch_recipe``'s cancel-aware None
    handling: a cancelled run leaves an unattempted row absent; everywhere
    else a missing row is a per-cell error."""
    if raw is None:
        if not missing_is_error:
            return {}, False, 0.0
        raw = {"__error__": "batch recipe returned no result"}
    if isinstance(raw, tuple) and len(raw) == 2:
        data, returned_meta = raw
        meta: dict[str, Any] = {"tokens_in": None, "tokens_out": None, "cost": 0.0}
        meta.update(returned_meta or {})
    else:
        data = raw if isinstance(raw, (dict, PreparedRowPublication)) else {}
        meta = {"tokens_in": None, "tokens_out": None, "cost": 0.0}

    if meta.get("error") is not None:
        meta["error"] = redact_text(meta["error"], max_chars=500)

    if isinstance(data, PreparedRowPublication):
        cells = finalize_prepared_row_publication(data, field_names, meta)
        return (
            cells,
            any(cell.get("error") is not None for cell in cells.values()),
            meta.get("cost") or 0.0,
        )

    err = data.get("__error__")
    if err:
        cells = {
            name: {
                "value": None,
                "error": redact_text(err, max_chars=500),
                # batch recipes (clean_column, census demographics, ...)
                # raise a bare str, not a classified RemediatedError — a
                # generic code still beats none for the error summary's
                # grouping/display.
                "error_code": "batch_recipe_error",
                "publication_effect": PUBLISH_ERROR,
            }
            for name in field_names
        }
        return cells, True, 0.0

    confidence = next(
        (v for k, v in data.items() if k.endswith("_confidence")),
        data.get("confidence"),
    )
    cells = {}
    required = frozenset(str(name) for name in required_field_names)
    # Preserve the established progress contract: metadata-carried errors are
    # persisted on the cell, but only a recipe-row error or an unresolved
    # declared field increments the batch helper's row-failed return flag.
    failed = False
    for name in field_names:
        value_was_returned = name in data
        cell: dict[str, Any] = {
            "value": data.get(name) if value_was_returned else None,
            **meta,
        }
        if cell.get("error") is not None:
            if managed_publication:
                cell["value"] = None
            cell["error_code"] = cell.get("error_code") or "batch_recipe_error"
            cell["publication_effect"] = PUBLISH_ERROR
        elif not value_was_returned and managed_publication:
            if name not in required:
                continue
            cell["error"] = redact_text(
                f"batch recipe did not return declared output field {name!r}",
                max_chars=500,
            )
            cell["error_code"] = "invalid_output"
            cell["outcome"] = "invalid_output"
            cell["publication_effect"] = PUBLISH_ERROR
            failed = True
        elif cell["value"] is None:
            cell["publication_effect"] = PUBLISH_NULL
        else:
            cell["publication_effect"] = PUBLISH_VALUE
        if cells:
            cell["cost"] = 0.0
            cell["tokens_in"] = None
            cell["tokens_out"] = None
            cell.pop("model_calls", None)
        if confidence is not None:
            cell["confidence"] = confidence
        just = data.get(f"{name}_justification")
        if just and not name.endswith("_justification"):
            cell["justification"] = just
        cells[name] = cell
    return cells, failed, meta.get("cost") or 0.0

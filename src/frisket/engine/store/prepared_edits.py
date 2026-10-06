"""Translate page-owned text edits into immutable prepared-content refs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)


def prepared_edit_values(
    project: Any,
    *,
    op_id: int,
    targets: Sequence[Mapping[str, Any]],
) -> list[Any]:
    """Return stored edit values, replacing only editable page selectors.

    Whole-document text has no reliable page partition, and prepared content
    whose source artifact was deleted can no longer mint a replacement set.
    Both remain ordinary edit values under the existing flat-text behavior.
    """

    ref_ids = []
    for target in targets:
        value_ref = target.get("current_value_ref")
        prepared_ref_id = (
            value_ref.get("prepared_ref_id") if isinstance(value_ref, Mapping) else None
        )
        if (
            isinstance(target.get("value_after"), str)
            and type(prepared_ref_id) is int
            and prepared_ref_id > 0
        ):
            ref_ids.append(prepared_ref_id)
    if not ref_ids:
        return [target.get("value_after") for target in targets]

    store = PreparedContentStore(project)
    resolved = store.resolve_many(ref_ids)
    values: list[Any] = []
    for target in targets:
        value = target.get("value_after")
        value_ref = target.get("current_value_ref")
        prepared_ref_id = (
            value_ref.get("prepared_ref_id") if isinstance(value_ref, Mapping) else None
        )
        if (
            not isinstance(value, str)
            or type(prepared_ref_id) is not int
            or prepared_ref_id <= 0
        ):
            values.append(value)
            continue
        prepared = resolved[prepared_ref_id]
        if prepared.page_number is None or prepared.source_artifact_id is None:
            values.append(value)
            continue
        values.append(
            store.stage_replacement(
                base_ref_id=prepared_ref_id,
                producing_op_id=op_id,
                replacements=(
                    PreparedPageDraft(
                        page_number=prepared.page_number,
                        text=value,
                        positions=None,
                    ),
                ),
                page_number=prepared.page_number,
            )
        )
    return values


__all__ = ["prepared_edit_values"]

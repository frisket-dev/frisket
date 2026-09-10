"""Copy admitted list-item grounding onto the rows actually materialized."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from frisket.actions.types import RowSource
from frisket.engine.store import Project
from frisket.engine.store.evidence import record_evidence_link


def propagate_list_item_evidence(
    project: Project,
    *,
    sheet_id: int,
    op_id: int,
    child_row_ids: Sequence[int],
    sources: Sequence[RowSource],
    item_associations: Mapping[RowSource, Mapping[str, Any]],
) -> None:
    """Repoint existing per-item links in the caller's publication transaction.

    Associations belong to the admitted reader and use source token identity,
    since two distinct items can share the same parent row. Ungrounded items
    remain ungrounded; this operation creates no source spans or new claims.
    """
    if not project.db.in_transaction:
        raise ValueError("list-item evidence requires a publication transaction")
    for child_row_id, source in zip(child_row_ids, sources, strict=True):
        association = item_associations.get(source)
        if association is None:
            raise ValueError("list-item source was not admitted")
        item_index = association["item_index"]
        if (
            type(item_index) is not int
            or item_index < 0
            or association["source_sheet_id"] != source.sheet_id
            or association["source_row_id"] != source.row_id
        ):
            raise ValueError("list-item source association is invalid")
        source_link = association["evidence_link"]
        if source_link is None or not source_link["spans"]:
            continue
        record_evidence_link(
            project,
            subject_kind="row",
            subject_ref={
                "kind": "materialized_row",
                "sheet_id": sheet_id,
                "row_id": child_row_id,
            },
            spans=source_link["spans"],
            sheet_id=sheet_id,
            row_id=child_row_id,
            column_id=None,
            op_id=op_id,
            link_role="primary_support",
            producer={
                "source_action_kind": "derive.table_from_list",
                "repointed_from_link_id": source_link["id"],
                "repointed_from_row_id": source.row_id,
                "item_index": item_index,
            },
            metadata={
                "schema_version": "frisket.table_from_list_item_evidence_link.v1",
                "source_link_stable_id": source_link["stable_id"],
                "item_index": item_index,
            },
        )

"""Transaction-neutral persistence for undoable cell edit overlays."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.evidence import mark_evidence_stale_for_cell_refs
from frisket.engine.store.op_log import append_op, set_undo_info


def write_edit_overlay(
    project: Any,
    *,
    kind: str,
    label: str,
    spec: Mapping[str, Any],
    targets: list[dict[str, Any]],
    stale_reason: str,
    undo_info: Mapping[str, Any] | None = None,
) -> int:
    """Write one edit operation and its citation transition in the caller's txn."""

    info = dict(undo_info or {})
    op_id = append_op(
        project,
        kind,
        dict(spec),
        label=label,
        undo_info=info,
        commit=False,
    )
    insert_edits(
        project.db,
        op_id=op_id,
        edits=[
            EditCellWrite(
                row_id=int(target["row_id"]),
                column_id=int(target["column_id"]),
                value=target["value_after"],
            )
            for target in targets
        ],
    )
    stale_ids = mark_evidence_stale_for_cell_refs(
        project,
        targets,
        reason=stale_reason,
        preserve_map_extract_item_links=True,
    )
    if stale_ids:
        info["evidence_links_staled"] = [
            {"stable_id": stable_id, "stale_reason": stale_reason}
            for stable_id in stale_ids
        ]
        set_undo_info(project, op_id, info, commit=False)
    return op_id

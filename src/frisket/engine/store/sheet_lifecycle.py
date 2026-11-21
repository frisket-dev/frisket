"""Reversible sheet deletion and dependency inspection."""

from __future__ import annotations

import json
from typing import Any

from frisket.engine.store.import_sessions import require_import_sheet_write
from frisket.engine.store.changes import start_change
from frisket.engine.store.op_log import append_change_in_transaction
from frisket.engine.store.structural_changes import note_row_membership_change
from frisket.engine.store.transactions import write_transaction


class SheetDeleteNotFound(LookupError):
    pass


class SheetDeleteBlocked(RuntimeError):
    def __init__(self, message: str, *, dependent_sheet_ids: list[int] | None = None):
        super().__init__(message)
        self.dependent_sheet_ids = dependent_sheet_ids or []


def materialized_source_sheets(project: Any) -> dict[int, set[int]]:
    """Admitted table reads remain dependencies even with no materialized rows."""
    sources: dict[int, set[int]] = {}
    for row in project.db.execute(
        "SELECT child.id, op.spec FROM sheets child "
        "JOIN ops op ON op.id=child.parent_op_id "
        "WHERE child.hidden=0 AND op.status='applied'"
    ):
        try:
            spec = json.loads(row["spec"])
        except (TypeError, ValueError):
            continue
        # Only the typed table host's private read facts count, not authored
        # source metadata or arbitrary nested request fields.
        reads = spec.get("reads", []) if isinstance(spec, dict) else []
        if not isinstance(reads, list):
            continue
        for fact in reads:
            if not isinstance(fact, dict):
                continue
            if fact.get("kind") == "semantic_join_link_source":
                keys = ("source_sheet_id", "target_sheet_id")
            elif fact.get("kind") == "joined_tables_source":
                keys = ("left_sheet_id", "right_sheet_id")
            else:
                continue
            for key in keys:
                sheet_id = fact.get(key)
                if type(sheet_id) is int and sheet_id > 0:
                    sources.setdefault(int(row["id"]), set()).add(sheet_id)
    return sources


def dependent_sheets(project: Any, sheet_id: int) -> list[dict[str, Any]]:
    """Visible materialized sheets that consume ``sheet_id``.

    ``parent_sheet_id`` owns the ordinary single-parent edge. Join and other
    multi-parent materializations additionally record row membership in
    ``materialized_row_sources``; the UNION keeps the deletion guard honest for
    both sides of a join.
    """

    rows = project.db.execute(
        """
        SELECT DISTINCT child.id, child.name
        FROM sheets child
        WHERE child.hidden=0 AND child.parent_sheet_id=?
        UNION
        SELECT DISTINCT child.id, child.name
        FROM current_materialized_row_sources source
        JOIN rows materialized ON materialized.id=source.materialized_row_id
        JOIN sheets child ON child.id=materialized.sheet_id
        WHERE child.hidden=0 AND child.id<>? AND source.source_sheet_id=?
        UNION
        SELECT DISTINCT child.id, child.name
        FROM run_output_generations generation
        JOIN runs owner ON owner.id=generation.run_id
        JOIN ops owner_op ON owner_op.id=owner.op_id
        JOIN sheets child ON child.id=owner.sheet_id
        JOIN runs base ON base.id=generation.expected_base_run_id
        WHERE child.id<>? AND base.sheet_id=? AND owner_op.status<>'discarded'
        ORDER BY id
        """,
        (sheet_id, sheet_id, sheet_id, sheet_id, sheet_id),
    ).fetchall()
    dependents = {int(row["id"]): str(row["name"]) for row in rows}
    for child, sources in materialized_source_sheets(project).items():
        if child != sheet_id and sheet_id in sources and child not in dependents:
            dependents[child] = str(
                project.db.execute(
                    "SELECT name FROM sheets WHERE id=?", (child,)
                ).fetchone()["name"]
            )
    return [{"id": child, "name": dependents[child]} for child in sorted(dependents)]


def delete_sheet(project: Any, sheet_id: int) -> dict[str, Any]:
    """Hide containment without deleting independently retained derived data."""
    if project.db.in_transaction:
        raise RuntimeError("sheet deletion cannot run in a caller-owned transaction")
    with write_transaction(project.db):
        sheet = project.db.execute(
            "SELECT id,name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        if sheet is None:
            raise SheetDeleteNotFound(f"no sheet {sheet_id}")
        require_import_sheet_write(project.db, sheet_id)
        op_id = append_change_in_transaction(
            project,
            "sheet.delete",
            {"sheet_id": sheet_id},
            label=f"delete sheet {sheet['name']}",
            undo_info={"deleted_sheets": [sheet_id]},
        )
        start_change(project.db, op_id)
        project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet_id,))
        from .source_dependencies import stop_direct_consumers

        stop_direct_consumers(
            project,
            sheet_ids=[sheet_id],
            column_ids=[
                int(row[0])
                for row in project.db.execute(
                    "SELECT id FROM columns WHERE sheet_id=?", (sheet_id,)
                )
            ],
        )
        note_row_membership_change(project.db, [sheet_id])
    project.refresh_pending_review_summary()
    return {
        "ok": True,
        "deleted_sheet_id": sheet_id,
        "deleted_sheet_name": str(sheet["name"]),
    }

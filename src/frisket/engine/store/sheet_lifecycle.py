"""Permanent sheet deletion and dependency inspection."""

from __future__ import annotations

import json
from typing import Any


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
        FROM materialized_row_sources source
        JOIN rows materialized ON materialized.id=source.materialized_row_id
        JOIN sheets child ON child.id=materialized.sheet_id
        WHERE child.hidden=0 AND child.id<>? AND source.source_sheet_id=?
        ORDER BY id
        """,
        (sheet_id, sheet_id, sheet_id),
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
    sheet = project.db.execute(
        "SELECT id, name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise SheetDeleteNotFound(f"no sheet {sheet_id}")

    dependents = dependent_sheets(project, sheet_id)
    if dependents:
        names = ", ".join(item["name"] for item in dependents)
        raise SheetDeleteBlocked(
            f"cannot delete {sheet['name']!r}; used by {names}",
            dependent_sheet_ids=[item["id"] for item in dependents],
        )

    running = project.db.execute(
        "SELECT COUNT(*) FROM runs WHERE sheet_id=? AND status='running'", (sheet_id,)
    ).fetchone()[0]
    if running:
        raise SheetDeleteBlocked(
            f"cannot delete {sheet['name']!r} while it has a run in progress"
        )

    run_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM runs WHERE sheet_id=?", (sheet_id,)
        )
    ]
    artifact_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM source_artifacts WHERE source_sheet_id=?", (sheet_id,)
        )
    ]

    with project.db:
        if artifact_ids:
            marks = ",".join("?" for _ in artifact_ids)
            project.db.execute(
                f"DELETE FROM artifact_timeline_segments WHERE "
                f"derived_artifact_id IN ({marks}) OR source_artifact_id IN ({marks})",
                (*artifact_ids, *artifact_ids),
            )
            project.db.execute(
                f"DELETE FROM source_artifacts WHERE id IN ({marks})", artifact_ids
            )

        if run_ids:
            marks = ",".join("?" for _ in run_ids)
            # Generation rows deliberately RESTRICT both their owning and base
            # runs. A whole-sheet delete removes those declarations before the
            # runs and columns they describe.
            project.db.execute(
                f"DELETE FROM run_output_generations WHERE run_id IN ({marks}) "
                f"OR expected_base_run_id IN ({marks})",
                (*run_ids, *run_ids),
            )
            project.db.execute(f"DELETE FROM runs WHERE id IN ({marks})", run_ids)

        project.db.execute("DELETE FROM sheets WHERE id=?", (sheet_id,))
        project.db.execute(
            "UPDATE sheets SET position=("
            "SELECT COUNT(*) FROM sheets earlier "
            "WHERE earlier.hidden=0 AND (earlier.position<sheets.position "
            "OR (earlier.position=sheets.position AND earlier.id<sheets.id))"
            ")+1 WHERE hidden=0"
        )
        project.db.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
        spec = json.dumps(
            {"sheet_id": sheet_id, "sheet_name": str(sheet["name"])}, sort_keys=True
        )
        cursor = project.db.execute(
            "INSERT INTO ops (kind, label, spec, undo_info, barrier) "
            "VALUES ('sheet.delete', ?, ?, NULL, 1)",
            (f"delete sheet {sheet['name']}", spec),
        ).lastrowid
        project.db.execute(
            "UPDATE meta SET value=? WHERE key='op_cursor'", (str(cursor),)
        )

    # Physical sheet deletion drops cells/results/evidence roots. Reclaim any
    # media bytes that are no longer referenced elsewhere in the project.
    project.gc_blobs()
    return {
        "ok": True,
        "deleted_sheet_id": sheet_id,
        "deleted_sheet_name": str(sheet["name"]),
    }

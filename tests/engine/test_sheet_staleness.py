"""Lazy derived-sheet staleness (Workbench IA increment 7 backend).

Staleness is PULL-computed at read time from the watermark
(``sheets.last_verified_op_cursor`` falling back to ``parent_op_id``) versus the
live op log -- never pushed from mutation sites. These tests drive REAL actions
(import -> derive -> cell.edit -> undo) and assert the resolver
(``frisket.store.staleness.compute_sync_states``) flips a derived sheet
synced<->stale, cascades to grandchildren, honors cursor rewinds (undo), covers
review-state flips on ancestors, and never reports syncState for a root sheet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.staleness import compute_sync_states
from helpers import initialize_test_source_cells


def _seed_parent() -> dict[str, Any]:
    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Filings",
        "params": {
            "columns": [
                {"name": "title", "type": "text"},
                {"name": "rows_json", "type": "json"},
            ],
            "rows": [
                {
                    "title": "Q1",
                    "rows_json": [{"vendor": "Acme"}, {"vendor": "Globex"}],
                },
                {"title": "Q2", "rows_json": [{"vendor": "Initech"}]},
            ],
            "source": {
                "kind": "inline",
                "label": "staleness seed",
                "fingerprint": "sha256:staleness-seed",
            },
        },
        "idempotency_key": "staleness_seed@sha256:v1",
    }


def _derive_column(
    *, sheet_id: int, column_id: int, target: str, key: str
) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": target,
        "params": {
            "source": {"kind": "column", "sheet_id": sheet_id, "column_id": column_id},
        },
        "idempotency_key": key,
    }


def _cell_edit(*, row_id: int, column_id: int, value: Any, key: str) -> dict[str, Any]:
    return {
        "action_id": "cell.edit",
        "scope": {"kind": "project"},
        "params": {
            "edits": [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": value,
                }
            ]
        },
        "idempotency_key": key,
    }


def _ids(project: Project, sheet_name: str) -> tuple[int, int, list[int]]:
    sheet_id = int(
        project.db.execute(
            "SELECT id FROM sheets WHERE name=?", (sheet_name,)
        ).fetchone()["id"]
    )
    col_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='rows_json'", (sheet_id,)
        ).fetchone()["id"]
    )
    row_ids = [
        int(r["id"])
        for r in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        )
    ]
    return sheet_id, col_id, row_ids


def _state(project: Project, sheet_name: str) -> dict[str, str | None] | None:
    sid = int(
        project.db.execute(
            "SELECT id FROM sheets WHERE name=?", (sheet_name,)
        ).fetchone()["id"]
    )
    return compute_sync_states(project).get(sid)


def _build(tmp_path: Path) -> Project:
    project = Project.create(tmp_path / "stale.frisket", name="Stale")
    run_action_spec(project, _seed_parent(), project_id="p-stale")
    parent_id, col_id, _ = _ids(project, "Filings")
    run_action_spec(
        project,
        _derive_column(
            sheet_id=parent_id, column_id=col_id, target="Vendors", key="d1@v1"
        ),
        project_id="p-stale",
    )
    return project


def test_root_sheets_have_no_sync_state_and_fresh_derive_is_synced(
    tmp_path: Path,
) -> None:
    project = _build(tmp_path)
    try:
        states = compute_sync_states(project)
        root_id = int(
            project.db.execute("SELECT id FROM sheets WHERE name='Filings'").fetchone()[
                "id"
            ]
        )
        # Root sheet is absent entirely -- syncState is meaningless without a parent.
        assert root_id not in states
        vendors = _state(project, "Vendors")
        assert vendors == {"sync_state": "synced", "stale_reason": None}
    finally:
        project.close()


def test_parent_cell_edit_marks_child_stale(tmp_path: Path) -> None:
    project = _build(tmp_path)
    try:
        _, col_id, row_ids = _ids(project, "Filings")
        run_action_spec(
            project,
            _cell_edit(
                row_id=row_ids[0],
                column_id=col_id,
                value=[{"vendor": "Acme"}, {"vendor": "NEW"}],
                key="edit1@v1",
            ),
            project_id="p-stale",
        )
        assert _state(project, "Vendors") == {
            "sync_state": "stale",
            "stale_reason": "parent_changed",
        }
    finally:
        project.close()


def test_undo_of_parent_edit_recomputes_synced(tmp_path: Path) -> None:
    project = _build(tmp_path)
    try:
        _, col_id, row_ids = _ids(project, "Filings")
        run_action_spec(
            project,
            _cell_edit(
                row_id=row_ids[0], column_id=col_id, value=[{"vendor": "X"}], key="e@v1"
            ),
            project_id="p-stale",
        )
        assert _state(project, "Vendors")["sync_state"] == "stale"
        # Undo rewinds op_cursor and flips the edit op to 'undone'; the resolver
        # recomputes from live op status and must NOT trust a stale flag.
        undone = project.undo()
        assert undone is not None
        assert _state(project, "Vendors") == {
            "sync_state": "synced",
            "stale_reason": None,
        }
    finally:
        project.close()


def test_grandchild_cascade_from_root_edit(tmp_path: Path) -> None:
    project = _build(tmp_path)
    try:
        vendors_id = int(
            project.db.execute("SELECT id FROM sheets WHERE name='Vendors'").fetchone()[
                "id"
            ]
        )
        # The child's 'vendor' column is scalar text, not JSON -- add a JSON list
        # column on the child so a grandchild derive has a list source.
        child_json_col = project.add_column(vendors_id, "tags", type="json")
        initialize_test_source_cells(
            project,
            (
                (int(row["id"]), child_json_col, [{"k": "v"}])
                for row in project.db.execute(
                    "SELECT id FROM rows WHERE sheet_id=?", (vendors_id,)
                ).fetchall()
            ),
            label="seed child JSON",
        )
        run_action_spec(
            project,
            _derive_column(
                sheet_id=vendors_id,
                column_id=child_json_col,
                target="Tags",
                key="d2@v1",
            ),
            project_id="p-stale",
        )
        assert _state(project, "Tags")["sync_state"] == "synced"

        _, root_col, row_ids = _ids(project, "Filings")
        run_action_spec(
            project,
            _cell_edit(
                row_id=row_ids[0],
                column_id=root_col,
                value=[{"vendor": "Z"}],
                key="edit_root@v1",
            ),
            project_id="p-stale",
        )
        # Editing the ROOT must cascade staleness down to the grandchild (Tags),
        # whose ancestors transitively include Filings.
        assert _state(project, "Tags")["sync_state"] == "stale"
        assert _state(project, "Vendors")["sync_state"] == "stale"
    finally:
        project.close()


def test_review_flip_on_ancestor_marks_descendant_stale(tmp_path: Path) -> None:
    project = _build(tmp_path)
    try:
        parent_id, _, row_ids = _ids(project, "Filings")
        # Seed a completed AI run + an unreviewed result on the parent, then flip it
        # verified through the real review.decision action. A derive's _approved_rows
        # filters on review_state='verified', so a flip changes what it would include
        # -> descendants stale.
        ai_col = project.add_column(parent_id, "score", type="text", ai_generated=True)
        op_id = project.append_op("map.judge", {"sheet_id": parent_id})
        cur = project.db.execute(
            "INSERT INTO runs (op_id, sheet_id, action_kind, status, model) "
            "VALUES (?, ?, 'map.judge', 'completed', 'stub/model')",
            (op_id, parent_id),
        )
        run_id = int(cur.lastrowid)
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?", (run_id, ai_col)
        )
        project.db.execute(
            "INSERT INTO results (run_id, row_id, column_id, value, review_state) "
            "VALUES (?, ?, ?, '\"ok\"', 'unreviewed')",
            (run_id, row_ids[0], ai_col),
        )
        project.db.commit()
        # Refresh the child's watermark past this seed so it starts synced.
        project.db.execute(
            "UPDATE sheets SET last_verified_op_cursor=? WHERE name='Vendors'",
            (project.op_cursor,),
        )
        project.db.commit()
        assert _state(project, "Vendors")["sync_state"] == "synced"

        run_action_spec(
            project,
            {
                "action_id": "review.decision",
                "scope": {"kind": "project"},
                "params": {
                    "run_id": run_id,
                    "row_id": row_ids[0],
                    "column_id": ai_col,
                    "decision": "accept",
                },
                "idempotency_key": "review1@v1",
            },
            project_id="p-stale",
        )
        assert _state(project, "Vendors")["sync_state"] == "stale"
    finally:
        project.close()


def test_ancestry_is_acyclic_by_construction(tmp_path: Path) -> None:
    project = _build(tmp_path)
    try:
        # parent_sheet_id is write-once (append-only forest); no sheet is its own
        # ancestor. Assert the invariant holds on the built graph.
        rows = project.db.execute("SELECT id, parent_sheet_id FROM sheets").fetchall()
        parent_of = {int(r["id"]): r["parent_sheet_id"] for r in rows}
        for sid in parent_of:
            seen = set()
            cur = sid
            while parent_of.get(cur) is not None:
                cur = int(parent_of[cur])
                assert cur not in seen, "cycle detected in sheet forest"
                assert cur != sid, "sheet is its own ancestor"
                seen.add(cur)
    finally:
        project.close()

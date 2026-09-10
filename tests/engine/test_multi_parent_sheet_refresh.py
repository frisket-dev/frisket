from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import actions as executor_actions
from frisket.engine.store import Project
from frisket.engine.store.sheet_lifecycle import SheetDeleteBlocked
from frisket.engine.store.staleness import compute_sync_states


PROJECT_ID = "project-multiparent-refresh"
_KEYGEN = itertools.count(1)


def _run(project: Project, action: dict[str, Any]):
    return executor_actions.run_action_spec(project, action, project_id=PROJECT_ID)


def _seed_join(
    project_path: Path, *, output_names=None, selected_left_only=False
) -> dict[str, Any]:
    """Seed two sheets and materialize an inner ``derive.join`` child.

    Returns the parent ids/columns/rows and the join child sheet id.
    """
    project = Project.create(project_path, name="Multi-parent refresh")
    left_id = project.add_sheet("States")
    left_cols = {
        "state_fips": project.add_column(left_id, "state_fips", type="integer"),
        "state_name": project.add_column(left_id, "state_name", type="text"),
    }
    left_rows = project.add_rows(
        left_id,
        [
            {"state_fips": 1, "state_name": "Alabama"},
            {"state_fips": 2, "state_name": "Alaska"},
            {"state_fips": 6, "state_name": "California"},
        ],
        left_cols,
    )
    right_id = project.add_sheet("Population")
    right_cols = {
        "state_fips": project.add_column(right_id, "state_fips", type="integer"),
        "population": project.add_column(right_id, "population", type="integer"),
    }
    right_rows = project.add_rows(
        right_id,
        [
            {"state_fips": 1, "population": 5024279},
            {"state_fips": 2, "population": 733391},
            {"state_fips": 6, "population": 39538223},
        ],
        right_cols,
    )
    project.db.commit()

    join = _run(
        project,
        {
            "action_id": "derive.join",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": left_id,
                **({"row_ids": left_rows[:1]} if selected_left_only else {}),
            },
            "sheet_name": "Joined",
            **({"output_names": output_names} if output_names else {}),
            "params": {
                "right": {"sheet_id": right_id},
                "join_keys": [
                    {"left_column": "state_fips", "right_column": "state_fips"}
                ],
                "how": "inner",
                "indicator": True,
            },
            "idempotency_key": f"seed_join@sha256:{next(_KEYGEN)}",
        },
    )
    assert join.status == "completed", [e.model_dump() for e in join.errors]
    child_id = int(
        project.db.execute(
            "SELECT id FROM sheets WHERE name='Joined' AND hidden=0"
        ).fetchone()["id"]
    )
    return {
        "project": project,
        "left_id": left_id,
        "right_id": right_id,
        "left_cols": left_cols,
        "right_cols": right_cols,
        "left_rows": left_rows,
        "right_rows": right_rows,
        "child_id": child_id,
    }


def _refresh(
    *,
    sheet_id: int,
    key: str,
    confirmation: str | None = None,
) -> dict[str, Any]:
    return {
        "action_id": "sheet.refresh",
        "scope": {"kind": "project"},
        "params": {"sheet_id": sheet_id},
        "idempotency_key": key,
        **({"confirmation": confirmation} if confirmation is not None else {}),
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


def _child_table(project: Project, child_id: int) -> dict[str, list[Any]]:
    cols = [(str(c["name"]), int(c["id"])) for c in project.columns(child_id)]
    row_ids = project.visible_row_ids(child_id)
    return {
        name: [project.get_values(child_id, col_id).get(rid) for rid in row_ids]
        for name, col_id in cols
    }


def _row_count(project: Project, sheet_id: int) -> int:
    return len(project.visible_row_ids(sheet_id))


@pytest.mark.parametrize("how", ["inner", "right"])
def test_empty_or_right_only_join_tracks_both_sources_and_refreshes(tmp_path, how):
    project = Project.create(tmp_path / f"{how}.frisket")
    try:
        left = project.add_sheet("Left")
        left_cols = {"key": project.add_column(left, "key", type="integer")}
        left_row = project.add_rows(left, [{"key": 1}], left_cols)[0]
        right = project.add_sheet("Right")
        right_cols = {"key": project.add_column(right, "key", type="integer")}
        right_row = project.add_rows(right, [{"key": 2}], right_cols)[0]
        project.db.commit()
        created = _run(
            project,
            {
                "action_id": "derive.join",
                "scope": {"kind": "sheet_rows", "sheet_id": left},
                "sheet_name": "Joined",
                "params": {
                    "right": {"sheet_id": right},
                    "join_keys": [{"left_column": "key", "right_column": "key"}],
                    "how": how,
                },
                "idempotency_key": "join",
            },
        )
        assert created.status == "completed", created.errors
        child = created.outputs[0].sheet_id
        assert _child_table(project, child)["key"] == ([] if how == "inner" else [2])
        assert not project.db.execute(
            "SELECT 1 FROM materialized_row_sources m JOIN rows r "
            "ON r.id=m.materialized_row_id WHERE r.sheet_id=? AND m.role='join_left'",
            (child,),
        ).fetchone()
        for source in (left, right):
            assert project.dependent_sheets(source) == [{"id": child, "name": "Joined"}]
            with pytest.raises(SheetDeleteBlocked):
                project.delete_sheet(source)
        edited = _run(
            project,
            _cell_edit(
                row_id=right_row,
                column_id=right_cols["key"],
                value=1,
                key="match-right",
            ),
        )
        assert edited.status == "completed", edited.errors
        assert compute_sync_states(project)[child]["sync_state"] == "stale"
        refreshed = _run(project, _refresh(sheet_id=child, key="refresh"))
        assert refreshed.status == "completed", refreshed.errors
        assert _child_table(project, child)["key"] == [1]
        assert (
            project.db.execute(
                "SELECT parent_row_id FROM rows WHERE sheet_id=?", (child,)
            ).fetchone()[0]
            == left_row
        )
        edited = _run(
            project,
            _cell_edit(
                row_id=left_row, column_id=left_cols["key"], value=3, key="unmatch-left"
            ),
        )
        assert edited.status == "completed", edited.errors
        assert compute_sync_states(project)[child]["sync_state"] == "stale"
    finally:
        project.close()


def test_join_refresh_preserves_requested_names_and_reconciles_source_types(tmp_path):
    ctx = _seed_join(
        tmp_path / "renamed.frisket", output_names={"population": "People"}
    )
    project, child = ctx["project"], ctx["child_id"]
    try:
        before = {c["name"]: c["id"] for c in project.columns(child)}
        assert "People" in before and "population" not in before
        project.db.execute(
            "UPDATE columns SET type='text' WHERE id=?",
            (ctx["right_cols"]["population"],),
        )
        project.db.commit()
        edited = _run(
            project,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "idempotency_key": "source-type-change",
                "params": {
                    "edits": [
                        {
                            "row_id": row,
                            "column_id": ctx["right_cols"]["population"],
                            "value": value,
                        }
                        for row, value in zip(
                            ctx["right_rows"], ["many", "some", "more"], strict=True
                        )
                    ]
                },
            },
        )
        assert edited.status == "completed", edited.errors
        refreshed = _run(project, _refresh(sheet_id=child, key="refresh-type"))
        assert refreshed.status == "completed", refreshed.errors
        after = {c["name"]: c["id"] for c in project.columns(child)}
        assert after == before
        column = next(c for c in project.columns(child) if c["name"] == "People")
        assert column["type"] == "text"
        assert _child_table(project, child)["People"][0] == "many"
    finally:
        project.close()


def test_join_refresh_reuses_selected_left_rows_and_fresh_full_right_sheet(tmp_path):
    ctx = _seed_join(tmp_path / "subset.frisket", selected_left_only=True)
    project, child = ctx["project"], ctx["child_id"]
    try:
        assert _child_table(project, child)["state_name"] == ["Alabama"]
        project.add_rows(
            ctx["left_id"],
            [{"state_fips": 1, "state_name": "Unselected"}],
            ctx["left_cols"],
        )
        project.add_rows(
            ctx["right_id"], [{"state_fips": 1, "population": 123}], ctx["right_cols"]
        )
        project.db.commit()
        refreshed = _run(project, _refresh(sheet_id=child, key="refresh-subset"))
        assert refreshed.status == "completed", refreshed.errors
        table = _child_table(project, child)
        assert table["state_name"] == ["Alabama", "Alabama"]
        assert table["population"] == [5024279, 123]
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Left-parent edit
# ---------------------------------------------------------------------------


def test_refresh_join_after_left_parent_edit(tmp_path: Path) -> None:
    ctx = _seed_join(tmp_path / "left.frisket")
    project = ctx["project"]
    child_id = ctx["child_id"]
    try:
        before = _child_table(project, child_id)
        assert before["state_name"] == ["Alabama", "Alaska", "California"]

        _run(
            project,
            _cell_edit(
                row_id=ctx["left_rows"][0],
                column_id=ctx["left_cols"]["state_name"],
                value="ALABAMA-RENAMED",
                key="edit_left@1",
            ),
        )
        assert compute_sync_states(project)[child_id]["sync_state"] == "stale"

        result = _run(project, _refresh(sheet_id=child_id, key="refresh_left@1"))
        assert result.status == "completed", [e.model_dump() for e in result.errors]
        after = _child_table(project, child_id)
        assert after["state_name"] == ["ALABAMA-RENAMED", "Alaska", "California"]
        # Right-side data unchanged, still coherently joined.
        assert after["population"] == [5024279, 733391, 39538223]
        assert compute_sync_states(project)[child_id]["sync_state"] == "synced"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Right-parent edit
# ---------------------------------------------------------------------------


def test_refresh_join_after_right_parent_edit(tmp_path: Path) -> None:
    ctx = _seed_join(tmp_path / "right.frisket")
    project = ctx["project"]
    child_id = ctx["child_id"]
    try:
        _run(
            project,
            _cell_edit(
                row_id=ctx["right_rows"][2],
                column_id=ctx["right_cols"]["population"],
                value=40000000,
                key="edit_right@1",
            ),
        )
        assert compute_sync_states(project)[child_id]["sync_state"] == "stale"

        result = _run(project, _refresh(sheet_id=child_id, key="refresh_right@1"))
        assert result.status == "completed", [e.model_dump() for e in result.errors]
        after = _child_table(project, child_id)
        assert after["population"] == [5024279, 733391, 40000000]
        assert compute_sync_states(project)[child_id]["sync_state"] == "synced"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Row additions change match counts
# ---------------------------------------------------------------------------


def test_refresh_join_after_row_additions_change_match_counts(tmp_path: Path) -> None:
    ctx = _seed_join(tmp_path / "adds.frisket")
    project = ctx["project"]
    child_id = ctx["child_id"]
    try:
        assert _row_count(project, child_id) == 3  # 3 shared keys, inner

        # A new matching key on BOTH sides (FIPS 12 = Florida) creates a new
        # inner match; a left-only key (FIPS 99) does NOT (inner drops it).
        project.add_rows(
            ctx["left_id"],
            [
                {"state_fips": 12, "state_name": "Florida"},
                {"state_fips": 99, "state_name": "Nowhere"},
            ],
            ctx["left_cols"],
        )
        project.add_rows(
            ctx["right_id"],
            [{"state_fips": 12, "population": 21538187}],
            ctx["right_cols"],
        )
        project.db.commit()

        result = _run(project, _refresh(sheet_id=child_id, key="refresh_adds@1"))
        assert result.status == "completed", [e.model_dump() for e in result.errors]
        after = _child_table(project, child_id)
        assert after["state_name"] == ["Alabama", "Alaska", "California", "Florida"]
        assert _row_count(project, child_id) == 4  # replaced, not appended
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Replace (not append) semantics + id/column-id stability
# ---------------------------------------------------------------------------


def test_refresh_join_replaces_rows_preserving_sheet_and_column_ids(
    tmp_path: Path,
) -> None:
    ctx = _seed_join(tmp_path / "ids.frisket")
    project = ctx["project"]
    child_id = ctx["child_id"]
    try:
        column_ids_before = {
            str(c["name"]): int(c["id"]) for c in project.columns(child_id)
        }
        assert _row_count(project, child_id) == 3

        _run(
            project,
            _cell_edit(
                row_id=ctx["left_rows"][1],
                column_id=ctx["left_cols"]["state_name"],
                value="Alaska-2",
                key="edit_ids@1",
            ),
        )
        result = _run(project, _refresh(sheet_id=child_id, key="refresh_ids@1"))
        assert result.status == "completed", [e.model_dump() for e in result.errors]

        # Same sheet id (in-place refresh).
        still = int(
            project.db.execute(
                "SELECT id FROM sheets WHERE name='Joined' AND hidden=0"
            ).fetchone()["id"]
        )
        assert still == child_id
        # Column ids preserved (reconciled by name, none dropped/re-minted).
        column_ids_after = {
            str(c["name"]): int(c["id"]) for c in project.columns(child_id)
        }
        assert column_ids_after == column_ids_before
        # Replaced not appended.
        assert _row_count(project, child_id) == 3
        assert _child_table(project, child_id)["state_name"] == [
            "Alabama",
            "Alaska-2",
            "California",
        ]
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Receipts differ-but-coherent: fresh match stats on the refresh receipt
# ---------------------------------------------------------------------------


def test_refresh_join_receipt_carries_fresh_match_stats(tmp_path: Path) -> None:
    ctx = _seed_join(tmp_path / "receipt.frisket")
    project = ctx["project"]
    child_id = ctx["child_id"]
    try:
        # Add a new matching key so the fresh stats differ from the original run.
        project.add_rows(
            ctx["left_id"],
            [{"state_fips": 12, "state_name": "Florida"}],
            ctx["left_cols"],
        )
        project.add_rows(
            ctx["right_id"],
            [{"state_fips": 12, "population": 21538187}],
            ctx["right_cols"],
        )
        project.db.commit()

        result = _run(project, _refresh(sheet_id=child_id, key="refresh_receipt@1"))
        assert result.status == "completed", [e.model_dump() for e in result.errors]
        assert result.receipt_id is not None

        body = project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert body is not None
        receipt = json.loads(body["body"])
        stats = [
            ev["ref"]
            for ev in receipt["evidence"]
            if isinstance(ev.get("ref"), dict)
            and ev["ref"].get("kind") == "derive_join_match_stats"
        ]
        assert stats, "refresh receipt must carry fresh derive_join_match_stats"
        assert stats[0]["both"] == 4  # 4 matched keys after the addition
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Fan-out guard stays honest on refresh
# ---------------------------------------------------------------------------


def test_refresh_join_fanout_guard_reconfirms(tmp_path: Path) -> None:
    """A refresh whose fresh projection exceeds ``max_output_rows`` must
    re-confirm, not silently blow past the guard."""
    project = Project.create(tmp_path / "fanout.frisket", name="Fan-out refresh")
    try:
        left_id = project.add_sheet("L")
        left_cols = {"k": project.add_column(left_id, "k", type="integer")}
        project.add_rows(left_id, [{"k": 1}, {"k": 1}], left_cols)
        right_id = project.add_sheet("R")
        right_cols = {"k": project.add_column(right_id, "k", type="integer")}
        project.add_rows(right_id, [{"k": 1}, {"k": 1}], right_cols)
        project.db.commit()

        # Original run: 2x2 = 4 fan-out rows, cap 4 -> fits (no confirmation).
        join = _run(
            project,
            {
                "action_id": "derive.join",
                "scope": {"kind": "sheet_rows", "sheet_id": left_id},
                "sheet_name": "Fan",
                "params": {
                    "right": {"sheet_id": right_id},
                    "join_keys": [{"left_column": "k", "right_column": "k"}],
                    "how": "inner",
                    "max_output_rows": 4,
                },
                "idempotency_key": "seed_fanout@1",
            },
        )
        assert join.status == "completed", [e.model_dump() for e in join.errors]
        child_id = int(
            project.db.execute(
                "SELECT id FROM sheets WHERE name='Fan' AND hidden=0"
            ).fetchone()["id"]
        )
        assert _row_count(project, child_id) == 4

        # Grow the right side: now 2x3 = 6 > cap 4.
        project.add_rows(right_id, [{"k": 1}], right_cols)
        project.db.commit()

        gated = _run(project, _refresh(sheet_id=child_id, key="refresh_fanout@1"))
        assert gated.status == "needs_confirmation", gated.status
        assert gated.errors[0].code == "join_fanout_requires_confirmation"
        promise_set_hash = gated.errors[0].details.get("promise_set_hash")
        assert isinstance(promise_set_hash, str)
        assert len(promise_set_hash) == 64
        # Child rows untouched by the gated refresh.
        assert _row_count(project, child_id) == 4

        bare_confirmed = _run(
            project, _refresh(sheet_id=child_id, key="refresh_fanout@2")
        )
        assert bare_confirmed.status == "needs_confirmation"
        assert _row_count(project, child_id) == 4

        confirmed = _run(
            project,
            _refresh(
                sheet_id=child_id,
                key="refresh_fanout@3",
                confirmation=promise_set_hash,
            ),
        )
        assert confirmed.status == "completed", [
            e.model_dump() for e in confirmed.errors
        ]
        assert _row_count(project, child_id) == 6
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Single-parent refresh untouched (regression guard)
# ---------------------------------------------------------------------------


def test_single_parent_table_from_list_refresh_still_works(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "single.frisket", name="Single")
    try:
        parent_id = project.add_sheet("Filings")
        cols = {
            "title": project.add_column(parent_id, "title", type="text"),
            "rows_json": project.add_column(parent_id, "rows_json", type="json"),
        }
        rows = project.add_rows(
            parent_id,
            [{"title": "Q1", "rows_json": [{"vendor": "Acme"}]}],
            cols,
        )
        project.db.commit()

        derive = _run(
            project,
            {
                "action_id": "derive.table_from_list",
                "scope": {"kind": "project"},
                "sheet_name": "Vendors",
                "params": {
                    "source": {
                        "kind": "column",
                        "sheet_id": parent_id,
                        "column_id": cols["rows_json"],
                    },
                },
                "idempotency_key": "single_derive@1",
            },
        )
        assert derive.status == "completed", [e.model_dump() for e in derive.errors]
        child_id = int(
            project.db.execute("SELECT id FROM sheets WHERE name='Vendors'").fetchone()[
                "id"
            ]
        )

        _run(
            project,
            _cell_edit(
                row_id=rows[0],
                column_id=cols["rows_json"],
                value=[{"vendor": "Acme"}, {"vendor": "NewCo"}],
                key="single_edit@1",
            ),
        )
        result = _run(project, _refresh(sheet_id=child_id, key="single_refresh@1"))
        assert result.status == "completed", [e.model_dump() for e in result.errors]
        vendor_col = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='vendor'", (child_id,)
            ).fetchone()["id"]
        )
        assert list(project.get_values(child_id, vendor_col).values()) == [
            "Acme",
            "NewCo",
        ]
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Unsupported families stay honest
# ---------------------------------------------------------------------------


def test_refresh_nonjoin_multiparent_still_unsupported(tmp_path: Path) -> None:
    """A multi-parent child whose parent op is NOT derive.join (e.g. an
    entity-resolution / reduce / link_table membership) stays
    ``refresh_unsupported`` — only derive.join is lifted in this increment."""
    ctx = _seed_join(tmp_path / "unsupported.frisket")
    project = ctx["project"]
    try:
        # Build a fresh derived child whose parent op is resolve.entities and
        # attach a materialized_row_sources membership row (multi-parent shape).
        other_child = project.add_sheet("Resolved", parent_sheet_id=ctx["left_id"])
        op_id = project.append_op("resolve.entities", {"sheet_id": other_child})
        project.db.execute(
            "UPDATE sheets SET parent_op_id=? WHERE id=?", (op_id, other_child)
        )
        child_row = project.add_rows(other_child, [{}], {})[0]
        project.db.execute(
            "INSERT INTO materialized_row_sources "
            "(materialized_row_id, source_row_id, source_sheet_id, op_id, role) "
            "VALUES (?, ?, ?, ?, 'edge_source')",
            (child_row, ctx["left_rows"][0], ctx["left_id"], op_id),
        )
        project.db.commit()

        result = _run(
            project, _refresh(sheet_id=other_child, key="refresh_unsupported@1")
        )
        assert result.status == "failed"
        assert result.errors[0].code == "refresh_unsupported"
    finally:
        project.close()

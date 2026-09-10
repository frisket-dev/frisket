"""sheet.refresh — in-place derived-sheet refresh (Workbench IA inc 7 backend).

Refresh re-materializes a derived sheet against CURRENT parent data, preserving
sheet identity (id + name) and advancing the staleness watermark. Supported
list/join refreshes are deterministic; only join fanout needs confirmation.
Unsupported multi-parent families still refuse.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.staleness import compute_sync_states


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
                "label": "refresh seed",
                "fingerprint": "sha256:refresh-seed",
            },
        },
        "idempotency_key": "refresh_seed@sha256:v1",
    }


def _derive(*, sheet_id: int, column_id: int) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Vendors",
        "params": {
            "source": {"kind": "column", "sheet_id": sheet_id, "column_id": column_id},
        },
        "idempotency_key": "refresh_derive@sha256:v1",
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


def _build(tmp_path: Path) -> tuple[Project, int, int, int, list[int]]:
    project = Project.create(tmp_path / "refresh.frisket", name="Refresh")
    run_action_spec(project, _seed_parent(), project_id="p-ref")
    parent_id = int(
        project.db.execute("SELECT id FROM sheets WHERE name='Filings'").fetchone()[
            "id"
        ]
    )
    col_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='rows_json'", (parent_id,)
        ).fetchone()["id"]
    )
    run_action_spec(
        project, _derive(sheet_id=parent_id, column_id=col_id), project_id="p-ref"
    )
    child_id = int(
        project.db.execute("SELECT id FROM sheets WHERE name='Vendors'").fetchone()[
            "id"
        ]
    )
    row_ids = [
        int(r["id"])
        for r in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (parent_id,)
        )
    ]
    return project, parent_id, col_id, child_id, row_ids


def _child_vendors(project: Project, child_id: int) -> list[Any]:
    col = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='vendor'", (child_id,)
        ).fetchone()["id"]
    )
    return list(project.get_values(child_id, col).values())


def test_refresh_replaces_rows_in_place_preserving_identity(tmp_path: Path) -> None:
    project, parent_id, col_id, child_id, row_ids = _build(tmp_path)
    try:
        assert _child_vendors(project, child_id) == ["Acme", "Globex", "Initech"]
        # Edit the parent JSON list -> child is now stale.
        run_action_spec(
            project,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {
                    "edits": [
                        {
                            "row_id": row_ids[0],
                            "column_id": col_id,
                            "value": [{"vendor": "Acme"}, {"vendor": "RENAMED"}],
                        }
                    ]
                },
                "idempotency_key": "refresh_edit@v1",
            },
            project_id="p-ref",
        )
        assert compute_sync_states(project)[child_id]["sync_state"] == "stale"

        result = run_action_spec(
            project,
            _refresh(sheet_id=child_id, key="sheet_refresh:c:1"),
            project_id="p-ref",
        )
        assert result.status == "completed", [e.model_dump() for e in result.errors]
        # Same sheet identity (id preserved), rows re-materialized from live parent.
        still = int(
            project.db.execute("SELECT id FROM sheets WHERE name='Vendors'").fetchone()[
                "id"
            ]
        )
        assert still == child_id
        assert _child_vendors(project, child_id) == ["Acme", "RENAMED", "Initech"]
        # Watermark advanced -> synced again.
        assert compute_sync_states(project)[child_id]["sync_state"] == "synced"
        watermark = project.db.execute(
            "SELECT last_verified_op_cursor FROM sheets WHERE id=?", (child_id,)
        ).fetchone()[0]
        assert watermark == project.op_cursor
    finally:
        project.close()


def test_refresh_is_idempotent_on_replay(tmp_path: Path) -> None:
    project, parent_id, col_id, child_id, row_ids = _build(tmp_path)
    try:
        first = run_action_spec(
            project,
            _refresh(sheet_id=child_id, key="sheet_refresh:c:same"),
            project_id="p-ref",
        )
        assert first.status == "completed"
        ops_after_first = int(
            project.db.execute(
                "SELECT COUNT(*) FROM ops WHERE kind='sheet.refresh'"
            ).fetchone()[0]
        )
        replay = run_action_spec(
            project,
            _refresh(sheet_id=child_id, key="sheet_refresh:c:same"),
            project_id="p-ref",
        )
        assert replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        # Replay served from the receipt -> no second refresh op.
        assert (
            int(
                project.db.execute(
                    "SELECT COUNT(*) FROM ops WHERE kind='sheet.refresh'"
                ).fetchone()[0]
            )
            == ops_after_first
        )
    finally:
        project.close()


def test_refresh_reuses_typed_runtime_output_names_and_column_ids(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "renamed.frisket")
    try:
        imported = run_action_spec(project, _seed_parent(), project_id="p-ref")
        parent = imported.outputs[0].ref
        action = _derive(
            sheet_id=parent["sheet_id"], column_id=parent["columns"]["rows_json"]
        )
        action["output_names"] = {"vendor": "Supplier"}
        created = run_action_spec(project, action, project_id="p-ref")
        assert created.status == "completed", created.errors
        child_id = next(
            output.sheet_id for output in created.outputs if output.kind == "sheet"
        )
        before_columns = [
            tuple(row)
            for row in project.db.execute(
                "SELECT id, name, type, ai_generated, hidden, format FROM columns WHERE sheet_id=? ORDER BY position",
                (child_id,),
            )
        ]
        assert [column[1] for column in before_columns] == ["Supplier"]
        source_row = project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
            (parent["sheet_id"],),
        ).fetchone()[0]
        edited = run_action_spec(
            project,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {
                    "edits": [
                        {
                            "row_id": source_row,
                            "column_id": parent["columns"]["rows_json"],
                            "value": [{"vendor": "Changed"}],
                        }
                    ]
                },
                "idempotency_key": "rename-source-edit",
            },
            project_id="p-ref",
        )
        assert edited.status == "completed", edited.errors
        refreshed = run_action_spec(
            project,
            _refresh(sheet_id=child_id, key="renamed-refresh"),
            project_id="p-ref",
        )
        assert refreshed.status == "completed", refreshed.errors
        after_columns = [
            tuple(row)
            for row in project.db.execute(
                "SELECT id, name, type, ai_generated, hidden, format FROM columns WHERE sheet_id=? ORDER BY position",
                (child_id,),
            )
        ]
        assert after_columns == before_columns
        assert list(project.get_values(child_id, before_columns[0][0]).values()) == [
            "Changed",
            "Initech",
        ]
    finally:
        project.close()


def test_refresh_of_root_sheet_is_unsupported(tmp_path: Path) -> None:
    project, parent_id, _, _, _ = _build(tmp_path)
    try:
        result = run_action_spec(
            project,
            _refresh(sheet_id=parent_id, key="sheet_refresh:root:1"),
            project_id="p-ref",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "refresh_root_sheet_unsupported"
    finally:
        project.close()


def test_refresh_refuses_unsupported_model_op_without_cost_prompt(
    tmp_path: Path,
) -> None:
    project, parent_id, _, _, _ = _build(tmp_path)
    try:
        # Historical provider work does not authorize unsupported rematerialization.
        op_id = project.append_op("reduce.group_summary", {"sheet_id": parent_id})
        project.db.execute(
            "INSERT INTO runs (op_id, sheet_id, action_kind, status, model, cost_estimate) "
            "VALUES (?, ?, 'reduce.group_summary', 'completed', 'stub/model', 0.42)",
            (op_id, parent_id),
        )
        model_child = project.add_sheet(
            "Summary", parent_sheet_id=parent_id, parent_op_id=op_id
        )
        project.db.commit()

        before = tuple(project.db.iterdump())
        refused = run_action_spec(
            project,
            _refresh(sheet_id=model_child, key="sheet_refresh:m:1"),
            project_id="p-ref",
        )
        assert refused.status == "failed"
        assert refused.errors[0].code == "refresh_unsupported"
        assert tuple(project.db.iterdump()) == before
    finally:
        project.close()


@pytest.mark.parametrize("confirmation", [None, "0" * 64])
@pytest.mark.parametrize("historical_cost", [0.42, 0.0, None])
def test_list_refresh_ignores_historical_model_cost(
    tmp_path: Path, monkeypatch, confirmation, historical_cost
) -> None:
    from frisket.execution import pricing_policy

    project, _, col_id, child_id, row_ids = _build(tmp_path)
    try:
        parent_op_id = project.db.execute(
            "SELECT parent_op_id FROM sheets WHERE id=?", (child_id,)
        ).fetchone()[0]
        project.db.execute(
            "INSERT INTO runs "
            "(op_id, sheet_id, action_kind, status, model, total_rows, cost_estimate) "
            "VALUES (?, ?, 'derive.table_from_list', 'completed', 'stub/model', 3, ?)",
            (parent_op_id, child_id, historical_cost),
        )
        project.db.commit()
        edited = run_action_spec(
            project,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {
                    "edits": [
                        {
                            "row_id": row_ids[0],
                            "column_id": col_id,
                            "value": [{"vendor": "CHANGED"}],
                        }
                    ]
                },
                "idempotency_key": "historical-cost-edit",
            },
            project_id="p-ref",
        )
        assert edited.status == "completed", edited.errors
        historical_runs = tuple(project.db.execute("SELECT * FROM runs"))
        model_calls = tuple(project.db.execute("SELECT * FROM model_calls"))

        def forbid_pricing():
            pytest.fail("deterministic refresh must not consult model pricing")

        monkeypatch.setattr(pricing_policy, "default_pricing_policy", forbid_pricing)
        request = _refresh(
            sheet_id=child_id, key="historical-cost", confirmation=confirmation
        )
        refreshed = run_action_spec(project, request, project_id="p-ref")
        assert refreshed.status == "completed", refreshed.errors
        assert refreshed.outputs[0].sheet_id == child_id
        assert _child_vendors(project, child_id) == ["CHANGED", "Initech"]
        assert tuple(project.db.execute("SELECT * FROM runs")) == historical_runs
        assert tuple(project.db.execute("SELECT * FROM model_calls")) == model_calls
        after = tuple(project.db.iterdump())
        replay = run_action_spec(project, request, project_id="p-ref")
        assert replay.receipt_id == refreshed.receipt_id
        assert tuple(project.db.iterdump()) == after
    finally:
        project.close()


def test_refresh_lease_blocks_concurrent_refresh(tmp_path: Path) -> None:
    project, parent_id, col_id, child_id, row_ids = _build(tmp_path)
    try:
        # Simulate an in-flight refresh by planting a live (unexpired) lease.
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        project.db.execute(
            "UPDATE sheets SET refresh_claim_token='other', "
            "refresh_lease_expires_at=? WHERE id=?",
            (future, child_id),
        )
        project.db.commit()

        busy = run_action_spec(
            project,
            _refresh(sheet_id=child_id, key="sheet_refresh:c:busy"),
            project_id="p-ref",
        )
        assert busy.status == "failed"
        assert busy.errors[0].code == "sheet_refresh_busy"
    finally:
        project.close()


def test_refresh_multiparent_sheet_is_unsupported(tmp_path: Path) -> None:
    project, parent_id, col_id, child_id, row_ids = _build(tmp_path)
    try:
        # Attach a materialized_row_sources membership row so the child looks
        # multi-parent (join/resolve/reduce semantics).
        child_row = int(
            project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? LIMIT 1", (child_id,)
            ).fetchone()["id"]
        )
        source_row = row_ids[0]
        op_id = project.append_op("join.semantic", {"sheet_id": child_id})
        project.db.execute(
            "INSERT INTO materialized_row_sources "
            "(materialized_row_id, source_row_id, source_sheet_id, op_id, role) "
            "VALUES (?, ?, ?, ?, 'edge_source')",
            (child_row, source_row, parent_id, op_id),
        )
        project.db.commit()

        result = run_action_spec(
            project,
            _refresh(sheet_id=child_id, key="sheet_refresh:mp:1"),
            project_id="p-ref",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "refresh_unsupported"
    finally:
        project.close()

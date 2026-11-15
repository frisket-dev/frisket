"""Derived refresh stages identities before any bounded contribution writes."""

import gc
import weakref

from frisket.engine.store.derived_refresh_stage import (
    DerivedRefreshStage,
    REFRESH_BATCH_SIZE,
)
from frisket.engine.store.derived_updates import (
    DerivedRefreshRow,
    reconcile_rows_into_open_change,
    refresh_config,
)
from frisket.engine.store.changes import ChangeStore
from frisket.engine.store.materialization import MaterializedColumnSpec
from test_sheet_refresh import _build, _child_vendors


def test_disk_stage_drops_input_objects_and_validates_duplicates_across_batches():
    with DerivedRefreshStage(keyed=True) as stage:
        row = DerivedRefreshRow({"value": "x" * 4096}, key="first")
        ref = weakref.ref(row)
        stage.add(row)
        del row
        gc.collect()
        assert ref() is None
        for index in range(REFRESH_BATCH_SIZE * 3):
            stage.add(DerivedRefreshRow({"value": str(index)}, key=str(index)))
        stage.add(DerivedRefreshRow({"value": "conflicting"}, key="first"))
        stage.add(DerivedRefreshRow({"value": "0"}, key="0"))
        stage.seal()
        assert stage.row_count == REFRESH_BATCH_SIZE * 3
        assert stage.contains(keys=["first", "absent"]) == {"first"}
        sizes = []
        for batch in stage.batches():
            sizes.append(len(batch))
            assert all(row.key != "first" for _, row, _ in batch)
        assert sizes == [REFRESH_BATCH_SIZE] * 3


def test_partial_refresh_keeps_prior_members_and_undo_restores_complete_batch(tmp_path):
    project, _, _, child, parents = _build(tmp_path)
    try:
        parent = project.db.execute(
            "SELECT o.* FROM ops o JOIN sheets s ON s.parent_op_id=o.id WHERE s.id=?",
            (child,),
        ).fetchone()
        config = refresh_config(parent)
        project.db.execute("BEGIN IMMEDIATE")
        op = project.append_op("sheet.refresh", {"sheet_id": child}, commit=False)
        ChangeStore(project).start(op)
        project.db.commit()
        with DerivedRefreshStage(keyed=False) as stage:
            for index in range(REFRESH_BATCH_SIZE + 1):
                stage.add(
                    DerivedRefreshRow(
                        {"vendor": f"new {index}"}, parent_row_id=parents[0]
                    )
                )
            stage.seal()
            batch = next(stage.batches())
            project.db.execute("BEGIN IMMEDIATE")
            written = reconcile_rows_into_open_change(
                project.db,
                op_id=op,
                sheet_id=child,
                config=config,
                columns=[MaterializedColumnSpec("vendor", "text")],
                rows=[row for _, row, _ in batch],
            )
            project.db.commit()
            stage.record_published(written)
            # The last update was never processed: no absence pass is authorized.
            project.db.execute("BEGIN IMMEDIATE")
            ChangeStore(project).close(op)
            project.db.commit()
        assert _child_vendors(project, child)[:3] == ["Acme", "Globex", "Initech"]
        assert len(_child_vendors(project, child)) == 3 + REFRESH_BATCH_SIZE
        project.undo()
        assert _child_vendors(project, child) == ["Acme", "Globex", "Initech"]
        project.redo()
        assert len(_child_vendors(project, child)) == 3 + REFRESH_BATCH_SIZE
    finally:
        project.close()


def _edit_items(project, column, parent, count):
    project.apply_edits(
        [
            {
                "row_id": parent,
                "column_id": column,
                "value": [{"vendor": f"item {index}"} for index in range(count)],
            }
        ]
    )


def test_action_publishes_bounded_batches_without_holding_lock_between_them(
    tmp_path, monkeypatch
):
    import sqlite3
    from frisket.engine.executor import ExecutorDeps, run_action_spec
    import frisket.engine.store.derived_updates as updates
    from test_sheet_refresh import _refresh

    project, _, column, child, parents = _build(tmp_path)
    try:
        _edit_items(project, column, parents[0], REFRESH_BATCH_SIZE * 2)
        actual = updates.reconcile_rows_into_open_change
        writes = []
        stage_add = DerivedRefreshStage.add

        def verify_stage(self, row, **kwargs):
            assert not project.db.in_transaction
            return stage_add(self, row, **kwargs)

        def verify_batch(db, **kwargs):
            assert db.in_transaction
            writes.append(len(kwargs["rows"]))
            assert len(kwargs["rows"]) <= REFRESH_BATCH_SIZE
            return actual(db, **kwargs)

        monkeypatch.setattr(DerivedRefreshStage, "add", verify_stage)
        monkeypatch.setattr(updates, "reconcile_rows_into_open_change", verify_batch)
        checks = []
        db_path = project.db.execute("PRAGMA database_list").fetchone()[2]

        def cancel_after_first_batch():
            assert not project.db.in_transaction
            with sqlite3.connect(db_path, timeout=0) as other:
                other.execute("BEGIN IMMEDIATE")  # succeeds: previous batch committed
                other.rollback()
            checks.append(True)
            return len(checks) == 2

        result = run_action_spec(
            project,
            _refresh(sheet_id=child, key="bounded-cancel"),
            project_id="p",
            deps=ExecutorDeps(cancelled=cancel_after_first_batch),
        )
        assert result.status == "cancelled", result.errors
        assert writes == [REFRESH_BATCH_SIZE]
        import json

        op = project.db.execute(
            "SELECT id,spec FROM ops WHERE kind='sheet.refresh' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        receipt_id = json.loads(op["spec"])["refresh_receipt_id"]
        assert receipt_id == result.receipt_id
        capture = project.db.execute(
            "SELECT capture_json FROM consumed_source_bundles WHERE owner_op_id=? AND output_sheet_id=?",
            (op["id"], child),
        ).fetchone()
        assert capture is not None  # cancellation must retain actual read facts
        assert not json.loads(capture[0]).get("incomplete", False)
        assert len(_child_vendors(project, child)) == 3 + REFRESH_BATCH_SIZE
        assert _child_vendors(project, child)[:3] == ["Acme", "Globex", "Initech"]
        assert not project.db.in_transaction
        project.undo()
        assert _child_vendors(project, child) == ["Acme", "Globex", "Initech"]
    finally:
        project.close()


def test_cancellation_during_absence_keeps_completed_retirements_and_undo_restores(
    tmp_path,
):
    from frisket.engine.executor import ExecutorDeps, run_action_spec
    from test_sheet_refresh import _refresh

    project, _, column, child, parents = _build(tmp_path)
    try:
        _edit_items(project, column, parents[0], REFRESH_BATCH_SIZE * 2)
        first = run_action_spec(
            project, _refresh(sheet_id=child, key="populate"), project_id="p"
        )
        assert first.status == "completed", first.errors
        original = project.visible_row_ids(child)
        assert len(original) == REFRESH_BATCH_SIZE * 2 + 1
        _edit_items(project, column, parents[0], 0)
        calls = 0

        def stop():
            nonlocal calls
            calls += 1
            return calls == 3  # one incoming batch, one absent batch, then cancel

        result = run_action_spec(
            project,
            _refresh(sheet_id=child, key="retire"),
            project_id="p",
            deps=ExecutorDeps(cancelled=stop),
        )
        assert result.status == "cancelled", result.errors
        remaining = project.visible_row_ids(child)
        assert len(set(original) - set(remaining)) == REFRESH_BATCH_SIZE
        assert len(set(remaining) - set(original)) == 1
        project.undo()
        assert project.visible_row_ids(child) == original
    finally:
        project.close()

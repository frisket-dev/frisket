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

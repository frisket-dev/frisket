"""Actual read partitions and immutable identity, through real store seams."""

from contextlib import contextmanager

from frisket.engine.store import Project
from frisket.engine.store.changes import ChangeStore, start_change
from frisket.engine.store.current_cells import refresh_current_cells
from frisket.engine.store.consumed_sources import (
    bundle_state,
    read_consumed_source,
    store_capture,
    verify_bundle,
)


def _seed(tmp_path, count=4):
    project = Project.create(tmp_path / "sources.frisket")
    sheet = project.add_sheet("source")
    column = project.add_column(sheet, "input")
    unrelated = project.add_column(sheet, "unrelated")
    rows = project.add_rows(
        sheet, [{"input": f"v{i}"} for i in range(count)], {"input": column}
    )
    owner = project.append_change("test.output", {})
    project.db.execute("BEGIN IMMEDIATE")
    start_change(project.db, owner)
    project.db.commit()
    return project, sheet, column, unrelated, rows, owner


def _save(project, capture, sheet, owner):
    project.db.execute("BEGIN IMMEDIATE")
    identity = store_capture(project, capture, owner_op_id=owner, output_sheet_id=sheet)
    project.db.commit()
    return identity


def _transition(project, op, column, row, *, restore=False):
    project.db.execute("BEGIN IMMEDIATE")
    change = ChangeStore(project)
    (change.restore if restore else change.revert)(op)
    refresh_current_cells(project.db, column_ids=[column], row_ids=[row])
    project.db.commit()


def test_mixed_batches_compare_the_actual_consumed_state_and_undo_restores_it(
    tmp_path, monkeypatch
):
    project, sheet, column, _, rows, owner = _seed(tmp_path)
    try:
        read_snapshot = project.read_snapshot
        reads = 0
        changes = []

        @contextmanager
        def interleaved_read():
            nonlocal reads
            with read_snapshot() as snapshot:
                yield snapshot
            reads += 1
            if reads == 1:
                changes.append(
                    project.apply_edits(
                        [
                            {
                                "row_id": rows[0],
                                "column_id": column,
                                "value": "late first",
                            }
                        ]
                    )
                )
                changes.append(
                    project.apply_edits(
                        [{"row_id": rows[-1], "column_id": column, "value": "new last"}]
                    )
                )

        monkeypatch.setattr(project, "read_snapshot", interleaved_read)
        _, values, capture = read_consumed_source(
            project, sheet, [column], batch_size=2
        )
        monkeypatch.setattr(project, "read_snapshot", read_snapshot)
        assert values[column][rows[0]] == "v0"
        assert values[column][rows[-1]] == "new last"
        assert capture["coherent"] is False
        bundle = _save(project, capture, sheet, owner)
        assert verify_bundle(project, bundle) == "stale"
        _transition(project, changes[0], column, rows[0])
        assert verify_bundle(project, bundle) == "fresh"
        _transition(project, changes[1], column, rows[-1])
        assert verify_bundle(project, bundle) == "stale"
        _transition(project, changes[1], column, rows[-1], restore=True)
        assert verify_bundle(project, bundle) == "fresh"
    finally:
        project.close()


def test_equal_text_new_contribution_is_stale_and_unrelated_column_is_clean(
    tmp_path, monkeypatch
):
    project, sheet, column, unrelated, rows, owner = _seed(tmp_path)
    try:
        _, _, capture = read_consumed_source(project, sheet, [column])
        bundle = _save(project, capture, sheet, owner)
        project.apply_edits(
            [{"row_id": rows[0], "column_id": unrelated, "value": "different"}]
        )
        # Clean guards share the same optimization for refs and fingerprints.
        import frisket.engine.store.consumed_sources as captures

        monkeypatch.setattr(
            captures,
            "_verify_aggregate",
            lambda *_: (_ for _ in ()).throw(AssertionError("clean capture scanned")),
        )
        assert bundle_state(project, bundle) == "fresh"
        monkeypatch.undo()
        edit = project.apply_edits(
            [{"row_id": rows[0], "column_id": column, "value": "v0"}]
        )
        assert verify_bundle(project, bundle) == "stale"
        _transition(project, edit, column, rows[0])
        assert verify_bundle(project, bundle) == "fresh"
    finally:
        project.close()


def test_whole_scope_and_explicit_cohort_have_distinct_append_policy(tmp_path):
    project, sheet, column, _, rows, owner = _seed(tmp_path)
    try:
        _, _, whole = read_consumed_source(project, sheet, [column])
        _, _, selected = read_consumed_source(
            project, sheet, [column], row_ids=rows, scope_intent="selected"
        )
        whole_id = _save(project, whole, sheet, owner)
        second_owner = project.append_change("test.selected", {})
        selected_id = _save(project, selected, sheet, second_owner)
        project.add_rows(sheet, [{"input": "appended"}], {"input": column})
        assert verify_bundle(project, whole_id) == "stale"
        assert verify_bundle(project, selected_id) == "fresh"
    finally:
        project.close()


def test_capture_rolls_back_with_result_transaction(tmp_path):
    project, sheet, column, _, _, owner = _seed(tmp_path)
    try:
        _, _, capture = read_consumed_source(project, sheet, [column])
        project.db.execute("BEGIN IMMEDIATE")
        store_capture(project, capture, owner_op_id=owner, output_sheet_id=sheet)
        project.db.rollback()
        assert (
            project.db.execute(
                "SELECT count(*) FROM consumed_source_bundles"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_earlier_writer_publishing_after_capture_invalidates_actual_identity(tmp_path):
    from frisket.engine.store.cell_writes import (
        BaseCellWrite,
        create_base_cell_producer,
        replace_base_cells,
    )

    project, sheet, column, _, rows, owner = _seed(tmp_path)
    try:
        writer = project.append_change("test.delayed_writer", {})
        project.db.execute("BEGIN IMMEDIATE")
        start_change(project.db, writer, writing=True)
        producer = create_base_cell_producer(
            project.db, stage_id="delayed", op_id=writer
        )
        project.db.commit()
        capture_owner = project.append_change("test.later_capture", {})
        assert writer < capture_owner
        _, _, capture = read_consumed_source(project, sheet, [column])
        bundle = _save(project, capture, sheet, capture_owner)
        assert bundle_state(project, bundle) == "fresh"
        project.db.execute("BEGIN IMMEDIATE")
        replace_base_cells(
            project.db,
            producer_id=producer,
            column_ids=[column],
            row_ids=[rows[0]],
            cells=[BaseCellWrite(rows[0], column, "published late")],
        )
        project.db.commit()
        assert project.get_values(sheet, column)[rows[0]] == "published late"
        assert bundle_state(project, bundle) == "stale"
        _transition(project, writer, column, rows[0])
        assert bundle_state(project, bundle) == "fresh"
    finally:
        project.close()


def test_equal_descriptor_value_from_new_owner_is_stale_until_reverted(tmp_path):
    from frisket.engine.store.column_descriptors import record_descriptor_changes

    project, sheet, column, _, rows, owner = _seed(tmp_path)
    try:
        _, _, capture = read_consumed_source(project, sheet, [column])
        bundle = _save(project, capture, sheet, owner)
        descriptor_change = project.append_change("column.retype", {})
        project.db.execute("BEGIN IMMEDIATE")
        start_change(project.db, descriptor_change, writing=True)
        info = {
            "column_types": {str(column): "text"},
            "column_types_after": {str(column): "text"},
        }
        project.set_undo_info(descriptor_change, info)
        record_descriptor_changes(project.db, descriptor_change, info)
        refresh_current_cells(project.db, column_ids=[column], row_ids=rows)
        ChangeStore(project).close(descriptor_change)
        project.db.commit()
        assert bundle_state(project, bundle) == "stale"
        _transition(project, descriptor_change, column, rows[0])
        assert bundle_state(project, bundle) == "fresh"
    finally:
        project.close()


def test_pending_and_successful_null_share_result_address_but_not_consumed_state(
    tmp_path,
):
    from frisket.engine.store.result_generations import ResultGenerationStore
    from test_result_generation_store import (
        _seed_project,
        _start_claimed_run,
        _declare,
        _write,
    )

    project, sheet, column, rows = _seed_project(tmp_path, row_count=1)
    try:
        run = _start_claimed_run(
            project,
            sheet_id=sheet,
            output_column_id=column,
            row_ids=rows,
            label="pending input",
        )
        _declare(
            ResultGenerationStore(project), run, column, write_mode="replace_scope"
        )
        refs = {}
        _, _, capture = read_consumed_source(project, sheet, [column], refs_out=refs)
        assert refs[column][rows[0]]["outcome"] == "pending"
        owner = project.append_change("test.pending_consumer", {})
        bundle = _save(project, capture, sheet, owner)
        _write(
            project,
            run,
            [
                {
                    "row_id": rows[0],
                    "column_id": column,
                    "value": None,
                    "publication_effect": "publish_null",
                }
            ],
        )
        from test_result_generation_store import _seal

        _seal(ResultGenerationStore(project), run, column)
        assert verify_bundle(project, bundle) == "stale"
    finally:
        project.close()

"""Read-side acceptance independent of the projection implementation."""

import json
import tracemalloc

import pytest

from frisket.engine.store import Project
from frisket.engine.store.cell_projection import repair_current_cells
from frisket.engine.store.current_value_windows import read_current_column
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.querysets import resolve_sheet_filter_rows
from frisket.server.exports.plan import build_sheet_export_plan, iter_export_row_batches
from helpers import run_writer_authority_fixture
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _release,
    _seal,
    _seed_project,
    _start_claimed_run,
    _write,
)


def _drain(project):
    for _ in range(100):
        if not project.db.execute("SELECT 1 FROM current_cell_repair").fetchone():
            return
        repair_current_cells(project, limit=128)
    pytest.fail("small fixture never completed projection repair")


def _revision(project, column):
    return project.db.execute(
        "SELECT revision FROM cell_authority_revisions WHERE column_id=?", (column,)
    ).fetchone()[0]


def _run(project, sheet, column, rows):
    run = _start_claimed_run(
        project,
        sheet_id=sheet,
        output_column_id=column,
        row_ids=rows,
        label="projection acceptance",
    )
    _declare(ResultGenerationStore(project), run, column, write_mode="replace_scope")
    return run


def test_manual_correction_starts_and_closes_its_own_change(make_project):
    from frisket.engine.store.cell_writes import EditCellWrite, insert_edits

    project = make_project("same-change")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "text")
    row = project.add_rows(sheet, [{"text": "original"}], {"text": column})[0]
    project.db.execute("BEGIN")
    with project.db:
        op = project.append_change("edit", {}, label="manual correction", commit=False)
        insert_edits(
            project.db, op_id=op, edits=[EditCellWrite(row, column, "corrected")]
        )
    state = project.db.execute(
        "SELECT change_order,publication_closed FROM ops WHERE id=?", (op,)
    ).fetchone()
    assert state[0] is not None
    assert state[1] == 1
    values, refs = project.get_values_with_refs(sheet, column, [row])
    assert values[row] == "corrected"
    assert refs[row]["kind"] == "manual_edit"


def test_shared_writers_reject_closed_change_before_dml(make_project):
    from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
    from frisket.engine.store.changes import ChangePublicationClosed

    project = make_project("closed-writers")
    try:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "text")
        row = project.add_rows(sheet, [{"text": "original"}], {"text": column})[0]
        op = project.db.execute(
            "SELECT producer.op_id FROM cells cell JOIN base_cell_producers producer "
            "ON producer.id=cell.producer_id WHERE cell.row_id=? AND cell.column_id=?",
            (row, column),
        ).fetchone()[0]
        before_sheets = project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0]

        project.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ChangePublicationClosed):
            insert_edits(
                project.db,
                op_id=op,
                edits=[EditCellWrite(row, column, "too late")],
            )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM edits WHERE op_id=?", (op,)
            ).fetchone()[0]
            == 0
        )
        project.db.rollback()

        project.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ChangePublicationClosed):
            project.add_sheet("Too late", parent_op_id=op, commit=False)
        assert (
            project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0]
            == before_sheets
        )
        project.db.rollback()

        one_shot = project.append_change("derive.once", {}, label="one-shot")
        created = project.add_sheet("One-shot child", parent_op_id=one_shot)
        assert (
            project.db.execute(
                "SELECT parent_op_id FROM sheets WHERE id=?", (created,)
            ).fetchone()[0]
            == one_shot
        )
        one_shot_state = project.db.execute(
            "SELECT change_order,publication_closed FROM ops WHERE id=?", (one_shot,)
        ).fetchone()
        assert one_shot_state[0] is not None
        assert one_shot_state[1] == 1
    finally:
        if project.db.in_transaction:
            project.db.rollback()
        project.close()


def test_open_change_accepts_multiple_edit_writes_until_owner_closes(make_project):
    from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
    from frisket.engine.store.changes import ChangeStore, start_change

    project = make_project("multi-writer")
    try:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "text")
        rows = project.add_rows(
            sheet, [{"text": "one"}, {"text": "two"}], {"text": column}
        )
        project.db.execute("BEGIN IMMEDIATE")
        op = project.append_change("edit", {}, label="multi-write", commit=False)
        start_change(project.db, op, writing=True)
        child_sheet = project.add_sheet(
            "Owned child",
            parent_sheet_id=sheet,
            parent_op_id=op,
            commit=False,
            close_change=False,
        )
        insert_edits(
            project.db,
            op_id=op,
            edits=[EditCellWrite(rows[0], column, "one edited")],
            close_change=False,
        )
        insert_edits(
            project.db,
            op_id=op,
            edits=[EditCellWrite(rows[1], column, "two edited")],
            close_change=False,
        )
        assert (
            project.db.execute(
                "SELECT publication_closed FROM ops WHERE id=?", (op,)
            ).fetchone()[0]
            == 0
        )
        ChangeStore(project).close(op)
        project.db.commit()
        assert (
            project.db.execute(
                "SELECT publication_closed FROM ops WHERE id=?", (op,)
            ).fetchone()[0]
            == 1
        )
        assert (
            project.db.execute(
                "SELECT parent_op_id FROM sheets WHERE id=?", (child_sheet,)
            ).fetchone()[0]
            == op
        )
        assert project.get_values(sheet, column, rows) == {
            rows[0]: "one edited",
            rows[1]: "two edited",
        }
    finally:
        if project.db.in_transaction:
            project.db.rollback()
        project.close()


def test_dirty_large_text_reads_stay_bounded(make_project):
    project = make_project("large-dirty")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "text")
    rows = project.add_rows(
        sheet,
        [{"text": "a" * (5 * 1024 * 1024)}] + [{"text": "small"}] * 519,
        {"text": column},
    )
    from frisket.engine.store.cell_projection import invalidate_region

    project.db.execute("BEGIN IMMEDIATE")
    invalidate_region(project.db, column, rows[0], rows[-1])
    project.db.commit()
    assert project.db.execute("SELECT 1 FROM current_cell_repair").fetchone()
    tracemalloc.start()
    try:
        values = read_current_column(
            project.db,
            sheet_id=sheet,
            column_id=column,
            row_ids=rows[:1],
            bounded=True,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert values[rows[0]].value_present is False
    assert values[rows[0]].bounded_value.total_bytes == 5 * 1024 * 1024
    assert peak < 2 * 1024 * 1024


def test_dirty_undo_finds_new_filter_matches_and_exports_before_bounded_repair(
    tmp_path,
):
    project, sheet, column, rows = _seed_project(tmp_path, row_count=520)
    try:
        _publish_initial_generation(project, sheet, column, rows)
        run = _run(project, sheet, column, rows)
        _write(
            project,
            run,
            [
                {
                    "row_id": row,
                    "column_id": column,
                    "value": "replacement",
                    "publication_effect": "publish_value",
                }
                for row in rows
            ],
        )
        _seal(ResultGenerationStore(project), run, column)
        _release(project, run)
        _drain(project)
        before_revision = _revision(project, column)
        assert project.undo() == run.op_id
        assert _revision(project, column) > before_revision
        assert project.db.execute("SELECT 1 FROM current_cell_repair").fetchone()
        # Establish that this is genuinely a dirty-cache test, not an eager path.
        assert (
            project.db.execute(
                "SELECT inline_value FROM current_cells WHERE column_id=? AND row_id=?",
                (column, rows[-1]),
            ).fetchone()[0]
            == "replacement"
        )

        def check_readers():
            selected = resolve_sheet_filter_rows(
                project,
                sheet,
                filter_=json.dumps({"generated": {"eq": "first:519"}}),
            )
            assert selected.row_ids == [rows[-1]]
            assert selected.total == 1
            plan = build_sheet_export_plan(project, sheet)
            output_index = plan.column_names.index("generated")
            values = [
                record[output_index]
                for batch in iter_export_row_batches(project, plan, batch_size=101)
                for record in batch.rows
            ]
            assert values == [f"first:{index}" for index in range(520)]

        check_readers()
        revision = _revision(project, column)
        assert repair_current_cells(project, limit=7) <= 7
        assert project.db.execute("SELECT 1 FROM current_cell_repair").fetchone()
        assert _revision(project, column) == revision, "repair is not a new mutation"
        check_readers()
        # A newer write must survive both the old repair remainder and later reads.
        project.apply_edits(
            [
                {
                    "row_id": rows[-1],
                    "column_id": column,
                    "value": "newer edit",
                }
            ]
        )
        _drain(project)
        assert project.get_values(sheet, column)[rows[-1]] == "newer edit"
        assert resolve_sheet_filter_rows(
            project,
            sheet,
            filter_=json.dumps({"generated": {"eq": "newer edit"}}),
        ).row_ids == [rows[-1]]
    finally:
        project.close()


def test_existing_read_snapshot_does_not_mix_new_invalidation_with_old_values(tmp_path):
    project, sheet, column, rows = _seed_project(tmp_path)
    _publish_initial_generation(project, sheet, column, rows)
    other = Project(tmp_path / "result-generations.frisket")
    try:
        project.db.execute("BEGIN")
        original = project.get_values(sheet, column)
        revision = _revision(project, column)
        other.apply_edits(
            [
                {
                    "row_id": rows[0],
                    "column_id": column,
                    "value": "concurrent edit",
                }
            ]
        )
        assert project.get_values(sheet, column) == original
        assert _revision(project, column) == revision
        project.db.commit()
        assert project.get_values(sheet, column)[rows[0]] == "concurrent edit"
        assert _revision(project, column) > revision
    finally:
        other.close()
        project.close()


def test_unpublished_checkpoint_never_becomes_visible_through_dirty_canonical_read(
    tmp_path,
):
    project, sheet, column, rows = _seed_project(tmp_path, row_count=520)
    try:
        _publish_initial_generation(project, sheet, column, rows)
        run = _run(project, sheet, column, rows)
        RunResultStore(project).write_results(
            run.run_id,
            [
                {
                    "row_id": rows[0],
                    "column_id": column,
                    "value": "not yet published",
                    "publication_effect": "publish_value",
                }
            ],
            project_heads=False,
            **run.authority.kwargs(),
        )
        assert project.db.execute("SELECT 1 FROM current_cell_repair").fetchone()
        assert project.get_values(sheet, column)[rows[0]] is None
        assert (
            resolve_sheet_filter_rows(
                project,
                sheet,
                filter_=json.dumps({"generated": {"eq": "not yet published"}}),
            ).total
            == 0
        )
        _drain(project)
        assert project.get_values(sheet, column)[rows[0]] is None
        _seal(ResultGenerationStore(project), run, column)
        assert project.get_values(sheet, column)[rows[0]] == "not yet published"
    finally:
        project.close()


@pytest.mark.parametrize("fail_evidence", [False, True])
def test_output_family_and_evidence_publish_atomically(tmp_path, fail_evidence):
    project, sheet, column, rows = _seed_project(tmp_path)
    other = None
    try:
        _publish_initial_generation(project, sheet, column, rows)
        evidence_column = project.add_column(sheet, "evidence", ai_generated=True)
        op_id = project.append_change("map.extract", label="two-output family")
        store = RunResultStore(project)
        run_id = store.start_run(
            op_id, sheet, "map.extract", row_ids=rows[:1], total_rows=1
        )
        authority = run_writer_authority_fixture(
            project, run_id, output_column_ids={column, evidence_column}
        )
        generations = ResultGenerationStore(project)
        for output, role in ((column, "answer"), (evidence_column, "evidence")):
            generations.declare(
                run_id,
                output,
                output_role=role,
                compatibility_key="sha256:regex-text-v1",
                write_mode="replace_scope" if output == column else "create",
                claim_token=authority.claim_token,
            )
        other = Project(tmp_path / "result-generations.frisket")
        before_revision = _revision(project, column)

        def evidence_writer():
            # A different SQLite connection must never observe half the family,
            # even after both result records exist inside the writing transaction.
            for output in (column, evidence_column):
                assert other.get_values(sheet, output)[rows[0]] is None
            if fail_evidence:
                raise RuntimeError("injected evidence failure")

        def publish():
            store.publish_result_batch(
                run_id,
                [
                    {
                        "row_id": rows[0],
                        "column_id": output,
                        "value": value,
                        "publication_effect": "publish_value",
                    }
                    for output, value in (
                        (column, "answer"),
                        (evidence_column, "citation"),
                    )
                ],
                evidence_writer=evidence_writer,
                **authority.kwargs(),
            )

        if fail_evidence:
            with pytest.raises(RuntimeError, match="injected evidence failure"):
                publish()
            assert _revision(project, column) == before_revision
            assert not project.db.execute(
                "SELECT 1 FROM results WHERE run_id=?", (run_id,)
            ).fetchone()
        else:
            publish()
        for output, value in ((column, "answer"), (evidence_column, "citation")):
            assert other.get_values(sheet, output)[rows[0]] == (
                None if fail_evidence else value
            )
    finally:
        if other is not None:
            other.close()
        project.close()


def test_source_replacements_preserve_consumed_versions_and_allow_repeated_clear(
    make_project,
):
    from frisket.engine.store.cell_writes import (
        BaseCellWrite,
        replace_base_cells,
        remove_base_cells,
        create_base_cell_producer,
    )

    project = make_project("versions")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "text")
    row = project.add_rows(sheet, [{"text": "original"}], {"text": column})[0]
    original = project.get_values_with_refs(sheet, column, [row])[1][row]
    original_producer = original["producer_id"]
    versions = [original_producer]
    project.db.execute("BEGIN")
    with project.db:
        op_id = project.append_change("source.refresh", commit=False)
        producer = create_base_cell_producer(
            project.db, stage_id=f"op:{op_id}", op_id=op_id
        )
    for value in ("replacement", "another"):
        project.db.execute("BEGIN")
        with project.db:
            replace_base_cells(
                project.db,
                producer_id=producer,
                column_ids=[column],
                row_ids=[row],
                cells=[BaseCellWrite(row, column, value)],
            )
        values, refs = project.get_values_with_refs(sheet, column, [row])
        assert values[row] == value
        versions.append(refs[row]["producer_id"])
    assert len(set(versions)) == 3
    assert [
        r[0]
        for r in project.db.execute(
            "SELECT value FROM cells WHERE row_id=? AND column_id=? ORDER BY producer_id",
            (row, column),
        )
    ] == ["original", "replacement", "another"]
    for _ in range(2):
        project.db.execute("BEGIN")
        with project.db:
            remove_base_cells(
                project.db, producer_id=producer, column_ids=[column], row_ids=[row]
            )
        assert project.get_values_with_refs(sheet, column, [row])[0][row] is None
    assert (
        project.db.execute(
            "SELECT value FROM cells WHERE row_id=? AND column_id=? AND producer_id=?",
            (row, column, original_producer),
        ).fetchone()[0]
        == "original"
    )
    from frisket.engine.store.changes import ChangeStore, ChangePublicationClosed

    project.db.execute("BEGIN")
    with project.db:
        ChangeStore(project).close(op_id)
    project.db.execute("BEGIN")
    with project.db, pytest.raises(ChangePublicationClosed):
        replace_base_cells(
            project.db,
            producer_id=producer,
            column_ids=[column],
            row_ids=[row],
            cells=[BaseCellWrite(row, column, "too late")],
        )

"""Read-side acceptance independent of the projection implementation."""

import json

import pytest

from frisket.engine.store import Project
from frisket.engine.store.cell_projection import repair_current_cells
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
        op_id = project.append_op("map.extract", label="two-output family")
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

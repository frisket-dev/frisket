"""Accepted Change semantics, independently modeled; initially red on old core."""

from contextlib import closing

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from change_oracle import Changes
from frisket.engine.store.result_generations import (
    ResultGenerationError,
    ResultGenerationStore,
)
from frisket.execution.attempt import StaleAttemptWriter
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _release,
    _seed_project,
    _seal,
    _start_claimed_run,
    _write,
)
from frisket.engine.store.transactions import write_transaction


def _replacement(project, sheet, column, rows):
    run = _start_claimed_run(
        project,
        sheet_id=sheet,
        output_column_id=column,
        row_ids=rows,
        label="independent replacement",
    )
    _declare(ResultGenerationStore(project), run, column, write_mode="replace_scope")
    return run


def _publish(project, run, row, column, value):
    _write(
        project,
        run,
        [
            {
                "row_id": row,
                "column_id": column,
                "value": value,
                "publication_effect": "publish_null"
                if value is None
                else "publish_value",
            }
        ],
    )


def test_started_scope_masks_old_values_without_touching_other_rows(make_project):
    project, sheet, column, rows = _seed_project(make_project)
    _publish_initial_generation(project, sheet, column, rows)
    _replacement(project, sheet, column, rows[:2])
    current = project.get_values(sheet, column)
    assert current[rows[0]] is None, "pending replacement must hide old success"
    assert current[rows[1]] is None
    assert current[rows[2]] == "first:2"


def test_active_undo_closes_writer_and_redo_cannot_reopen_it(make_project):
    project, sheet, column, rows = _seed_project(make_project)
    _publish_initial_generation(project, sheet, column, rows)
    run = _replacement(project, sheet, column, rows[:2])
    _publish(project, run, rows[0], column, "saved")
    assert project.undo() == run.op_id
    assert project.get_values(sheet, column)[rows[0]] == "first:0"
    # A late worker may seal the closed generation after Undo. Its earlier
    # published result is still saved work, not an aborted staged output.
    _seal(ResultGenerationStore(project), run, column, disposition="cancelled")
    assert project.redo() == run.op_id
    with write_transaction(project.db):
        ResultGenerationStore(project).rebuild_heads([column])
    assert project.get_values(sheet, column)[rows[0]] == "saved"
    assert project.get_values(sheet, column)[rows[1]] is None
    with pytest.raises((ResultGenerationError, StaleAttemptWriter, ValueError)):
        _publish(project, run, rows[1], column, "late forbidden")
    assert project.get_values(sheet, column)[rows[1]] is None


def test_manual_edit_after_start_beats_older_late_publication(make_project):
    project, sheet, column, rows = _seed_project(make_project)
    _publish_initial_generation(project, sheet, column, rows)
    run = _replacement(project, sheet, column, rows[:2])
    edit = project.apply_edits(
        [
            {
                "row_id": rows[1],
                "column_id": column,
                "value": "human",
            }
        ]
    )
    _publish(project, run, rows[1], column, "late model")
    assert project.get_values(sheet, column)[rows[1]] == "human"
    assert project.undo() == edit
    assert project.get_values(sheet, column)[rows[1]] == "late model"


@settings(max_examples=20, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=3),
            st.one_of(st.none(), st.text(max_size=10)),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_partial_runs_keep_untouched_cells_without_carry_forward(
    tmp_path_factory, updates
):
    # Fresh DB for each generated example; the oracle does not use product reads.
    directory = tmp_path_factory.mktemp("change-sequence")
    project, sheet, column, rows = _seed_project(directory)
    model = Changes()
    with closing(project):
        _publish_initial_generation(project, sheet, column, rows)
        initial = model.start(rows, [column])
        for index, row in enumerate(rows):
            model.publish(initial, row, (f"first:{index}",))
        model.close(initial)
        for position, value in updates:
            row = rows[position]
            run = _replacement(project, sheet, column, [row])
            owner = model.start([row], [column])
            _publish(project, run, row, column, value)
            model.publish(owner, row, (value,))
            _seal(ResultGenerationStore(project), run, column)
            model.close(owner)
            _release(project, run)
            expected = {row: model.read(row, column).value for row in rows}
            assert project.get_values(sheet, column) == expected
            assert project.undo() == run.op_id
            model.deactivate(owner)
            assert project.get_values(sheet, column) == {
                row: model.read(row, column).value for row in rows
            }
            assert project.redo() == run.op_id
            model.restore(owner)
            assert project.get_values(sheet, column) == expected


def test_oracle_restore_retains_terminal_remainder_and_start_precedence():
    model = Changes()
    earlier = model.start([1, 2], [10, 11])
    later = model.start([1], [10])
    model.publish(later, 1, ("edit",))
    model.close(later)
    model.publish(earlier, 1, ("answer", "evidence"))
    assert model.read(1, 10).value == "edit"
    assert model.read(1, 11).value == "evidence"
    model.deactivate(earlier)
    model.restore(earlier)
    assert model.read(2, 10).state == "not_completed"
    with pytest.raises(ValueError, match="closed publication"):
        model.publish(earlier, 2, (None, None))
    model.deactivate(later)
    assert model.read(1, 10).value == "answer"

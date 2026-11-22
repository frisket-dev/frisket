from __future__ import annotations

from contextlib import contextmanager

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store.cell_projection import repair_current_cells
from frisket.engine.store.result_generations import ResultGenerationStore
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _release,
    _seal,
    _seed_project,
    _start_claimed_run,
    _write,
)


@contextmanager
def _vm_work(db):
    work = [0]

    def progress():
        work[0] += 1
        return 0

    db.set_progress_handler(progress, 1)
    try:
        yield work
    finally:
        db.set_progress_handler(None, 0)


# Rows 1-3 sit on "Rows" (row 3 deleted), rows 4-5 on "Other"; "Empty" has
# no rows and "missing" names no column.
@pytest.mark.parametrize(
    ("columns", "keys", "expected"),
    [
        ([], [], True),
        (["generated"], [], False),
        (["missing"], [], False),
        (["missing"], [(1, "missing")], False),
        (["empty"], [], False),
        (["generated"], [(1, "generated"), (2, "generated")], False),
        (["generated"], [(1, "generated"), (2, "generated"), (3, "generated")], True),
        (
            ["generated"],
            [(1, "generated"), (2, "generated"), (3, "generated"), (4, "generated")],
            False,
        ),
        (
            ["generated", "generated"],
            [(1, "generated"), (2, "generated"), (3, "generated")],
            True,
        ),
        (
            ["generated", "second"],
            [(1, "generated"), (2, "generated"), (3, "generated")],
            False,
        ),
        (
            ["generated", "second"],
            [(r, c) for r in (1, 2, 3) for c in ("generated", "second")],
            True,
        ),
        (
            ["generated", "other"],
            [
                (1, "generated"),
                (2, "generated"),
                (3, "generated"),
                (4, "other"),
                (5, "other"),
            ],
            True,
        ),
        (
            ["generated", "other"],
            [(1, "generated"), (2, "generated"), (3, "generated"), (4, "other")],
            False,
        ),
    ],
)
def test_generation_coverage_preserves_column_and_hidden_row_semantics(
    make_project, columns, keys, expected
):
    project = make_project("coverage")
    rows_sheet = project.add_sheet("Rows")
    other_sheet = project.add_sheet("Other")
    empty_sheet = project.add_sheet("Empty")
    column_ids = {
        "generated": project.add_column(
            rows_sheet, "generated", type="integer", generated=True
        ),
        "second": project.add_column(rows_sheet, "second"),
        "other": project.add_column(other_sheet, "other"),
        "empty": project.add_column(empty_sheet, "empty"),
    }
    column_ids["missing"] = max(column_ids.values()) + 1
    row_ids = project.add_rows(
        rows_sheet,
        [{"generated": value} for value in (1, 2, 3)],
        {"generated": column_ids["generated"]},
    ) + project.add_rows(
        other_sheet, [{"other": "a"}, {"other": "b"}], {"other": column_ids["other"]}
    )
    deleted = run_action_spec(
        project,
        {
            "action_id": "row.delete",
            "scope": {"kind": "project"},
            "params": {"sheet_id": rows_sheet, "row_ids": [row_ids[2]]},
            "idempotency_key": "delete-third-row",
        },
        project_id="test",
    )
    assert deleted.status == "completed", deleted.errors
    store = ResultGenerationStore(project)
    with store._empty_key_table() as table:
        project.db.executemany(
            f"INSERT INTO {table} VALUES (?,?)",
            [(row_ids[row - 1], column_ids[name]) for row, name in keys],
        )
        assert (
            store.key_table_covers_columns(table, [column_ids[c] for c in columns])
            is expected
        )


def _coverage_work(make_project, row_count: int) -> int:
    project, sheet_id, column_id, row_ids = _seed_project(
        lambda _name: make_project(f"coverage-{row_count}"), row_count=row_count
    )
    _publish_initial_generation(project, sheet_id, column_id, row_ids)
    while repair_current_cells(project):
        pass
    project.db.execute("ANALYZE")
    project.db.commit()
    store = ResultGenerationStore(project)
    with store._empty_key_table() as table:
        project.db.execute(
            f"INSERT INTO {table} VALUES (?,?)", (row_ids[-1], column_id)
        )
        with _vm_work(project.db) as work:
            assert not store.key_table_covers_columns(table, [column_id])
        return work[0]


def test_sparse_generation_coverage_work_is_independent_of_sheet_size(
    make_project, record_property
):
    small = _coverage_work(make_project, 1_000)
    large = _coverage_work(make_project, 10_000)
    record_property("coverage_steps_1000", small)
    record_property("coverage_steps_10000", large)
    assert large <= small * 2 + 500, (small, large)


def _publication_work(make_project, row_count: int):
    project, sheet_id, column_id, row_ids = _seed_project(
        lambda _name: make_project(f"publication-{row_count}"), row_count=row_count
    )
    second = None
    released = False
    try:
        # Only one row has output history. All the other sheet rows must stay
        # irrelevant to publication and undo of this one-row replacement.
        target = row_ids[-1]
        generations, first = _publish_initial_generation(
            project, sheet_id, column_id, [target]
        )
        second = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=column_id,
            row_ids=[target],
            label="one-row replacement",
        )
        _declare(generations, second, column_id, write_mode="replace_scope")
        assert target not in generations.read_cell_heads(column_id)
        assert project.get_values(sheet_id, column_id, [target]) == {target: None}
        project.db.execute("ANALYZE")
        project.db.commit()
        measurements = {}
        with _vm_work(project.db) as work:
            _write(
                project,
                second,
                [
                    {
                        "row_id": target,
                        "column_id": column_id,
                        "value": "replacement",
                        "publication_effect": "publish_value",
                    }
                ],
            )
        measurements["publish"] = work[0]
        project.db.execute("ANALYZE")
        project.db.commit()
        # Same-descriptor results publish progressively before terminal sealing.
        assert generations.read_cell_heads(column_id)[target].run_id == second.run_id
        assert project.get_values(sheet_id, column_id, [target]) == {
            target: "replacement"
        }
        with _vm_work(project.db) as work:
            _seal(generations, second, column_id)
        measurements["seal"] = work[0]
        _release(project, second)
        released = True
        assert project.get_values(sheet_id, column_id, [target]) == {
            target: "replacement"
        }
        with _vm_work(project.db) as work:
            assert project.undo() == second.change_id
        measurements["undo"] = work[0]
        assert project.get_values(sheet_id, column_id, [target]) == {target: "first:0"}
        with _vm_work(project.db) as work:
            assert project.redo() == second.change_id
        measurements["redo"] = work[0]
        assert project.get_values(sheet_id, column_id, [target]) == {
            target: "replacement"
        }
        assert set(generations.read_cell_heads(column_id)) == {target}
        assert project.get_values(sheet_id, column_id, [row_ids[0]]) == {
            row_ids[0]: None
        }
        return measurements
    finally:
        if second is not None and not released:
            _release(project, second)


def test_sparse_seal_undo_redo_work_does_not_scan_unrelated_sheet_rows(
    make_project, record_property
):
    small = _publication_work(make_project, 1_000)
    large = _publication_work(make_project, 10_000)
    for phase in ("publish", "seal", "undo", "redo"):
        record_property(f"{phase}_steps_1000", small[phase])
        record_property(f"{phase}_steps_10000", large[phase])
    for phase in ("publish", "seal", "undo", "redo"):
        assert large[phase] <= small[phase] * 2 + 500, (phase, small, large)

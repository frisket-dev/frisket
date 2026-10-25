from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from test_sparse_projection_performance import _production_shaped_database


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


@pytest.mark.parametrize(
    ("columns", "keys", "expected"),
    [
        ([], [], True),
        ([10], [], False),
        ([999], [], False),
        ([999], [(1, 999)], False),
        ([40], [], False),
        ([10], [(1, 10), (2, 10)], False),
        ([10], [(1, 10), (2, 10), (3, 10)], True),
        ([10], [(1, 10), (2, 10), (3, 10), (4, 10)], False),
        ([10, 10], [(1, 10), (2, 10), (3, 10)], True),
        ([10, 20], [(1, 10), (2, 10), (3, 10)], False),
        ([10, 20], [(r, c) for r in (1, 2, 3) for c in (10, 20)], True),
        ([10, 30], [(1, 10), (2, 10), (3, 10), (4, 30), (5, 30)], True),
        ([10, 30], [(1, 10), (2, 10), (3, 10), (4, 30)], False),
    ],
)
def test_generation_coverage_preserves_column_and_hidden_row_semantics(
    columns, keys, expected
):
    db = _production_shaped_database(3)
    try:
        db.execute("INSERT INTO sheets(id,name) VALUES (2,'Other'),(3,'Empty')")
        db.execute(
            "INSERT INTO columns(id,sheet_id,name,type) VALUES "
            "(20,1,'second','text'),(30,2,'other','text'),(40,3,'empty','text')"
        )
        db.execute("INSERT INTO rows(id,sheet_id,position) VALUES (4,2,0),(5,2,1)")
        db.execute("UPDATE rows SET hidden=1 WHERE id=3")
        store = ResultGenerationStore(SimpleNamespace(db=db))
        with store._empty_key_table() as table:
            db.executemany(f"INSERT INTO {table} VALUES (?,?)", keys)
            assert store.key_table_covers_columns(table, columns) is expected
    finally:
        db.close()


def _coverage_work(row_count):
    db = _production_shaped_database(row_count)
    try:
        store = ResultGenerationStore(SimpleNamespace(db=db))
        with store._empty_key_table() as table:
            db.execute(f"INSERT INTO {table} VALUES (?,10)", (row_count,))
            with _vm_work(db) as work:
                assert not store.key_table_covers_columns(table, [10])
            return work[0]
    finally:
        db.close()


def test_sparse_generation_coverage_work_is_independent_of_sheet_size(record_property):
    small, large = _coverage_work(1_000), _coverage_work(10_000)
    record_property("coverage_steps_1000", small)
    record_property("coverage_steps_10000", large)
    assert large <= small * 2 + 500, (small, large)


def _publication_work(root: Path, row_count: int):
    project, sheet_id, column_id, row_ids = _seed_project(root, row_count=row_count)
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
        project.db.execute("ANALYZE")
        project.db.commit()
        assert generations.read_cell_heads(column_id)[target].run_id == first.run_id
        measurements = {}
        with _vm_work(project.db) as work:
            _seal(generations, second, column_id)
        measurements["seal"] = work[0]
        _release(project, second)
        released = True
        assert project.get_values(sheet_id, column_id, [target]) == {
            target: "replacement"
        }
        with _vm_work(project.db) as work:
            assert project.undo() == second.op_id
        measurements["undo"] = work[0]
        assert project.get_values(sheet_id, column_id, [target]) == {target: "first:0"}
        with _vm_work(project.db) as work:
            assert project.redo() == second.op_id
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
        project.close()


def test_sparse_seal_undo_redo_work_does_not_scan_unrelated_sheet_rows(
    tmp_path, record_property
):
    small = _publication_work(tmp_path / "small", 1_000)
    large = _publication_work(tmp_path / "large", 10_000)
    for phase in ("seal", "undo", "redo"):
        record_property(f"{phase}_steps_1000", small[phase])
        record_property(f"{phase}_steps_10000", large[phase])
    for phase in ("seal", "undo", "redo"):
        assert large[phase] <= small[phase] * 2 + 500, (phase, small, large)

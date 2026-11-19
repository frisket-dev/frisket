from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket.engine.store.current_cells import register_current_cell_functions
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.schema import SCHEMA
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _release,
    _seal,
    _seed_project,
    _start_claimed_run,
    _write,
)


def _production_shaped_database(row_count: int) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    register_current_cell_functions(db)
    db.executescript(SCHEMA)
    db.execute("DELETE FROM search_dirty_scopes")
    db.execute("INSERT INTO sheets (id,name) VALUES (1,'Rows')")
    db.execute(
        "INSERT INTO columns (id,sheet_id,name,type,ai_generated) "
        "VALUES (10,1,'generated','integer',1)"
    )
    db.execute(
        "INSERT INTO ops (id,kind,label,spec,undo_info,change_order) "
        "VALUES (1,'source.write','source','{}','{}',1)"
    )
    db.execute(
        "INSERT INTO base_cell_producers (id,stage_id,op_id) VALUES (1,'op:1',1)"
    )
    db.execute(
        "INSERT INTO ops (id,kind,label,spec,undo_info,change_order,publication_closed) "
        "VALUES (2,'map.test','generated','{}','{}',2,0)"
    )
    db.execute(
        "INSERT INTO runs (id,op_id,sheet_id,action_kind) VALUES (20,2,1,'map.test')"
    )
    db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token,output_descriptor_json) "
        "VALUES (20,10,'value','integer-v1','create','active','claim-20',?)",
        (
            json.dumps(
                {
                    "column_type": "integer",
                    "schema": {},
                    "format": None,
                    "semantic_type": None,
                }
            ),
        ),
    )
    rows = [(row_id, 1, row_id) for row_id in range(1, row_count + 1)]
    db.executemany("INSERT INTO rows (id,sheet_id,position) VALUES (?,?,?)", rows)
    db.executemany(
        "INSERT INTO run_rows(run_id,row_id,position) VALUES (20,?,?)",
        ((row_id, row_id - 1) for row_id in range(1, row_count + 1)),
    )
    db.executemany(
        "INSERT INTO cells (row_id,column_id,value_kind,value,producer_id) "
        "VALUES (?,10,'integer',?,1)",
        ((row_id, -row_id) for row_id in range(1, row_count + 1)),
    )
    db.executemany(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value_kind,value,outcome,publication_effect,published) "
        "VALUES (20,?,10,'integer',?,'ok','publish_value',1)",
        ((row_id, row_id) for row_id in range(1, row_count + 1)),
    )
    db.executemany(
        "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (10,?,20)",
        ((row_id,) for row_id in range(1, row_count + 1)),
    )
    db.executemany(
        "INSERT INTO current_cells "
        "(column_id,row_id,retrieval_revision,origin_kind,origin_op_id,"
        "origin_run_id,validity) "
        "VALUES (10,?,1,'run_result',2,20,'valid')",
        ((row_id,) for row_id in range(1, row_count + 1)),
    )
    db.commit()
    db.execute("ANALYZE")
    return db


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
    for phase in ("publish", "seal", "undo", "redo"):
        record_property(f"{phase}_steps_1000", small[phase])
        record_property(f"{phase}_steps_10000", large[phase])
    for phase in ("publish", "seal", "undo", "redo"):
        assert large[phase] <= small[phase] * 2 + 500, (phase, small, large)

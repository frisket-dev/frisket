from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from frisket.engine.store import Project
from frisket.engine.store.current_cells import refresh_current_cells_from_key_table
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.schema import SCHEMA


def _production_shaped_database(row_count: int) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("DELETE FROM search_dirty_scopes")
    db.execute("INSERT INTO sheets (id,name) VALUES (1,'Rows')")
    db.execute(
        "INSERT INTO columns (id,sheet_id,name,type,ai_generated) "
        "VALUES (10,1,'generated','integer',1)"
    )
    db.execute(
        "INSERT INTO ops (id,kind,label,spec,undo_info) "
        "VALUES (1,'source.write','source','{}','{}')"
    )
    db.execute(
        "INSERT INTO base_cell_producers (id,stage_id,op_id) VALUES (1,'op:1',1)"
    )
    db.execute(
        "INSERT INTO ops (id,kind,label,spec,undo_info) "
        "VALUES (2,'map.test','generated','{}','{}')"
    )
    db.execute(
        "INSERT INTO runs (id,op_id,sheet_id,action_kind) VALUES (20,2,1,'map.test')"
    )
    db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
        "VALUES (20,10,'value','integer-v1','create','active','claim-20')"
    )
    rows = [(row_id, 1, row_id) for row_id in range(1, row_count + 1)]
    db.executemany("INSERT INTO rows (id,sheet_id,position) VALUES (?,?,?)", rows)
    db.executemany(
        "INSERT INTO cells (row_id,column_id,value_kind,value,producer_id) "
        "VALUES (?,10,'integer',?,1)",
        ((row_id, -row_id) for row_id in range(1, row_count + 1)),
    )
    db.executemany(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value_kind,value,outcome,publication_effect) "
        "VALUES (20,?,10,'integer',?,'ok','publish_value')",
        ((row_id, row_id) for row_id in range(1, row_count + 1)),
    )
    db.executemany(
        "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (10,?,20)",
        ((row_id,) for row_id in range(1, row_count + 1)),
    )
    db.executemany(
        "INSERT INTO current_cells "
        "(column_id,row_id,origin_kind,origin_op_id,origin_run_id,validity) "
        "VALUES (10,?,'run_result',2,20,'valid')",
        ((row_id,) for row_id in range(1, row_count + 1)),
    )
    db.commit()
    db.execute("ANALYZE")
    return db


def test_exact_key_refresh_work_is_bounded_by_sparse_target() -> None:
    row_count = 25_000
    db = _production_shaped_database(row_count)
    key_table = "temp_result_keys_1234abcd"
    db.execute(
        f"CREATE TEMP TABLE {key_table} ("
        "row_id INTEGER NOT NULL,column_id INTEGER NOT NULL,"
        "PRIMARY KEY(row_id,column_id)) WITHOUT ROWID"
    )
    target_rows = list(range(row_count - 300, row_count + 1))
    db.executemany(
        f"INSERT INTO {key_table} (row_id,column_id) VALUES (?,10)",
        ((row_id,) for row_id in target_rows),
    )
    db.commit()
    db.execute("BEGIN")

    progress_calls = 0

    def count_vm_work() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return 0

    db.set_progress_handler(count_vm_work, 1_000)
    try:
        assert refresh_current_cells_from_key_table(db, key_table) == len(target_rows)
    finally:
        db.set_progress_handler(None, 0)

    assert progress_calls < 500
    refreshed = db.execute(
        "SELECT COUNT(*) AS cell_count,MIN(origin_kind) AS min_origin,"
        "MAX(origin_kind) AS max_origin FROM current_cells "
        f"WHERE (column_id,row_id) IN (SELECT column_id,row_id FROM {key_table})"
    ).fetchone()
    assert tuple(refreshed) == (len(target_rows), "run_result", "run_result")
    assert db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0] == row_count
    db.rollback()
    db.close()


def _sparse_head_rebuild_vm_work(row_count: int) -> int:
    db = _production_shaped_database(row_count)
    key_table = "temp_result_keys_5678abcd"
    db.execute(
        f"CREATE TEMP TABLE {key_table} ("
        "row_id INTEGER NOT NULL,column_id INTEGER NOT NULL,"
        "PRIMARY KEY(row_id,column_id)) WITHOUT ROWID"
    )
    target_rows = list(range(row_count - 300, row_count + 1))
    db.executemany(
        f"INSERT INTO {key_table} (row_id,column_id) VALUES (?,10)",
        ((row_id,) for row_id in target_rows),
    )
    db.commit()
    db.execute("BEGIN")

    progress_calls = 0

    def count_vm_work() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return 0

    db.set_progress_handler(count_vm_work, 1_000)
    try:
        store = ResultGenerationStore(SimpleNamespace(db=db))
        assert store.rebuild_heads_from_key_table(key_table, commit=False) == len(
            target_rows
        )
    finally:
        db.set_progress_handler(None, 0)

    assert {
        int(row["row_id"])
        for row in db.execute(
            "SELECT row_id FROM cell_result_heads WHERE column_id=10 "
            f"AND row_id IN (SELECT row_id FROM {key_table})"
        )
    } == set(target_rows)
    db.rollback()
    db.close()
    return progress_calls


def test_sparse_head_rebuild_work_does_not_scale_with_unrelated_history() -> None:
    small_work = _sparse_head_rebuild_vm_work(2_500)
    large_work = _sparse_head_rebuild_vm_work(25_000)

    assert large_work <= small_work * 2 + 50


def test_plain_source_and_output_edit_undo_redo_skip_review_recount(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "plain-edits.frisket", name="Edits")
    try:
        sheet_id = project.add_sheet("Rows")
        source_id = project.add_column(sheet_id, "source")
        output_id = project.add_column(
            sheet_id, "output", type="text", ai_generated=True
        )
        project.add_rows(sheet_id, [{"source": "before"}], {"source": source_id})
        row_id = int(
            project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=?", (sheet_id,)
            ).fetchone()[0]
        )
        edit_op_id = project.apply_edits(
            [
                {"row_id": row_id, "column_id": source_id, "value": "after"},
                {"row_id": row_id, "column_id": output_id, "value": "manual"},
            ]
        )
        refresh_calls = 0

        def count_refresh(_project: Project) -> int:
            nonlocal refresh_calls
            refresh_calls += 1
            return 0

        monkeypatch.setattr(Project, "refresh_pending_review_summary", count_refresh)

        assert project.undo() == edit_op_id
        assert project.get_values(sheet_id, source_id) == {row_id: "before"}
        assert project.get_values(sheet_id, output_id) == {row_id: None}
        assert project.redo() == edit_op_id
        assert project.get_values(sheet_id, source_id) == {row_id: "after"}
        assert project.get_values(sheet_id, output_id) == {row_id: "manual"}
        assert refresh_calls == 0
    finally:
        project.close()


@pytest.mark.parametrize(
    "transition_metadata",
    [
        {"review_states": {}},
        {"created_rows": []},
        {"column_pointers": {}},
    ],
    ids=["review-state", "visibility", "result-identity"],
)
def test_unknown_edit_transition_metadata_keeps_review_recount(
    tmp_path, monkeypatch: pytest.MonkeyPatch, transition_metadata: dict[str, object]
) -> None:
    project = Project.create(tmp_path / "guarded-edit.frisket", name="Guarded edit")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "source")
        project.add_rows(sheet_id, [{"source": "before"}], {"source": column_id})
        row_id = int(
            project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=?", (sheet_id,)
            ).fetchone()[0]
        )
        edit_op_id = project.apply_edits(
            [{"row_id": row_id, "column_id": column_id, "value": "after"}]
        )
        project.db.execute(
            "UPDATE ops SET undo_info=? WHERE id=?",
            (json.dumps(transition_metadata), edit_op_id),
        )
        project.db.commit()
        refresh_calls = 0

        def count_refresh(_project: Project) -> int:
            nonlocal refresh_calls
            refresh_calls += 1
            return 0

        monkeypatch.setattr(Project, "refresh_pending_review_summary", count_refresh)

        assert project.undo() == edit_op_id
        assert project.redo() == edit_op_id
        assert refresh_calls == 2
    finally:
        project.close()


def test_edit_operation_with_generation_metadata_keeps_review_recount(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "generation-edit.frisket", name="Generation")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "source")
        project.add_rows(sheet_id, [{"source": "before"}], {"source": column_id})
        row_id = int(
            project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=?", (sheet_id,)
            ).fetchone()[0]
        )
        edit_op_id = project.apply_edits(
            [{"row_id": row_id, "column_id": column_id, "value": "after"}]
        )
        run_id = int(
            project.db.execute(
                "INSERT INTO runs (op_id,sheet_id,action_kind) VALUES (?,?,?)",
                (edit_op_id, sheet_id, "map.test"),
            ).lastrowid
        )
        project.db.execute(
            "INSERT INTO run_output_generations "
            "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
            "VALUES (?,?,'value','text-v1','create','active','claim-test')",
            (run_id, column_id),
        )
        project.db.commit()
        refresh_calls = 0

        def count_refresh(_project: Project) -> int:
            nonlocal refresh_calls
            refresh_calls += 1
            return 0

        monkeypatch.setattr(Project, "refresh_pending_review_summary", count_refresh)

        assert project.undo() == edit_op_id
        assert project.redo() == edit_op_id
        assert refresh_calls == 2
    finally:
        project.close()

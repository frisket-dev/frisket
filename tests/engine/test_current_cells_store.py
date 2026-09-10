from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    EditCellWrite,
    bind_base_cell_producer,
    create_base_cell_producer,
    discard_pending_base_cell_producer,
    initialize_base_cells,
    insert_edits,
    remove_base_cells,
    replace_base_cells,
)
from frisket.engine.store.current_cells import (
    rebuild_current_cells,
    refresh_current_cell_pairs,
    refresh_current_cells,
)
from frisket.engine.store.schema import SCHEMA


def _database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("INSERT INTO sheets (id,name) VALUES (1,'Sheet')")
    db.execute("INSERT INTO columns (id,sheet_id,name) VALUES (10,1,'value')")
    db.executemany(
        "INSERT INTO rows (id,sheet_id,position) VALUES (?,1,?)",
        [(100, 1), (101, 2), (102, 3)],
    )
    db.commit()
    return db


def _op(
    db: sqlite3.Connection,
    op_id: int,
    *,
    kind: str = "source.write",
    spec: str = "{}",
    undo_info: str = "{}",
) -> None:
    db.execute(
        "INSERT INTO ops (id,kind,spec,undo_info) VALUES (?,?,?,?)",
        (op_id, kind, spec, undo_info),
    )


def _result_head(
    db: sqlite3.Connection,
    *,
    op_id: int,
    run_id: int,
    row_id: int,
    value: str | None,
    effect: str,
    error: str | None = None,
    write_mode: str = "create",
    additional: list[tuple[int, str | None, str, str | None]] | None = None,
) -> None:
    _op(
        db,
        op_id,
        kind="map.test",
        spec='{"replace_existing":true}' if write_mode == "replace_scope" else "{}",
    )
    db.execute(
        "INSERT INTO runs (id,op_id,sheet_id,action_kind) VALUES (?, ?, 1, 'map.test')",
        (run_id, op_id),
    )
    state = "staged" if write_mode == "replace_scope" else "active"
    db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
        "VALUES (?,10,'value','text',?,?,?)",
        (run_id, write_mode, state, f"claim-{run_id}"),
    )
    result_rows = [(row_id, value, effect, error), *(additional or [])]
    db.executemany(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value,error,outcome,publication_effect) "
        "VALUES (?,?,10,?,?,?,?)",
        [
            (
                run_id,
                result_row_id,
                result_value,
                result_error,
                "model_error" if result_error else "ok",
                result_effect,
            )
            for result_row_id, result_value, result_effect, result_error in result_rows
        ],
    )
    if write_mode == "replace_scope":
        db.execute(
            "UPDATE run_output_generations SET state='sealed',"
            "terminal_disposition='completed',sealed_at=datetime('now') "
            "WHERE run_id=? AND column_id=10",
            (run_id,),
        )
    db.executemany(
        "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (10,?,?)",
        [(result_row_id, run_id) for result_row_id, *_rest in result_rows],
    )


def test_base_and_edit_sinks_are_transactional_and_preserve_source_ref_shape() -> None:
    db = _database()
    with pytest.raises(RuntimeError, match="caller transaction"):
        create_base_cell_producer(db, stage_id="op:1", op_id=1)

    db.execute("BEGIN")
    _op(db, 1)
    producer_id = create_base_cell_producer(db, stage_id="op:1", op_id=1)
    assert create_base_cell_producer(db, stage_id="op:1", op_id=1) == producer_id
    assert (
        initialize_base_cells(
            db,
            producer_id=producer_id,
            cells=[
                BaseCellWrite(100, 10, {"nested": True}),
                BaseCellWrite(101, 10, None),
            ],
        )
        == 2
    )
    source = db.execute(
        "SELECT * FROM current_cells WHERE column_id=10 AND row_id=100"
    ).fetchone()
    assert source["origin_kind"] == "source_cell"
    assert source["origin_op_id"] is None
    assert source["origin_run_id"] is None
    assert source["base_producer_id"] == producer_id
    explicit_null = db.execute(
        "SELECT value,origin_kind FROM current_cells WHERE column_id=10 AND row_id=101"
    ).fetchone()
    assert tuple(explicit_null) == ("null", "source_cell")

    _op(db, 2, kind="edit")
    insert_edits(db, op_id=2, edits=[EditCellWrite(100, 10, None)])
    edited = db.execute(
        "SELECT * FROM current_cells WHERE column_id=10 AND row_id=100"
    ).fetchone()
    assert tuple(
        edited[key] for key in ("value", "origin_kind", "origin_op_id", "origin_run_id")
    ) == ("null", "manual_edit", 2, None)

    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0] == 0


def test_pending_producer_binds_once_and_abort_discards_only_unreferenced() -> None:
    db = _database()
    db.execute("BEGIN")
    db.execute("UPDATE sheets SET hidden=1 WHERE id=1")
    producer_id = create_base_cell_producer(db, stage_id="stream:receipt-1")
    initialize_base_cells(
        db,
        producer_id=producer_id,
        cells=[BaseCellWrite(100, 10, "staged")],
    )
    with pytest.raises(ValueError, match="referenced"):
        discard_pending_base_cell_producer(db, producer_id)
    _op(db, 1)
    bind_base_cell_producer(db, producer_id, op_id=1)
    db.execute("UPDATE sheets SET hidden=0 WHERE id=1")
    bind_base_cell_producer(db, producer_id, op_id=1)
    _op(db, 2)
    with pytest.raises(ValueError, match="already bound"):
        bind_base_cell_producer(db, producer_id, op_id=2)
    db.rollback()

    db.execute("BEGIN")
    aborted = create_base_cell_producer(db, stage_id="stream:receipt-2")
    discard_pending_base_cell_producer(db, aborted)
    discard_pending_base_cell_producer(db, aborted)
    db.commit()
    assert (
        db.execute(
            "SELECT 1 FROM base_cell_producers WHERE id=?", (aborted,)
        ).fetchone()
        is None
    )


def test_sinks_reject_unknown_authority_and_cross_sheet_targets() -> None:
    db = _database()
    db.execute("BEGIN")
    with pytest.raises(ValueError, match="unknown producing operation"):
        create_base_cell_producer(db, stage_id="op:404", op_id=404)
    with pytest.raises(ValueError, match="unknown base-cell producer"):
        initialize_base_cells(
            db,
            producer_id=404,
            cells=[BaseCellWrite(100, 10, "value")],
        )
    with pytest.raises(ValueError, match="unknown producing operation"):
        insert_edits(db, op_id=404, edits=[EditCellWrite(100, 10, "value")])

    pending = create_base_cell_producer(db, stage_id="stream:visible")
    with pytest.raises(ValueError, match="hidden staging sheet"):
        initialize_base_cells(
            db,
            producer_id=pending,
            cells=[BaseCellWrite(100, 10, "not hidden")],
        )

    _op(db, 1)
    producer = create_base_cell_producer(db, stage_id="op:1", op_id=1)
    db.execute("INSERT INTO sheets (id,name) VALUES (2,'Other')")
    db.execute("INSERT INTO columns (id,sheet_id,name) VALUES (20,2,'other')")
    with pytest.raises(ValueError, match="same sheet"):
        initialize_base_cells(
            db,
            producer_id=producer,
            cells=[BaseCellWrite(100, 20, "wrong")],
        )
    with pytest.raises(ValueError, match="same sheet"):
        insert_edits(db, op_id=1, edits=[EditCellWrite(100, 20, "wrong")])
    db.execute("UPDATE ops SET status='discarded' WHERE id=1")
    with pytest.raises(ValueError, match="not bound to an applied operation"):
        initialize_base_cells(
            db,
            producer_id=producer,
            cells=[BaseCellWrite(100, 10, "late")],
        )


def test_projection_preserves_explicit_result_null_error_and_edit_clear_heads() -> None:
    db = _database()
    db.execute("BEGIN")
    _op(db, 1)
    producer = create_base_cell_producer(db, stage_id="op:1", op_id=1)
    initialize_base_cells(
        db,
        producer_id=producer,
        cells=[
            BaseCellWrite(100, 10, "old-null"),
            BaseCellWrite(101, 10, "old-error"),
            BaseCellWrite(102, 10, "old-clear"),
        ],
    )
    _result_head(
        db,
        op_id=2,
        run_id=20,
        row_id=100,
        value=None,
        effect="publish_null",
    )
    _result_head(
        db,
        op_id=3,
        run_id=30,
        row_id=101,
        value=None,
        effect="publish_error",
        error="failed",
    )
    _op(db, 4, kind="edit")
    insert_edits(db, op_id=4, edits=[EditCellWrite(102, 10, None)])
    refresh_current_cells(db, column_ids=[10])

    projected = {
        int(row["row_id"]): (row["value"], row["origin_kind"])
        for row in db.execute(
            "SELECT row_id,value,origin_kind FROM current_cells ORDER BY row_id"
        )
    }
    assert projected == {
        100: (None, "run_result"),
        101: (None, "run_result"),
        102: ("null", "manual_edit"),
    }


def test_replacement_boundary_and_reject_clear_use_the_existing_precedence() -> None:
    db = _database()
    db.execute("BEGIN")
    _op(db, 1)
    producer = create_base_cell_producer(db, stage_id="op:1", op_id=1)
    initialize_base_cells(
        db,
        producer_id=producer,
        cells=[BaseCellWrite(100, 10, "base"), BaseCellWrite(101, 10, "base")],
    )
    _op(db, 2, kind="edit")
    insert_edits(
        db,
        op_id=2,
        edits=[EditCellWrite(100, 10, "old edit")],
    )
    _op(
        db,
        3,
        kind="review.decision",
        spec=('{"action_id":"review.decision","params":{"decision":"reject_clear"}}'),
    )
    insert_edits(db, op_id=3, edits=[EditCellWrite(101, 10, None)])

    _result_head(
        db,
        op_id=4,
        run_id=40,
        row_id=100,
        value='"replacement"',
        effect="publish_value",
        write_mode="replace_scope",
        additional=[(101, '"replacement two"', "publish_value", None)],
    )
    # The descriptor-change marker makes op 4 the #564 whole-column boundary.
    db.execute(
        "UPDATE ops SET undo_info=? WHERE id=4",
        ('{"column_types_after":{"10":"text"}}',),
    )
    # A replacement head also supersedes the older reject-clear overlay.
    refresh_current_cells(db, column_ids=[10])
    assert [
        tuple(row)
        for row in db.execute(
            "SELECT row_id,value,origin_kind FROM current_cells ORDER BY row_id"
        )
    ] == [
        (100, '"replacement"', "run_result"),
        (101, '"replacement two"', "run_result"),
    ]

    _op(db, 5, kind="edit")
    insert_edits(db, op_id=5, edits=[EditCellWrite(100, 10, "after")])
    assert tuple(
        db.execute(
            "SELECT value,origin_kind,origin_op_id FROM current_cells "
            "WHERE column_id=10 AND row_id=100"
        ).fetchone()
    ) == ('"after"', "manual_edit", 5)


def test_replace_remove_and_rebuild_share_the_same_projector() -> None:
    db = _database()
    db.execute("BEGIN")
    _op(db, 1)
    first = create_base_cell_producer(db, stage_id="op:1", op_id=1)
    initialize_base_cells(
        db,
        producer_id=first,
        cells=[BaseCellWrite(100, 10, "first"), BaseCellWrite(101, 10, "keep")],
    )
    _op(db, 2)
    second = create_base_cell_producer(db, stage_id="op:2", op_id=2)
    replace_base_cells(
        db,
        producer_id=second,
        column_ids=[10],
        row_ids=[100],
        cells=[BaseCellWrite(100, 10, "second")],
    )
    assert db.execute(
        "SELECT value,base_producer_id FROM current_cells WHERE row_id=100"
    ).fetchone()[:] == ('"second"', second)
    assert (
        remove_base_cells(db, producer_id=second, column_ids=[10], row_ids=[101]) == 1
    )
    assert (
        db.execute(
            "SELECT 1 FROM current_cells WHERE column_id=10 AND row_id=101"
        ).fetchone()
        is None
    )

    db.execute("DELETE FROM current_cells")
    assert rebuild_current_cells(db) == 1
    assert (
        db.execute(
            "SELECT value FROM current_cells WHERE column_id=10 AND row_id=100"
        ).fetchone()[0]
        == '"second"'
    )


def test_exact_pair_refresh_groups_dense_sets_without_expanding_sparse_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import frisket.engine.store.current_cells as current_cells_module

    db = _database()
    db.execute("BEGIN")
    calls: list[tuple[tuple[int, ...], tuple[int, ...]]] = []

    def record_region(
        _db: sqlite3.Connection,
        *,
        column_ids: list[int],
        row_ids: tuple[int, ...],
    ) -> int:
        calls.append((tuple(column_ids), tuple(row_ids)))
        return len(column_ids) * len(row_ids)

    monkeypatch.setattr(current_cells_module, "refresh_current_cells", record_region)
    refreshed = refresh_current_cell_pairs(
        db,
        {
            (100, 10),
            (101, 10),
            (100, 11),
            (101, 11),
            (102, 12),
            (103, 13),
        },
    )

    assert calls == [
        ((10, 11), (100, 101)),
        ((12,), (102,)),
        ((13,), (103,)),
    ]
    assert refreshed == 6

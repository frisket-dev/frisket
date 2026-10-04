from __future__ import annotations

import math
import sqlite3

import pytest

from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    EditCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
    insert_edits,
)
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.schema import SCHEMA, SCHEMA_DIGEST
from frisket.engine.store.typed_storage_migration import TYPED_VALUES_TO_DIGEST
from frisket.engine.store.value_codec import (
    decode_stored_value,
    encode_stored_value,
)


def _database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("INSERT INTO sheets (id,name) VALUES (1,'Sheet')")
    db.execute("INSERT INTO columns (id,sheet_id,name) VALUES (10,1,'value')")
    db.executemany(
        "INSERT INTO rows (id,sheet_id,position) VALUES (?,1,?)",
        [(100, 1), (101, 2)],
    )
    db.commit()
    return db


def test_typed_migration_endpoint_matches_fresh_schema() -> None:
    assert TYPED_VALUES_TO_DIGEST == SCHEMA_DIGEST


@pytest.mark.parametrize(
    ("value", "kind", "sqlite_type"),
    [
        (None, "null", "null"),
        ("0012", "text", "text"),
        (12, "integer", "integer"),
        (1.5, "real", "real"),
        (True, "boolean", "integer"),
        ({"nested": [1, "two"]}, "json", "text"),
        (2**80, "bigint", "text"),
    ],
)
def test_value_codec_round_trips_native_sqlite_storage(
    value: object, kind: str, sqlite_type: str
) -> None:
    value_kind, stored = encode_stored_value(value)
    assert value_kind == kind
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE typed(value_kind TEXT NOT NULL,value)")
    db.execute("INSERT INTO typed VALUES (?,?)", (value_kind, stored))
    row = db.execute("SELECT value_kind,value,typeof(value) FROM typed").fetchone()
    assert row[0] == kind
    assert row[2] == sqlite_type
    decoded = decode_stored_value(row[0], row[1])
    assert decoded == value
    if isinstance(value, float):
        assert math.copysign(1.0, decoded) == math.copysign(1.0, value)


def test_value_codec_preserves_legacy_invalid_bytes_and_failure_contract() -> None:
    raw = '{"broken":'
    with pytest.raises(ValueError):
        decode_stored_value("legacy_invalid", raw)
    assert decode_stored_value("legacy_invalid", raw, tolerate_errors=True) is None


def test_current_cell_values_resolves_native_source_and_edit_payloads() -> None:
    db = _database()
    db.execute("BEGIN")
    db.execute("INSERT INTO ops (id,kind,spec) VALUES (1,'source.write','{}')")
    producer_id = create_base_cell_producer(db, stage_id="op:1", op_id=1)
    initialize_base_cells(
        db,
        producer_id=producer_id,
        cells=[BaseCellWrite(100, 10, "0012"), BaseCellWrite(101, 10, 7)],
    )
    db.execute("INSERT INTO ops (id,kind,spec) VALUES (2,'edit','{}')")
    insert_edits(db, op_id=2, edits=[EditCellWrite(101, 10, None)])

    rows = db.execute(
        "SELECT row_id,value_kind,value,typeof(value),origin_kind,validity "
        "FROM current_cell_values WHERE column_id=10 ORDER BY row_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (100, "text", "0012", "text", "source_cell", "valid"),
        (101, "null", None, "null", "manual_edit", "missing"),
    ]
    projection_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(current_cells)")
    }
    assert "value" not in projection_columns


def test_current_cell_values_correlated_lookup_uses_exact_head_key() -> None:
    db = _database()
    plan = "\n".join(
        str(row[3])
        for row in db.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT r.id,(SELECT value FROM current_cell_values AS live "
            "WHERE live.column_id=10 AND live.row_id=r.id) "
            "FROM rows AS r WHERE r.sheet_id=1 ORDER BY r.position"
        )
    )
    assert (
        "SEARCH head USING INDEX idx_current_cells_column_row "
        "(column_id=? AND row_id=?)" in plan
    )
    assert "SCAN head" not in plan


class _Project:
    def __init__(self, db: sqlite3.Connection):
        self.db = db


def test_decoded_result_rows_batches_coordinates_and_preserves_metadata() -> None:
    db = _database()
    db.execute("INSERT INTO ops (id,kind,spec) VALUES (1,'map.test','{}')")
    db.execute(
        "INSERT INTO runs (id,op_id,sheet_id,action_kind) VALUES (20,1,1,'map.test')"
    )
    db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
        "VALUES (20,10,'value','test','create','active','claim')"
    )
    number_kind, number_value = encode_stored_value(4.5)
    null_kind, null_value = encode_stored_value(None)
    db.executemany(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value_kind,value,confidence,outcome,publication_effect) "
        "VALUES (20,?,10,?,?,?,?,?)",
        [
            (100, number_kind, number_value, 0.75, "ok", "publish_value"),
            (101, null_kind, null_value, None, "empty", "publish_null"),
        ],
    )
    rows = RunResultStore(_Project(db)).decoded_result_rows(
        [(20, 101, 10), (20, 100, 10), (20, 100, 10), (404, 1, 2)]
    )
    assert set(rows) == {(20, 100, 10), (20, 101, 10)}
    assert rows[(20, 100, 10)]["value"] == 4.5
    assert rows[(20, 100, 10)]["value_kind"] == "real"
    assert rows[(20, 100, 10)]["confidence"] == 0.75
    assert rows[(20, 101, 10)]["value"] is None
    assert rows[(20, 101, 10)]["value_kind"] == "null"
    assert rows[(20, 101, 10)]["publication_effect"] == "publish_null"

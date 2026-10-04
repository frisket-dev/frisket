from __future__ import annotations

import math
import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.bundle_open import _TYPED_VALUES_FROM_DIGEST
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    EditCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
    insert_edits,
)
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.schema import SCHEMA, SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.engine.store.typed_storage_migration import (
    TYPED_VALUES_TO_DIGEST,
    migrate_typed_values,
)
from frisket.engine.store.value_codec import (
    decode_stored_value,
    encode_stored_value,
    migrate_legacy_json_value,
)
from frisket.search import drain_index, search_project
from frisket.search_index import index_needs_work
from tests.engine.test_bundle_schema_fence import _restore_legacy_authorities


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


def test_legacy_surrogate_string_stays_bindable_and_decodable() -> None:
    raw = '"\\ud800"'
    value_kind, stored = migrate_legacy_json_value(raw)
    assert (value_kind, stored) == ("legacy_invalid", raw)
    assert decode_stored_value(value_kind, stored) == "\ud800"
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE migrated(value_kind TEXT NOT NULL,value)")
    db.execute("INSERT INTO migrated VALUES (?,?)", (value_kind, stored))


def test_forward_write_preserves_lone_surrogate_string(tmp_path) -> None:
    value = "prefix\ud800suffix"
    project = Project.create(tmp_path / "surrogate.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "text", type="text")

        bad_row, ordinary_row = project.add_rows(
            sheet_id,
            [{"text": value}, {"text": "ordinaryneedle"}],
            {"text": column_id},
        )

        assert project.get_values(sheet_id, column_id) == {
            bad_row: value,
            ordinary_row: "ordinaryneedle",
        }
        stored = project.db.execute(
            "SELECT value_kind,value,typeof(value) FROM cells "
            "WHERE row_id=? AND column_id=?",
            (bad_row, column_id),
        ).fetchone()
        assert stored[:] == ("legacy_invalid", '"prefix\\ud800suffix"', "text")

        drain_index(project)
        assert not index_needs_work(project)
        assert search_project(project, "prefix", rerank="off") == []
        assert [
            hit["row_id"]
            for hit in search_project(project, "ordinaryneedle", rerank="off")
        ] == [ordinary_row]
    finally:
        project.close()


def test_typed_migration_preserves_surrogate_in_every_authority(tmp_path) -> None:
    db = _database()
    db.execute("INSERT INTO rows (id,sheet_id,position) VALUES (102,1,3)")
    db.execute("PRAGMA foreign_keys=OFF")
    _restore_legacy_authorities(db)
    raw = '"\\ud800"'
    db.executemany(
        "INSERT INTO ops (id,kind,spec) VALUES (?,?, '{}')",
        [(1, "source.write"), (2, "map.test"), (3, "edit")],
    )
    db.execute(
        "INSERT INTO base_cell_producers (id,stage_id,op_id) VALUES (1,'op:1',1)"
    )
    db.execute(
        "INSERT INTO cells (row_id,column_id,value,producer_id) VALUES (100,10,?,1)",
        (raw,),
    )
    db.execute(
        "INSERT INTO runs (id,op_id,sheet_id,action_kind) VALUES (20,2,1,'map.test')"
    )
    db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
        "VALUES (20,10,'value','text','create','active','claim')"
    )
    db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value,outcome,publication_effect) "
        "VALUES (20,101,10,?,'ok','publish_value')",
        (raw,),
    )
    db.execute(
        "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (10,101,20)"
    )
    db.execute(
        "INSERT INTO edits (op_id,row_id,column_id,value) VALUES (3,102,10,?)",
        (raw,),
    )
    db.executemany(
        "INSERT INTO current_cells "
        "(column_id,row_id,value,origin_kind,origin_op_id,origin_run_id,"
        "base_producer_id,validity) VALUES (10,?,?,?,?,?,?, 'valid')",
        [
            (100, raw, "source_cell", None, None, 1),
            (101, raw, "run_result", 2, 20, None),
            (102, raw, "manual_edit", 3, None, None),
        ],
    )
    db.execute(
        "INSERT INTO meta (key,value) VALUES (?,?)",
        (SCHEMA_DIGEST_META_KEY, _TYPED_VALUES_FROM_DIGEST),
    )
    db.commit()

    migrate_typed_values(
        db,
        bundle_path=tmp_path / "project.db",
        from_digest=_TYPED_VALUES_FROM_DIGEST,
    )

    for table in ("cells", "results", "edits"):
        assert db.execute(f"SELECT value_kind,value FROM {table}").fetchone()[:] == (
            "legacy_invalid",
            raw,
        )
    migrated = db.execute(
        "SELECT row_id,value_kind,value FROM current_cell_values ORDER BY row_id"
    ).fetchall()
    assert [tuple(row) for row in migrated] == [
        (100, "legacy_invalid", raw),
        (101, "legacy_invalid", raw),
        (102, "legacy_invalid", raw),
    ]
    assert (
        db.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone()[0]
        == SCHEMA_DIGEST
    )


def test_project_open_reads_migrated_surrogate_as_original_string(tmp_path) -> None:
    path = tmp_path / "migrated-surrogate.frisket"
    project = Project.create(path)
    sheet_id = project.add_sheet("Rows")
    column_id = project.add_column(sheet_id, "text", type="text")
    row_id = project.add_rows(
        sheet_id, [{"text": "placeholder"}], {"text": column_id}
    )[0]
    project.close()

    raw = '"\\ud800"'
    with sqlite3.connect(path / "project.db") as legacy:
        legacy.row_factory = sqlite3.Row
        legacy.execute("PRAGMA foreign_keys=OFF")
        _restore_legacy_authorities(legacy)
        legacy.execute(
            "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
            (raw, row_id, column_id),
        )
        legacy.execute(
            "UPDATE current_cells SET value=? WHERE row_id=? AND column_id=?",
            (raw, row_id, column_id),
        )
        legacy.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (_TYPED_VALUES_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
        )

    migrated = Project(path)
    assert migrated.get_values(sheet_id, column_id) == {row_id: "\ud800"}
    migrated.close()


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


def test_current_cell_values_join_flattens_to_exact_head_key() -> None:
    db = _database()
    plan = "\n".join(
        str(row[3])
        for row in db.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT r.id,live.value FROM rows AS r "
            "LEFT JOIN current_cell_values AS live "
            "ON live.column_id=10 AND live.row_id=r.id "
            "WHERE r.sheet_id=1 ORDER BY r.position"
        )
    )
    assert (
        "SEARCH head USING INDEX idx_current_cells_column_row "
        "(column_id=? AND row_id=?)" in plan
    )
    assert "MATERIALIZE current_cell_values" not in plan
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


def test_tolerant_historical_rows_decode_valid_legacy_json_and_keep_malformed_raw() -> (
    None
):
    db = _database()
    db.executemany(
        "INSERT INTO rows (id,sheet_id,position) VALUES (?,1,?)",
        [(102, 3), (103, 4)],
    )
    db.execute("INSERT INTO ops (id,kind,spec) VALUES (1,'map.test','{}')")
    db.execute(
        "INSERT INTO runs (id,op_id,sheet_id,action_kind) VALUES (20,1,1,'map.test')"
    )
    db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
        "VALUES (20,10,'value','test','create','active','claim')"
    )
    db.executemany(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value_kind,value,outcome,publication_effect) "
        "VALUES (20,?,10,'legacy_invalid',?,'ok','publish_value')",
        [(102, '"\\ud800"'), (103, '{"broken":')],
    )

    rows = RunResultStore(_Project(db)).decoded_result_rows(
        [(20, 102, 10), (20, 103, 10)], tolerate_decode_errors=True
    )

    assert rows[(20, 102, 10)]["value"] == "\ud800"
    assert rows[(20, 103, 10)]["value"] == '{"broken":'

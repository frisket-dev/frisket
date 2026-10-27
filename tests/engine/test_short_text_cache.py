from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from frisket.engine.store import Project
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.engine.store.short_text_cache_migration import (
    SHORT_TEXT_CACHE_FROM_DIGEST,
    SHORT_TEXT_CACHE_TO_DIGEST,
    migrate_short_text_cache,
)
from frisket.querysets import (
    resolve_sheet_filter_rows,
    sheet_row_scope_plan,
    sheet_row_scope_query,
)
from test_prepared_content_readers import _stage_document_and_page


def old_cache(p):
    p.db.execute("DROP INDEX idx_current_cells_numeric_value")
    p.db.execute(
        "UPDATE current_cells SET inline_value_kind=NULL,inline_value=NULL WHERE inline_value_kind='text'"
    )
    p.db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (SHORT_TEXT_CACHE_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    p.db.commit()


def test_cache_is_per_cell_and_counts_utf8_bytes(tmp_path):
    p = Project.create(tmp_path / "bounded")
    try:
        s = p.add_sheet("Text")
        c = p.add_column(s, "body", type="text")
        values = ["a"] * 99 + ["x" * 1000, "x" * 512, "x" * 513, "é" * 256, "é" * 257]
        rows = p.add_rows(s, [{"body": v} for v in values], {"body": c})
        expected = dict(zip(rows, values, strict=True))
        assert p.get_values(s, c) == expected
        cached = {
            r[0]: r[1]
            for r in p.db.execute(
                "SELECT row_id,inline_value FROM current_cells WHERE inline_value_kind='text'"
            )
        }
        assert cached == {
            r: v for r, v in expected.items() if len(v.encode("utf-8")) <= 512
        }
        p.apply_edits([{"row_id": rows[0], "column_id": c, "value": "x" * 1000}])
        assert (
            p.db.execute(
                "SELECT inline_value_kind FROM current_cells WHERE row_id=? AND column_id=?",
                (rows[0], c),
            ).fetchone()[0]
            is None
        )
        p.undo()
        assert p.get_values(s, c) == expected
        p.redo()
        assert p.get_values(s, c)[rows[0]] == "x" * 1000
    finally:
        p.close()


def test_upgrade_preserves_prepared_refs_and_reopens(tmp_path):
    p = Project.create(tmp_path / "upgrade")
    path = p.path
    s = p.add_sheet("Docs")
    c = p.add_column(s, "body", type="text")
    doc, page = _stage_document_and_page(p)
    rows = p.add_rows(
        s,
        [
            {"body": "short"},
            {"body": "é" * 256},
            {"body": "long" * 1000},
            {"body": doc},
            {"body": page},
        ],
        {"body": c},
        commit=False,
    )
    p.db.commit()
    before = p.get_values_with_refs(s, c, rows)
    old_cache(p)
    # Backfill must not expand prepared text, even a short single-page selection.
    p.db.set_authorizer(
        lambda action, a, b, d, source: (
            sqlite3.SQLITE_DENY
            if source == "prepared_content_ref_values"
            else sqlite3.SQLITE_OK
        )
    )
    migrate_short_text_cache(p.db)
    p.db.set_authorizer(None)
    assert p.get_values_with_refs(s, c, rows) == before
    assert (
        p.db.execute(
            "SELECT inline_value_kind FROM current_cells WHERE row_id=? AND column_id=?",
            (rows[3], c),
        ).fetchone()[0]
        is None
    )
    assert (
        p.get_meta(SCHEMA_DIGEST_META_KEY)
        == SHORT_TEXT_CACHE_TO_DIGEST
        == SCHEMA_DIGEST
    )
    p.close()
    p = Project(path)
    try:
        assert p.get_values_with_refs(s, c, rows) == before
        assert p.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert p.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        p.close()


def test_a102_upgrade_chains_pdf_split_and_short_text_cache(tmp_path):
    path = tmp_path / "a102"
    with closing(Project.create(path)) as p:
        sheet = p.add_sheet("Data")
        column = p.add_column(sheet, "body", type="text")
        rows = p.add_rows(sheet, [{"body": "short"}], {"body": column})
        old_cache(p)
        p.db.execute("DROP TABLE import_session_pdf_sources")
        p.db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (
                "frisket.schema.v1:887874773c58ac58982913e6604d9ec9",
                SCHEMA_DIGEST_META_KEY,
            ),
        )
        p.db.commit()
    with closing(Project(path)) as p:
        assert p.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        assert p.get_values(sheet, column) == {rows[0]: "short"}
        assert (
            p.db.execute("SELECT COUNT(*) FROM import_session_pdf_sources").fetchone()[
                0
            ]
            == 0
        )
        assert (
            p.db.execute(
                "SELECT inline_value FROM current_cells WHERE column_id=? AND row_id=?",
                (column, rows[0]),
            ).fetchone()[0]
            == "short"
        )
        assert (
            p.db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_current_cells_numeric_value'"
            ).fetchone()
            is not None
        )


def test_upgrade_rolls_back_cache_and_stamp_on_index_failure(tmp_path):
    p = Project.create(tmp_path / "rollback")
    try:
        s = p.add_sheet("Data")
        c = p.add_column(s, "body", type="text")
        p.add_rows(s, [{"body": "short"}], {"body": c})
        old_cache(p)
        p.db.set_authorizer(
            lambda action, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_INDEX
                else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            migrate_short_text_cache(p.db)
        p.db.set_authorizer(None)
        assert p.get_meta(SCHEMA_DIGEST_META_KEY) == SHORT_TEXT_CACHE_FROM_DIGEST
        assert (
            p.db.execute(
                "SELECT inline_value_kind FROM current_cells WHERE column_id=?", (c,)
            ).fetchone()[0]
            is None
        )
        migrate_short_text_cache(p.db)
        assert p.get_values(s, c)
    finally:
        p.close()


@pytest.mark.parametrize("kind", ["integer", "number"])
@pytest.mark.parametrize(
    "predicate", [{"gte": "1"}, {"lte": "3"}, {"between": {"start": "1", "end": "3"}}]
)
def test_numeric_range_index_matches_general_reader(tmp_path, kind, predicate):
    p = Project.create(tmp_path / "ranges")
    try:
        s = p.add_sheet("Numbers")
        c = p.add_column(s, "n", type=kind)
        values = [None, True, "2", "oops", 1, 1.5, 2.0, 3, 4, 2**80, -(2**80), -0.0]
        rows = p.add_rows(s, [{"n": v} for v in values], {"n": c})
        p.apply_edits([{"row_id": rows[0], "column_id": c, "value": 2}])
        f = json.dumps({"n": predicate})
        _, where, params, order, order_params = sheet_row_scope_query(p, s, filter_=f)
        expected = [
            r[0]
            for r in p.db.execute(
                "SELECT r.id FROM rows r WHERE "
                + where
                + " ORDER BY "
                + ",".join(order),
                [*params, *order_params],
            )
        ]
        actual = resolve_sheet_filter_rows(p, s, filter_=f, limit=100)
        assert actual.row_ids == expected
        plan = sheet_row_scope_plan(p, s, filter_=f)
        explain = p.db.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM "
            + plan.filter_from_sql
            + " WHERE "
            + plan.where_sql,
            [*plan.filter_join_params, *plan.where_params],
        ).fetchall()
        assert any(
            "idx_current_cells_numeric_value" in row[3] and "inline_value" in row[3]
            for row in explain
        ), [tuple(r) for r in explain]
    finally:
        p.close()

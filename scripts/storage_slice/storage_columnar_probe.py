#!/usr/bin/env python3
"""Matched SQLite-current-cells and DuckDB/Parquet analytical probe.

This is a research runner, not a Frisket storage adapter.  It deliberately
measures direct engine kernels on the same rebuildable current-value slice.
The actual Project/API measurements remain separate evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from typing import Any, Callable, Iterable

import duckdb
import pyarrow as pa


COLUMNS = {
    "record_id": 1,
    "category": 2,
    "amount": 3,
    "published_at": 4,
    "status": 5,
}
CATEGORIES = (
    "contracts",
    "courts",
    "education",
    "environment",
    "health",
    "housing",
    "labor",
    "transport",
)
CATEGORY_THRESHOLDS = (38, 59, 73, 83, 90, 95, 98, 100)
MAX_OWNED_BYTES = 1024**3


def category_for(row_id: int) -> str:
    bucket = row_id % 100
    return next(
        category
        for category, threshold in zip(CATEGORIES, CATEGORY_THRESHOLDS, strict=True)
        if bucket < threshold
    )


def amount_for(row_id: int) -> int | str | None:
    state = row_id % 10
    if state == 0:
        return None
    if state == 1:
        return "not-stated"
    return ((row_id * 7919) % 9_000_000) + 10_000


def date_for(row_id: int) -> str:
    return f"2026-{row_id % 12 + 1:02d}-{row_id % 28 + 1:02d}"


def json_value(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def create_sqlite_fixture(
    path: Path, rows: int, batch_rows: int, *, ordinary_rowid: bool
) -> dict[str, Any]:
    started = time.perf_counter()
    db = sqlite3.connect(path)
    try:
        current_table = (
            """
            CREATE TABLE current_cells (
              id INTEGER PRIMARY KEY,
              column_id INTEGER NOT NULL,
              row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
              value TEXT,
              validity TEXT NOT NULL CHECK(validity IN ('valid','missing','invalid'))
            );
            CREATE UNIQUE INDEX uq_current_cells_column_row
              ON current_cells(column_id,row_id);
            CREATE INDEX idx_current_cells_row
              ON current_cells(row_id,column_id);
            """
            if ordinary_rowid
            else """
            CREATE TABLE current_cells (
              column_id INTEGER NOT NULL,
              row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
              value TEXT,
              validity TEXT NOT NULL CHECK(validity IN ('valid','missing','invalid')),
              PRIMARY KEY (column_id, row_id)
            ) WITHOUT ROWID;
            CREATE INDEX idx_current_cells_row ON current_cells(row_id);
            """
        )
        db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE rows (
              id INTEGER PRIMARY KEY,
              position INTEGER NOT NULL UNIQUE
            );
            """
            + current_table
        )
        for start in range(1, rows + 1, batch_rows):
            stop = min(rows + 1, start + batch_rows)
            row_batch = [(row_id, row_id - 1) for row_id in range(start, stop)]
            cell_batch: list[tuple[int, int, str, str]] = []
            for row_id in range(start, stop):
                cell_batch.extend(
                    (
                        (COLUMNS["record_id"], row_id, json_value(row_id), "valid"),
                        (
                            COLUMNS["category"],
                            row_id,
                            json_value(category_for(row_id)),
                            "valid",
                        ),
                        (
                            COLUMNS["published_at"],
                            row_id,
                            json_value(date_for(row_id)),
                            "valid",
                        ),
                        (
                            COLUMNS["status"],
                            row_id,
                            json_value(("open", "review", "closed")[row_id % 3]),
                            "valid",
                        ),
                    )
                )
                amount = amount_for(row_id)
                if amount is not None:
                    cell_batch.append(
                        (
                            COLUMNS["amount"],
                            row_id,
                            json_value(amount),
                            "invalid" if amount == "not-stated" else "valid",
                        )
                    )
            with db:
                db.executemany("INSERT INTO rows(id,position) VALUES (?,?)", row_batch)
                db.executemany(
                    "INSERT INTO current_cells(column_id,row_id,value,validity) "
                    "VALUES (?,?,?,?)",
                    cell_batch,
                )
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    return {
        "seconds": time.perf_counter() - started,
        "database_bytes": path.stat().st_size,
        "rows": rows,
        "cells": rows * len(COLUMNS) - rows // 10,
        "layout": "ordinary_rowid" if ordinary_rowid else "without_rowid",
    }


WIDE_SQL = f"""
SELECT r.id AS row_id,
       r.position AS position,
       CAST(json_extract(record.value, '$') AS INTEGER) AS record_id,
       CAST(json_extract(category.value, '$') AS TEXT) AS category,
       CASE WHEN amount.validity='valid'
            THEN CAST(json_extract(amount.value, '$') AS INTEGER) END AS amount,
       COALESCE(amount.validity, 'missing') AS amount_quality,
       CAST(json_extract(published.value, '$') AS TEXT) AS published_at,
       CAST(json_extract(status.value, '$') AS TEXT) AS status
FROM rows r
JOIN current_cells record
  ON record.row_id=r.id AND record.column_id={COLUMNS["record_id"]}
JOIN current_cells category
  ON category.row_id=r.id AND category.column_id={COLUMNS["category"]}
LEFT JOIN current_cells amount
  ON amount.row_id=r.id AND amount.column_id={COLUMNS["amount"]}
JOIN current_cells published
  ON published.row_id=r.id AND published.column_id={COLUMNS["published_at"]}
JOIN current_cells status
  ON status.row_id=r.id AND status.column_id={COLUMNS["status"]}
ORDER BY r.id
"""
WIDE_SQL_WITHOUT_ORDER = WIDE_SQL.removesuffix("ORDER BY r.id\n")


def create_duck_table(db: Any) -> None:
    db.execute(
        """
        CREATE TABLE analytical_current (
          row_id BIGINT PRIMARY KEY,
          position BIGINT NOT NULL,
          record_id BIGINT NOT NULL,
          category VARCHAR NOT NULL,
          amount BIGINT,
          amount_quality VARCHAR NOT NULL,
          published_at VARCHAR NOT NULL,
          status VARCHAR NOT NULL
        )
        """
    )


def build_sqlite_projection(
    source_path: Path,
    final_path: Path,
    batch_rows: int,
    *,
    wide_sql: str,
) -> dict[str, Any]:
    temporary = final_path.with_suffix(final_path.suffix + ".next")
    if temporary.exists():
        temporary.unlink()
    old_bytes = final_path.stat().st_size if final_path.exists() else 0
    started = time.perf_counter()
    source = open_readonly(source_path)
    target = sqlite3.connect(temporary)
    rows = 0
    try:
        target.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE analytical_current (
              row_id INTEGER PRIMARY KEY,
              position INTEGER NOT NULL UNIQUE,
              record_id INTEGER NOT NULL,
              category TEXT NOT NULL,
              amount INTEGER,
              amount_quality TEXT NOT NULL,
              published_at TEXT NOT NULL,
              status TEXT NOT NULL
            );
            """
        )
        cursor = source.execute(wide_sql)
        while batch := cursor.fetchmany(batch_rows):
            with target:
                target.executemany(
                    "INSERT INTO analytical_current VALUES (?,?,?,?,?,?,?,?)", batch
                )
            rows += len(batch)
        target.executescript(
            """
            CREATE INDEX idx_analytical_record
              ON analytical_current(record_id,position,row_id);
            CREATE INDEX idx_analytical_category
              ON analytical_current(category);
            CREATE INDEX idx_analytical_valid_amount
              ON analytical_current(amount,position,row_id)
              WHERE amount_quality='valid';
            PRAGMA optimize;
            """
        )
    finally:
        source.close()
        target.close()
    seconds = time.perf_counter() - started
    new_bytes = temporary.stat().st_size
    os.replace(temporary, final_path)
    return {
        "seconds": seconds,
        "rows": rows,
        "artifact_bytes": new_bytes,
        "peak_replacement_bytes": old_bytes + new_bytes,
        "indexes": [
            "UNIQUE(position)",
            "record_id,position,row_id",
            "category",
            "amount,position,row_id WHERE amount_quality='valid'",
        ],
    }


def arrow_batches(
    sqlite_path: Path,
    batch_rows: int,
    *,
    wide_sql: str = WIDE_SQL,
    edits: dict[int, int] | None = None,
) -> Iterable[pa.Table]:
    db = open_readonly(sqlite_path)
    try:
        cursor = db.execute(wide_sql)
        while rows := cursor.fetchmany(batch_rows):
            columns = list(zip(*rows, strict=True))
            amounts = list(columns[4])
            qualities = list(columns[5])
            if edits:
                for index, row_id in enumerate(columns[0]):
                    if row_id in edits:
                        amounts[index] = edits[row_id]
                        qualities[index] = "valid"
            yield pa.table(
                {
                    "row_id": pa.array(columns[0], type=pa.int64()),
                    "position": pa.array(columns[1], type=pa.int64()),
                    "record_id": pa.array(columns[2], type=pa.int64()),
                    "category": pa.array(columns[3], type=pa.string()),
                    "amount": pa.array(amounts, type=pa.int64()),
                    "amount_quality": pa.array(qualities, type=pa.string()),
                    "published_at": pa.array(columns[6], type=pa.string()),
                    "status": pa.array(columns[7], type=pa.string()),
                }
            )
    finally:
        db.close()


def load_duck_table(
    db: Any,
    sqlite_path: Path,
    batch_rows: int,
    *,
    wide_sql: str = WIDE_SQL,
    edits: dict[int, int] | None = None,
) -> None:
    create_duck_table(db)
    for index, batch in enumerate(
        arrow_batches(sqlite_path, batch_rows, wide_sql=wide_sql, edits=edits)
    ):
        name = f"source_batch_{index}"
        db.register(name, batch)
        try:
            db.execute(f"INSERT INTO analytical_current SELECT * FROM {name}")
        finally:
            db.unregister(name)


def configure_duck(db: Any, scratch: Path) -> None:
    scratch.mkdir(parents=True, exist_ok=True)
    escaped = str(scratch).replace("'", "''")
    db.execute("SET threads=1")
    db.execute("SET memory_limit='256MB'")
    db.execute("SET preserve_insertion_order=false")
    db.execute(f"SET temp_directory='{escaped}'")


def build_duckdb(
    sqlite_path: Path,
    final_path: Path,
    scratch: Path,
    batch_rows: int,
    *,
    wide_sql: str = WIDE_SQL,
    edits: dict[int, int] | None = None,
) -> dict[str, Any]:
    temporary = final_path.with_suffix(final_path.suffix + ".next")
    if temporary.exists():
        temporary.unlink()
    old_bytes = final_path.stat().st_size if final_path.exists() else 0
    started = time.perf_counter()
    db = duckdb.connect(str(temporary))
    try:
        configure_duck(db, scratch)
        load_duck_table(db, sqlite_path, batch_rows, wide_sql=wide_sql, edits=edits)
        db.execute("CHECKPOINT")
    finally:
        db.close()
    build_seconds = time.perf_counter() - started
    new_bytes = temporary.stat().st_size
    peak_replacement_bytes = old_bytes + new_bytes
    os.replace(temporary, final_path)
    return {
        "seconds": build_seconds,
        "artifact_bytes": final_path.stat().st_size,
        "peak_replacement_bytes": peak_replacement_bytes,
    }


def build_parquet(
    sqlite_path: Path,
    final_path: Path,
    scratch: Path,
    batch_rows: int,
    row_group_size: int,
    *,
    wide_sql: str = WIDE_SQL,
    edits: dict[int, int] | None = None,
) -> dict[str, Any]:
    temporary = final_path.with_suffix(final_path.suffix + ".next")
    if temporary.exists():
        temporary.unlink()
    old_bytes = final_path.stat().st_size if final_path.exists() else 0
    started = time.perf_counter()
    db = duckdb.connect(":memory:")
    try:
        configure_duck(db, scratch)
        load_duck_table(db, sqlite_path, batch_rows, wide_sql=wide_sql, edits=edits)
        escaped = str(temporary).replace("'", "''")
        db.execute(
            "COPY analytical_current TO "
            f"'{escaped}' (FORMAT parquet, COMPRESSION zstd, "
            f"ROW_GROUP_SIZE {row_group_size})"
        )
    finally:
        db.close()
    build_seconds = time.perf_counter() - started
    new_bytes = temporary.stat().st_size
    peak_replacement_bytes = old_bytes + new_bytes
    os.replace(temporary, final_path)
    return {
        "seconds": build_seconds,
        "artifact_bytes": final_path.stat().st_size,
        "peak_replacement_bytes": peak_replacement_bytes,
    }


def sqlite_wide_cte(extra_where: str = "") -> str:
    return f"WITH wide AS ({WIDE_SQL_WITHOUT_ORDER}) {extra_where}"


def sqlite_queries(rows: int) -> dict[str, str]:
    narrow = rows - max(1, math.ceil(rows * 0.001)) + 1
    base = sqlite_wide_cte()
    analytics = """
    , ranked AS (
      SELECT category, amount,
             ROW_NUMBER() OVER (PARTITION BY category ORDER BY amount) AS rn,
             COUNT(*) OVER (PARTITION BY category) AS n
      FROM wide WHERE amount_quality='valid' {ranked_and}
    ), medians AS (
      SELECT category,
             CASE WHEN MAX(n) % 2 = 1
                  THEN MAX(CASE WHEN rn=(n + 1) / 2 THEN amount END)
                  ELSE MAX(CASE WHEN rn=n / 2 THEN amount / 2.0 END)
                     + MAX(CASE WHEN rn=n / 2 + 1 THEN amount / 2.0 END)
             END AS median
      FROM ranked GROUP BY category
    ), aggregated AS (
      SELECT category, COUNT(*) AS rows, COUNT(amount) AS valid,
             SUM(amount_quality='missing') AS missing,
             SUM(amount_quality='invalid') AS invalid,
             SUM(amount) AS total, AVG(amount) AS mean
      FROM wide {where} GROUP BY category
    )
    SELECT a.category,a.rows,a.valid,a.missing,a.invalid,a.total,a.mean,m.median
    FROM aggregated a JOIN medians m USING(category) {outer_where}
    ORDER BY a.category
    """
    return {
        "read_middle_50": base
        + f" SELECT * FROM wide ORDER BY row_id LIMIT 50 OFFSET {max(0, rows // 2 - 25)}",
        "filter_0_1_percent": base
        + f" SELECT record_id,COUNT(*) OVER() FROM wide WHERE record_id>={narrow} "
        "ORDER BY record_id LIMIT 50",
        "sort_amount_asc": base
        + " SELECT record_id,amount FROM wide WHERE amount_quality='valid' "
        "ORDER BY amount ASC, row_id ASC LIMIT 50",
        "sort_amount_desc": base
        + " SELECT record_id,amount FROM wide WHERE amount_quality='valid' "
        "ORDER BY amount DESC, row_id ASC LIMIT 50",
        "grouped_analytics": base
        + analytics.format(where="", ranked_and="", outer_where=""),
        "filtered_10_percent": base
        + analytics.format(
            where="WHERE category='environment'",
            ranked_and="AND category='environment'",
            outer_where="WHERE a.category='environment'",
        ),
    }


def duck_queries(source: str, rows: int) -> dict[str, str]:
    narrow = rows - max(1, math.ceil(rows * 0.001)) + 1
    analytics = f"""
    SELECT category,COUNT(*) AS rows,COUNT(amount) AS valid,
           COUNT(*) FILTER (WHERE amount_quality='missing') AS missing,
           COUNT(*) FILTER (WHERE amount_quality='invalid') AS invalid,
           SUM(amount) AS total,AVG(amount) AS mean,MEDIAN(amount) AS median
    FROM {source} {{where}} GROUP BY category ORDER BY category
    """
    return {
        "read_middle_50": f"SELECT * FROM {source} ORDER BY position,row_id LIMIT 50 "
        f"OFFSET {max(0, rows // 2 - 25)}",
        "filter_0_1_percent": f"SELECT record_id,COUNT(*) OVER() FROM {source} "
        f"WHERE record_id>={narrow} ORDER BY position,row_id LIMIT 50",
        "sort_amount_asc": f"SELECT record_id,amount FROM {source} "
        "WHERE amount_quality='valid' ORDER BY amount ASC,position,row_id LIMIT 50",
        "sort_amount_desc": f"SELECT record_id,amount FROM {source} "
        "WHERE amount_quality='valid' ORDER BY amount DESC,position,row_id LIMIT 50",
        "grouped_analytics": analytics.format(where=""),
        "filtered_10_percent": analytics.format(where="WHERE category='environment'"),
    }


def timed(call: Callable[[], Any], samples: int) -> tuple[Any, dict[str, Any]]:
    call()
    durations: list[float] = []
    result = None
    for _ in range(samples):
        started = time.perf_counter()
        result = call()
        durations.append((time.perf_counter() - started) * 1000)
    assert result is not None
    return result, {
        "samples": samples,
        "first_ms": durations[0],
        "median_ms": statistics.median(durations),
        "max_ms": max(durations),
    }


def normalize(rows: Iterable[Iterable[Any]]) -> list[list[Any]]:
    return [[value for value in row] for row in rows]


def assert_equivalent(left: Any, right: Any, path: str = "result") -> None:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if not math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-9):
            raise AssertionError(f"{path}: {left!r} != {right!r}")
        return
    if type(left) is not type(right):
        raise AssertionError(f"{path}: {type(left).__name__} != {type(right).__name__}")
    if isinstance(left, list):
        if len(left) != len(right):
            raise AssertionError(f"{path}: lengths {len(left)} != {len(right)}")
        for index, (lvalue, rvalue) in enumerate(zip(left, right, strict=True)):
            assert_equivalent(lvalue, rvalue, f"{path}[{index}]")
        return
    if left != right:
        raise AssertionError(f"{path}: {left!r} != {right!r}")


def run_queries(
    sqlite_path: Path,
    sqlite_without_path: Path,
    duckdb_path: Path,
    parquet_path: Path,
    rows: int,
    samples: int,
    scratch: Path,
) -> dict[str, Any]:
    sqlite_db = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sqlite_without_db = sqlite3.connect(f"file:{sqlite_without_path}?mode=ro", uri=True)
    duck_db = duckdb.connect(str(duckdb_path), read_only=True)
    parquet_db = duckdb.connect(":memory:")
    try:
        configure_duck(parquet_db, scratch)
        parquet_literal = str(parquet_path).replace("'", "''")
        engines = {
            "sqlite_rowid_current_cells": (sqlite_db, sqlite_queries(rows)),
            "sqlite_without_rowid_current_cells": (
                sqlite_without_db,
                sqlite_queries(rows),
            ),
            "duckdb_native": (
                duck_db,
                duck_queries("analytical_current", rows),
            ),
            "duckdb_parquet": (
                parquet_db,
                duck_queries(f"read_parquet('{parquet_literal}')", rows),
            ),
        }
        report: dict[str, Any] = {name: {} for name in engines}
        reference: dict[str, Any] = {}
        for engine_name, (db, queries) in engines.items():
            for query_name, sql in queries.items():
                result, timings = timed(
                    lambda sql=sql, db=db: db.execute(sql).fetchall(), samples
                )
                normalized = normalize(result)
                if query_name not in reference:
                    reference[query_name] = normalized
                else:
                    assert_equivalent(reference[query_name], normalized, query_name)
                report[engine_name][query_name] = {
                    **timings,
                    "returned": len(normalized),
                }
        return report
    finally:
        sqlite_db.close()
        sqlite_without_db.close()
        duck_db.close()
        parquet_db.close()


def edit_row_ids(rows: int, count: int) -> list[int]:
    candidates = [row_id for row_id in range(2, rows + 1) if row_id % 10 >= 2]
    if count > len(candidates):
        raise ValueError(
            f"{count} edits require at least {math.ceil(count * 1.25)} rows"
        )
    if count == 1:
        return [candidates[0]]
    step = (len(candidates) - 1) / (count - 1)
    return [candidates[round(index * step)] for index in range(count)]


def apply_edits(
    sqlite_path: Path,
    sqlite_without_path: Path,
    duckdb_path: Path,
    row_ids: list[int],
    scratch: Path,
) -> dict[str, Any]:
    values = [(20_000_000 + index, row_id) for index, row_id in enumerate(row_ids)]

    def update_sqlite(path: Path) -> float:
        sqlite_db = sqlite3.connect(path)
        started = time.perf_counter()
        try:
            with sqlite_db:
                sqlite_db.executemany(
                    "UPDATE current_cells SET value=?,validity='valid' "
                    f"WHERE column_id={COLUMNS['amount']} AND row_id=?",
                    [(json_value(value), row_id) for value, row_id in values],
                )
            return time.perf_counter() - started
        finally:
            sqlite_db.close()

    sqlite_seconds = update_sqlite(sqlite_path)
    sqlite_without_seconds = update_sqlite(sqlite_without_path)

    duck_db = duckdb.connect(str(duckdb_path))
    try:
        configure_duck(duck_db, scratch)
        started = time.perf_counter()
        duck_db.execute("CREATE TEMP TABLE edits(row_id BIGINT,amount BIGINT)")
        duck_db.executemany(
            "INSERT INTO edits VALUES (?,?)", [(r, v) for v, r in values]
        )
        duck_db.execute(
            "UPDATE analytical_current SET amount=edits.amount,amount_quality='valid' "
            "FROM edits WHERE analytical_current.row_id=edits.row_id"
        )
        duck_db.execute("CHECKPOINT")
        duck_seconds = time.perf_counter() - started
    finally:
        duck_db.close()
    return {
        "cells": len(row_ids),
        "sqlite_authority_seconds": sqlite_seconds,
        "sqlite_without_rowid_seconds": sqlite_without_seconds,
        "duckdb_incremental_seconds": duck_seconds,
        "duckdb_artifact_bytes": duckdb_path.stat().st_size,
    }


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def discover_actual_project(project_db: Path, sheet_name: str | None) -> dict[str, Any]:
    required = {"record_id", "category", "amount", "published_at", "status"}
    db = open_readonly(project_db)
    try:
        sheets = db.execute(
            "SELECT s.id,s.name,COUNT(r.id) AS row_count "
            "FROM sheets s LEFT JOIN rows r ON r.sheet_id=s.id AND r.hidden=0 "
            "WHERE s.hidden=0 GROUP BY s.id,s.name ORDER BY row_count DESC,s.id"
        ).fetchall()
        candidates = []
        for sheet in sheets:
            columns = db.execute(
                "SELECT id,name,type,position FROM columns "
                "WHERE sheet_id=? AND hidden=0 ORDER BY position,id",
                (sheet["id"],),
            ).fetchall()
            by_name = {str(column["name"]): column for column in columns}
            if required <= set(by_name) and (
                sheet_name is None or str(sheet["name"]) == sheet_name
            ):
                candidates.append((sheet, by_name))
        if len(candidates) != 1:
            names = [str(sheet["name"]) for sheet, _columns in candidates]
            raise ValueError(
                "expected exactly one visible sheet with the analytical columns; "
                f"found {names!r}"
            )
        sheet, by_name = candidates[0]
        expected_types = {
            "record_id": "integer",
            "category": "category",
            "amount": "integer",
            "published_at": "date",
            "status": "category",
        }
        actual_types = {name: str(by_name[name]["type"]) for name in required}
        if actual_types != expected_types:
            raise ValueError(f"unexpected analytical column types: {actual_types!r}")
        column_ids = {name: int(by_name[name]["id"]) for name in required}
        sheet_id = int(sheet["id"])
        row_count = int(sheet["row_count"])
        cell_count = int(
            db.execute(
                "SELECT COUNT(*) FROM current_cells c JOIN rows r ON r.id=c.row_id "
                "WHERE r.sheet_id=? AND r.hidden=0",
                (sheet_id,),
            ).fetchone()[0]
        )
        op_cursor = int(
            db.execute("SELECT value FROM meta WHERE key='op_cursor'").fetchone()[0]
        )
        return {
            "sheet_id": sheet_id,
            "sheet_name": str(sheet["name"]),
            "row_count": row_count,
            "current_cell_count": cell_count,
            "op_cursor": op_cursor,
            "columns": column_ids,
        }
    finally:
        db.close()


def actual_wide_sql(info: dict[str, Any]) -> str:
    columns = info["columns"]
    sheet_id = int(info["sheet_id"])
    return f"""
SELECT r.id AS row_id,
       r.position AS position,
       CAST(json_extract(record.value, '$') AS INTEGER) AS record_id,
       CAST(json_extract(category.value, '$') AS TEXT) AS category,
       CASE WHEN amount.validity='valid'
            THEN CAST(json_extract(amount.value, '$') AS INTEGER) END AS amount,
       COALESCE(amount.validity, 'missing') AS amount_quality,
       CAST(json_extract(published.value, '$') AS TEXT) AS published_at,
       CAST(json_extract(status.value, '$') AS TEXT) AS status
FROM rows r
JOIN current_cells record
  ON record.row_id=r.id AND record.column_id={int(columns["record_id"])}
JOIN current_cells category
  ON category.row_id=r.id AND category.column_id={int(columns["category"])}
LEFT JOIN current_cells amount
  ON amount.row_id=r.id AND amount.column_id={int(columns["amount"])}
JOIN current_cells published
  ON published.row_id=r.id AND published.column_id={int(columns["published_at"])}
JOIN current_cells status
  ON status.row_id=r.id AND status.column_id={int(columns["status"])}
WHERE r.sheet_id={sheet_id} AND r.hidden=0
ORDER BY r.position,r.id
"""


def actual_workload_facts(
    project_db: Path, wide_sql: str, row_count: int
) -> dict[str, Any]:
    wide = wide_sql.rsplit("ORDER BY", 1)[0]
    top_count = max(1, math.ceil(row_count * 0.001))
    db = open_readonly(project_db)
    try:
        projected_rows = int(
            db.execute(
                "WITH wide AS (" + wide + ") SELECT COUNT(*) FROM wide"
            ).fetchone()[0]
        )
        if projected_rows != row_count:
            raise ValueError(
                "analytical projection did not cover every visible row: "
                f"{projected_rows} != {row_count}"
            )
        threshold_row = db.execute(
            "WITH wide AS ("
            + wide
            + ") SELECT record_id FROM wide ORDER BY record_id DESC "
            "LIMIT 1 OFFSET ?",
            (top_count - 1,),
        ).fetchone()
        if threshold_row is None:
            raise ValueError("actual Project has no analytical rows")
        categories = db.execute(
            "WITH wide AS (" + wide + ") SELECT category,COUNT(*) AS count FROM wide "
            "GROUP BY category ORDER BY category"
        ).fetchall()
        category_counts = {
            str(row["category"]): int(row["count"]) for row in categories
        }
        filtered_category = (
            "environment"
            if category_counts.get("environment", 0)
            else min(
                category_counts,
                key=lambda name: abs(category_counts[name] - row_count // 10),
            )
        )
        quality = db.execute(
            "WITH wide AS ("
            + wide
            + ") SELECT amount_quality,COUNT(*) FROM wide GROUP BY amount_quality"
        ).fetchall()
        narrow_actual_rows = int(
            db.execute(
                "WITH wide AS ("
                + wide
                + ") SELECT COUNT(*) FROM wide WHERE record_id>=?",
                (int(threshold_row["record_id"]),),
            ).fetchone()[0]
        )
        return {
            "narrow_threshold": int(threshold_row["record_id"]),
            "narrow_rows": narrow_actual_rows,
            "filtered_category": filtered_category,
            "category_counts": category_counts,
            "amount_quality_counts": {str(row[0]): int(row[1]) for row in quality},
        }
    finally:
        db.close()


def actual_sqlite_queries(
    wide_sql: str, rows: int, facts: dict[str, Any]
) -> dict[str, str]:
    wide = wide_sql.rsplit("ORDER BY", 1)[0]
    base = f"WITH wide AS ({wide})"
    threshold = int(facts["narrow_threshold"])
    category = str(facts["filtered_category"]).replace("'", "''")
    analytics = """
    , ranked AS (
      SELECT category, amount,
             ROW_NUMBER() OVER (PARTITION BY category ORDER BY amount) AS rn,
             COUNT(*) OVER (PARTITION BY category) AS n
      FROM wide WHERE amount_quality='valid' {ranked_and}
    ), medians AS (
      SELECT category,
             CASE WHEN MAX(n) % 2 = 1
                  THEN MAX(CASE WHEN rn=(n + 1) / 2 THEN amount END)
                  ELSE MAX(CASE WHEN rn=n / 2 THEN amount / 2.0 END)
                     + MAX(CASE WHEN rn=n / 2 + 1 THEN amount / 2.0 END)
             END AS median
      FROM ranked GROUP BY category
    ), aggregated AS (
      SELECT category, COUNT(*) AS rows, COUNT(amount) AS valid,
             SUM(amount_quality='missing') AS missing,
             SUM(amount_quality='invalid') AS invalid,
             SUM(amount) AS total, AVG(amount) AS mean
      FROM wide {where} GROUP BY category
    )
    SELECT a.category,a.rows,a.valid,a.missing,a.invalid,a.total,a.mean,m.median
    FROM aggregated a JOIN medians m USING(category) {outer_where}
    ORDER BY a.category
    """
    return {
        "read_middle_50": base
        + f" SELECT * FROM wide ORDER BY position,row_id LIMIT 50 OFFSET {max(0, rows // 2 - 25)}",
        "filter_0_1_percent": base
        + f" SELECT record_id,COUNT(*) OVER() FROM wide WHERE record_id>={threshold} "
        "ORDER BY position,row_id LIMIT 50",
        "sort_amount_asc": base
        + " SELECT record_id,amount FROM wide WHERE amount_quality='valid' "
        "ORDER BY amount ASC,position,row_id LIMIT 50",
        "sort_amount_desc": base
        + " SELECT record_id,amount FROM wide WHERE amount_quality='valid' "
        "ORDER BY amount DESC,position,row_id LIMIT 50",
        "grouped_analytics": base
        + analytics.format(where="", ranked_and="", outer_where=""),
        "filtered_10_percent": base
        + analytics.format(
            where=f"WHERE category='{category}'",
            ranked_and=f"AND category='{category}'",
            outer_where=f"WHERE a.category='{category}'",
        ),
    }


def actual_duck_queries(
    source: str, rows: int, facts: dict[str, Any]
) -> dict[str, str]:
    threshold = int(facts["narrow_threshold"])
    category = str(facts["filtered_category"]).replace("'", "''")
    analytics = f"""
    SELECT category,COUNT(*) AS rows,COUNT(amount) AS valid,
           COUNT(*) FILTER (WHERE amount_quality='missing') AS missing,
           COUNT(*) FILTER (WHERE amount_quality='invalid') AS invalid,
           SUM(amount) AS total,AVG(amount) AS mean,MEDIAN(amount) AS median
    FROM {source} {{where}} GROUP BY category ORDER BY category
    """
    return {
        "read_middle_50": f"SELECT * FROM {source} ORDER BY position,row_id LIMIT 50 "
        f"OFFSET {max(0, rows // 2 - 25)}",
        "filter_0_1_percent": f"SELECT record_id,COUNT(*) OVER() FROM {source} "
        f"WHERE record_id>={threshold} ORDER BY position,row_id LIMIT 50",
        "sort_amount_asc": f"SELECT record_id,amount FROM {source} "
        "WHERE amount_quality='valid' ORDER BY amount ASC,position,row_id LIMIT 50",
        "sort_amount_desc": f"SELECT record_id,amount FROM {source} "
        "WHERE amount_quality='valid' ORDER BY amount DESC,position,row_id LIMIT 50",
        "grouped_analytics": analytics.format(where=""),
        "filtered_10_percent": analytics.format(where=f"WHERE category='{category}'"),
    }


class ReadonlyProject:
    def __init__(self, project_db: Path):
        self.db_path = project_db

    def read_snapshot(self):
        return ReadonlySnapshot(self.db_path)


class ReadonlySnapshot:
    def __init__(self, project_db: Path):
        self.db = open_readonly(project_db)

    def __enter__(self):
        self.db.execute("BEGIN")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.db.in_transaction:
            self.db.rollback()
        self.db.close()

    def columns(self, sheet_id: int, include_hidden: bool = False):
        hidden = "" if include_hidden else "AND hidden=0"
        return self.db.execute(
            f"SELECT * FROM columns WHERE sheet_id=? {hidden} ORDER BY position,id",
            (sheet_id,),
        ).fetchall()

    @property
    def op_cursor(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='op_cursor'").fetchone()
        return int(row["value"] if row is not None else "0")

    def get_values_with_refs(self, *args: Any, **kwargs: Any):
        from frisket.engine.store import cells

        return cells.get_values_with_refs(self, *args, **kwargs)


def public_projected_rows(
    snapshot: ReadonlySnapshot,
    row_ids: list[int],
    info: dict[str, Any],
) -> list[list[Any]]:
    if not row_ids:
        return []
    placeholders = ",".join("?" for _ in row_ids)
    positions = {
        int(row["id"]): int(row["position"])
        for row in snapshot.db.execute(
            f"SELECT id,position FROM rows WHERE id IN ({placeholders})", row_ids
        )
    }
    values: dict[str, dict[int, Any]] = {}
    refs: dict[str, dict[int, dict[str, Any]]] = {}
    for name, column_id in info["columns"].items():
        values[name], refs[name] = snapshot.get_values_with_refs(
            info["sheet_id"],
            column_id,
            row_ids,
            preserve_invalid=True,
            include_validity=True,
        )
    result = []
    for row_id in row_ids:
        quality = refs["amount"].get(row_id, {}).get("validity", "missing")
        amount = values["amount"].get(row_id) if quality == "valid" else None
        result.append(
            [
                row_id,
                positions[row_id],
                values["record_id"].get(row_id),
                values["category"].get(row_id),
                amount,
                quality,
                values["published_at"].get(row_id),
                values["status"].get(row_id),
            ]
        )
    return result


def public_calls(
    project_db: Path, info: dict[str, Any], facts: dict[str, Any]
) -> dict[str, Callable[[], list[list[Any]]]]:
    from frisket.querysets import resolve_sheet_filter_rows
    from frisket.server.services.project_qa_analytics import evaluate_analytics

    owner = ReadonlyProject(project_db)
    sheet_id = int(info["sheet_id"])
    rows = int(info["row_count"])
    threshold = int(facts["narrow_threshold"])
    filtered_category = str(facts["filtered_category"])

    def grid(**kwargs: Any) -> list[list[Any]]:
        with owner.read_snapshot() as snapshot:
            rowset = resolve_sheet_filter_rows(snapshot, sheet_id, limit=50, **kwargs)
            return public_projected_rows(snapshot, rowset.row_ids, info)

    def filtered() -> list[list[Any]]:
        with owner.read_snapshot() as snapshot:
            rowset = resolve_sheet_filter_rows(
                snapshot,
                sheet_id,
                limit=50,
                filter_=json.dumps(
                    {"record_id": {"gte": str(threshold)}}, separators=(",", ":")
                ),
            )
            projected = public_projected_rows(snapshot, rowset.row_ids, info)
            return [[row[2], rowset.total] for row in projected]

    metrics = [
        {"id": "rows", "kind": "count"},
        {"id": "values", "kind": "value_count", "column_id": info["columns"]["amount"]},
        {
            "id": "missing",
            "kind": "missing_count",
            "column_id": info["columns"]["amount"],
        },
        {"id": "sum", "kind": "sum", "column_id": info["columns"]["amount"]},
        {"id": "mean", "kind": "mean", "column_id": info["columns"]["amount"]},
        {"id": "median", "kind": "median", "column_id": info["columns"]["amount"]},
    ]

    def analytics(filtered_only: bool) -> list[list[Any]]:
        request: dict[str, Any] = {
            "sheet_id": sheet_id,
            "metrics": metrics,
            "groups": [{"column_id": info["columns"]["category"]}],
            "sort": [{"kind": "group", "group_index": 0, "direction": "asc"}],
            "limit": 20,
        }
        if filtered_only:
            request["filter"] = {"category": {"eq": filtered_category}}
        result = evaluate_analytics(
            owner, request, {"kind": "sheet", "sheet_id": sheet_id}
        )
        rows_out = []
        for group in result["groups"]:
            category = group["group"][0]["value"]
            quality = group["quality"][str(info["columns"]["amount"])]
            rows_out.append(
                [
                    category,
                    group["metrics"]["rows"],
                    group["metrics"]["values"],
                    group["metrics"]["missing"],
                    quality["invalid"],
                    group["metrics"]["sum"],
                    group["metrics"]["mean"],
                    group["metrics"]["median"],
                ]
            )
        return rows_out

    return {
        "read_middle_50": lambda: grid(offset=max(0, rows // 2 - 25)),
        "filter_0_1_percent": filtered,
        "sort_amount_asc": lambda: [
            [row[2], row[4]]
            for row in grid(sort=json.dumps([{"column": "amount", "dir": "asc"}]))
        ],
        "sort_amount_desc": lambda: [
            [row[2], row[4]]
            for row in grid(sort=json.dumps([{"column": "amount", "dir": "desc"}]))
        ],
        "grouped_analytics": lambda: analytics(False),
        "filtered_10_percent": lambda: analytics(True),
    }


def run_actual_queries(
    project_db: Path,
    sqlite_projection_path: Path,
    duckdb_path: Path,
    parquet_path: Path,
    info: dict[str, Any],
    facts: dict[str, Any],
    wide_sql: str,
    samples: int,
    scratch: Path,
) -> dict[str, Any]:
    sqlite_db = open_readonly(project_db)
    sqlite_projection_db = open_readonly(sqlite_projection_path)
    duck_db = duckdb.connect(str(duckdb_path), read_only=True)
    parquet_db = duckdb.connect(":memory:")
    try:
        configure_duck(duck_db, scratch)
        configure_duck(parquet_db, scratch)
        parquet_literal = str(parquet_path).replace("'", "''")
        engines = {
            "sqlite_direct": (
                sqlite_db,
                actual_sqlite_queries(wide_sql, info["row_count"], facts),
            ),
            "sqlite_wide": (
                sqlite_projection_db,
                actual_sqlite_queries(
                    "SELECT row_id,position,record_id,category,amount,"
                    "amount_quality,published_at,status FROM analytical_current",
                    info["row_count"],
                    facts,
                ),
            ),
            "duckdb_native": (
                duck_db,
                actual_duck_queries("analytical_current", info["row_count"], facts),
            ),
            "duckdb_parquet": (
                parquet_db,
                actual_duck_queries(
                    f"read_parquet('{parquet_literal}')", info["row_count"], facts
                ),
            ),
        }
        report: dict[str, Any] = {name: {} for name in engines}
        reference: dict[str, Any] = {}
        for engine_name, (db, queries) in engines.items():
            for query_name, sql in queries.items():
                result, timings = timed(
                    lambda sql=sql, db=db: db.execute(sql).fetchall(), samples
                )
                normalized = normalize(result)
                if query_name not in reference:
                    reference[query_name] = normalized
                else:
                    assert_equivalent(reference[query_name], normalized, query_name)
                report[engine_name][query_name] = {
                    **timings,
                    "returned": len(normalized),
                }
        report["frisket_public"] = {}
        for query_name, call in public_calls(project_db, info, facts).items():
            result, timings = timed(call, samples)
            assert_equivalent(reference[query_name], result, f"public.{query_name}")
            report["frisket_public"][query_name] = {
                **timings,
                "returned": len(result),
            }
        return report
    finally:
        sqlite_db.close()
        sqlite_projection_db.close()
        duck_db.close()
        parquet_db.close()


def projection_edit_values(
    project_db: Path, wide_sql: str, count: int
) -> dict[int, int]:
    wide = wide_sql.rsplit("ORDER BY", 1)[0]
    db = open_readonly(project_db)
    try:
        rows = db.execute(
            "WITH wide AS ("
            + wide
            + ") SELECT row_id FROM wide WHERE amount_quality='valid' "
            "ORDER BY position,row_id LIMIT ?",
            (count,),
        ).fetchall()
    finally:
        db.close()
    if len(rows) != count:
        raise ValueError(f"actual Project has only {len(rows)} valid edit rows")
    return {int(row["row_id"]): 20_000_000 + index for index, row in enumerate(rows)}


def apply_projection_edits(
    duckdb_path: Path, edits: dict[int, int], scratch: Path
) -> dict[str, Any]:
    db = duckdb.connect(str(duckdb_path))
    try:
        configure_duck(db, scratch)
        started = time.perf_counter()
        batch = pa.table(
            {
                "row_id": pa.array(list(edits), type=pa.int64()),
                "amount": pa.array(list(edits.values()), type=pa.int64()),
            }
        )
        db.register("edit_batch", batch)
        try:
            db.execute(
                "UPDATE analytical_current SET amount=edit_batch.amount,"
                "amount_quality='valid' FROM edit_batch "
                "WHERE analytical_current.row_id=edit_batch.row_id"
            )
            db.execute("CHECKPOINT")
        finally:
            db.unregister("edit_batch")
        seconds = time.perf_counter() - started
    finally:
        db.close()
    return {
        "cells": len(edits),
        "seconds": seconds,
        "artifact_bytes": duckdb_path.stat().st_size,
    }


def assert_projection_edits(
    duckdb_path: Path, parquet_path: Path, edits: dict[int, int]
) -> None:
    expected = sorted(edits.items())
    placeholders = ",".join("?" for _ in expected)
    row_ids = [row_id for row_id, _value in expected]
    parquet_literal = str(parquet_path).replace("'", "''")
    native = duckdb.connect(str(duckdb_path), read_only=True)
    parquet = duckdb.connect(":memory:")
    try:
        for db, source in (
            (native, "analytical_current"),
            (parquet, f"read_parquet('{parquet_literal}')"),
        ):
            rows = db.execute(
                f"SELECT row_id,amount FROM {source} WHERE row_id IN ({placeholders}) "
                "ORDER BY row_id",
                row_ids,
            ).fetchall()
            assert_equivalent(
                [[row_id, value] for row_id, value in expected],
                normalize(rows),
                "projection_edits",
            )
    finally:
        native.close()
        parquet.close()


def source_files(bundle: Path) -> dict[str, dict[str, int]]:
    result = {}
    for name in (
        "manifest.json",
        "project.db",
        "project.db-wal",
        "project.db-shm",
        "project.search.db",
        "project.search.db-wal",
        "project.search.db-shm",
    ):
        path = bundle / name
        if path.exists():
            stat = path.stat()
            result[name] = {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return result


def actual_main(args: argparse.Namespace) -> int:
    if not args.source_sha:
        raise SystemExit("--source-sha is required with --project")
    if len(args.source_sha) != 40 or any(
        character not in "0123456789abcdef" for character in args.source_sha
    ):
        raise SystemExit("--source-sha must be a full lowercase Git SHA")
    bundle = args.project.resolve()
    project_db = bundle / "project.db"
    if not project_db.is_file():
        raise SystemExit(f"not a retained Project bundle: {bundle}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    scratch = output / "duckdb-scratch"
    sqlite_projection_path = output / "projection.sqlite3"
    duckdb_path = output / "projection.duckdb"
    parquet_path = output / "projection.parquet"
    before = source_files(bundle)
    nonempty_wals = {
        name: details["bytes"]
        for name, details in before.items()
        if name.endswith("-wal") and details["bytes"]
    }
    if nonempty_wals:
        raise SystemExit(
            "retained Project must be closed and checkpointed before immutable reads; "
            f"nonempty WAL files: {nonempty_wals}"
        )
    info = discover_actual_project(project_db, args.sheet_name)
    wide_sql = actual_wide_sql(info)
    facts = actual_workload_facts(project_db, wide_sql, info["row_count"])
    report: dict[str, Any] = {
        "status": "running",
        "mode": "retained_actual_project",
        "source_bundle": str(bundle),
        "source_sha": args.source_sha,
        "source_files_before": before,
        "project": info,
        "workload": facts,
        "samples": args.samples,
        "batch_rows": args.batch_rows,
        "row_group_size": args.row_group_size,
        "versions": {
            "python": sys.version,
            "sqlite": sqlite3.sqlite_version,
            "duckdb": duckdb.__version__,
            "pyarrow": pa.__version__,
        },
        "scope": (
            "Read-only retained Project source; matched public/direct analytical "
            "queries and rebuildable narrow projections, not a production adapter. "
            "Wide SQLite is a read/build/storage control; synchronization is unmeasured."
        ),
    }
    report["fresh_projection"] = {
        "sqlite_wide": build_sqlite_projection(
            project_db,
            sqlite_projection_path,
            args.batch_rows,
            wide_sql=wide_sql,
        ),
        "duckdb_native": build_duckdb(
            project_db,
            duckdb_path,
            scratch,
            args.batch_rows,
            wide_sql=wide_sql,
        ),
        "parquet": build_parquet(
            project_db,
            parquet_path,
            scratch,
            args.batch_rows,
            args.row_group_size,
            wide_sql=wide_sql,
        ),
    }
    if report["fresh_projection"]["sqlite_wide"]["rows"] != info["row_count"]:
        raise AssertionError("wide SQLite projection row count mismatch")
    guard_output(output)
    report["queries"] = run_actual_queries(
        project_db,
        sqlite_projection_path,
        duckdb_path,
        parquet_path,
        info,
        facts,
        wide_sql,
        args.samples,
        scratch,
    )
    edit_report = {}
    final_edits: dict[int, int] = {}
    available_edit_rows = int(facts["amount_quality_counts"].get("valid", 0))
    edit_counts = sorted(
        {min(count, available_edit_rows) for count in (1, 200, 1_000)} - {0}
    )
    for count in edit_counts:
        final_edits = projection_edit_values(project_db, wide_sql, count)
        native = apply_projection_edits(duckdb_path, final_edits, scratch)
        parquet_result = build_parquet(
            project_db,
            parquet_path,
            scratch,
            args.batch_rows,
            args.row_group_size,
            wide_sql=wide_sql,
            edits=final_edits,
        )
        assert_projection_edits(duckdb_path, parquet_path, final_edits)
        edit_report[str(count)] = {
            "native_incremental": native,
            "parquet_full_replacement": parquet_result,
        }
        guard_output(output)
    report["simulated_projection_catch_up"] = edit_report
    if not final_edits:
        raise ValueError("actual Project has no valid amount row for edit simulation")
    report["native_full_replacement_after_edits"] = build_duckdb(
        project_db,
        duckdb_path,
        scratch,
        args.batch_rows,
        wide_sql=wide_sql,
        edits=final_edits,
    )
    assert_projection_edits(duckdb_path, parquet_path, final_edits)
    guard_output(output)
    after = source_files(bundle)
    if after != before:
        raise AssertionError(
            "retained Project source files changed during read-only probe"
        )
    authority = sum(
        details["bytes"]
        for name, details in before.items()
        if name.startswith("project.db")
    )
    fts = sum(
        details["bytes"]
        for name, details in before.items()
        if name.startswith("project.search.db")
    )
    manifest = before.get("manifest.json", {}).get("bytes", 0)
    retained_bundle = directory_bytes(bundle)
    native_bytes = duckdb_path.stat().st_size
    parquet_bytes = parquet_path.stat().st_size
    sqlite_projection_bytes = sqlite_projection_path.stat().st_size
    parquet_replacement_peak = max(
        report["fresh_projection"]["parquet"]["peak_replacement_bytes"],
        *(
            edit["parquet_full_replacement"]["peak_replacement_bytes"]
            for edit in edit_report.values()
        ),
    )
    report["storage_accounting"] = {
        "authority_bytes": authority,
        "fts_bytes": fts,
        "manifest_bytes": manifest,
        "retained_bundle_bytes": retained_bundle,
        "authority_plus_fts_bytes": authority + fts,
        "native_projection_bytes": native_bytes,
        "parquet_projection_bytes": parquet_bytes,
        "wide_sqlite_projection_bytes": sqlite_projection_bytes,
        "authority_fts_native_bytes": authority + fts + native_bytes,
        "authority_fts_parquet_bytes": authority + fts + parquet_bytes,
        "authority_fts_wide_sqlite_bytes": authority + fts + sqlite_projection_bytes,
        "retained_bundle_native_bytes": retained_bundle + native_bytes,
        "retained_bundle_parquet_bytes": retained_bundle + parquet_bytes,
        "retained_bundle_wide_sqlite_bytes": retained_bundle + sqlite_projection_bytes,
        "wide_sqlite_initial_build_peak_bytes": retained_bundle
        + report["fresh_projection"]["sqlite_wide"]["peak_replacement_bytes"],
        "native_replacement_peak_bytes": retained_bundle
        + report["native_full_replacement_after_edits"]["peak_replacement_bytes"],
        "parquet_replacement_peak_bytes": retained_bundle + parquet_replacement_peak,
        "owned_output_bytes": directory_bytes(output),
    }
    report["source_files_after"] = after
    report["status"] = "passed"
    report_path = output / "results.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": "passed", "results": str(report_path)}, sort_keys=True))
    return 0


def directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def guard_output(path: Path) -> None:
    used = directory_bytes(path)
    if used > MAX_OWNED_BYTES:
        raise RuntimeError(
            f"owned output exceeded {MAX_OWNED_BYTES} bytes: {used} bytes"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--batch-rows", type=int, default=10_000)
    parser.add_argument("--row-group-size", type=int, default=122_880)
    parser.add_argument(
        "--project",
        type=Path,
        help="closed retained Project bundle to read without mutation",
    )
    parser.add_argument("--sheet-name")
    parser.add_argument(
        "--source-sha",
        help="exact Frisket source revision imported through PYTHONPATH",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    if args.batch_rows < 1:
        raise SystemExit("--batch-rows must be positive")
    if args.row_group_size < 1:
        raise SystemExit("--row-group-size must be positive")
    if args.project is not None:
        if args.rows != 10_000:
            raise SystemExit("--rows is synthetic-mode only")
        return actual_main(args)
    if args.sheet_name is not None or args.source_sha is not None:
        raise SystemExit("--sheet-name and --source-sha require --project")
    if not 1_250 <= args.rows <= 500_000:
        raise SystemExit("--rows must be between 1,250 and 500,000")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sqlite_path = output / "current-slice.sqlite3"
    sqlite_without_path = output / "current-slice-without-rowid.sqlite3"
    duckdb_path = output / "projection.duckdb"
    parquet_path = output / "projection.parquet"
    scratch = output / "duckdb-scratch"
    report: dict[str, Any] = {
        "status": "running",
        "rows": args.rows,
        "samples": args.samples,
        "batch_rows": args.batch_rows,
        "row_group_size": args.row_group_size,
        "versions": {
            "python": sys.version,
            "sqlite": sqlite3.sqlite_version,
            "duckdb": duckdb.__version__,
            "pyarrow": pa.__version__,
        },
        "fixture_path": str(sqlite_path),
        "scope": (
            "Direct equivalent analytical kernels over a current-value slice; "
            "not a Frisket application or production-adapter measurement."
        ),
    }
    report["fixture"] = {
        "ordinary_rowid": create_sqlite_fixture(
            sqlite_path, args.rows, args.batch_rows, ordinary_rowid=True
        ),
        "without_rowid": create_sqlite_fixture(
            sqlite_without_path,
            args.rows,
            args.batch_rows,
            ordinary_rowid=False,
        ),
    }
    guard_output(output)
    report["fresh_projection"] = {
        "duckdb_native": build_duckdb(
            sqlite_path, duckdb_path, scratch, args.batch_rows
        ),
        "parquet": build_parquet(
            sqlite_path,
            parquet_path,
            scratch,
            args.batch_rows,
            args.row_group_size,
        ),
    }
    guard_output(output)
    report["queries"] = run_queries(
        sqlite_path,
        sqlite_without_path,
        duckdb_path,
        parquet_path,
        args.rows,
        args.samples,
        scratch,
    )
    edits = {}
    for count in (1, 200, 1_000):
        edit = apply_edits(
            sqlite_path,
            sqlite_without_path,
            duckdb_path,
            edit_row_ids(args.rows, count),
            scratch,
        )
        edit["parquet_full_rebuild"] = build_parquet(
            sqlite_path,
            parquet_path,
            scratch,
            args.batch_rows,
            args.row_group_size,
        )
        guard_output(output)
        edits[str(count)] = edit
    report["edits"] = edits
    report["post_edit_parity"] = run_queries(
        sqlite_path,
        sqlite_without_path,
        duckdb_path,
        parquet_path,
        args.rows,
        1,
        scratch,
    )
    report["full_duckdb_replacement_after_edits"] = build_duckdb(
        sqlite_path, duckdb_path, scratch, args.batch_rows
    )
    guard_output(output)
    report["replacement_parity"] = run_queries(
        sqlite_path,
        sqlite_without_path,
        duckdb_path,
        parquet_path,
        args.rows,
        1,
        scratch,
    )
    report["artifacts"] = {
        "sqlite_slice_bytes": sqlite_path.stat().st_size,
        "sqlite_without_rowid_slice_bytes": sqlite_without_path.stat().st_size,
        "duckdb_projection_bytes": duckdb_path.stat().st_size,
        "parquet_projection_bytes": parquet_path.stat().st_size,
        "owned_output_bytes": directory_bytes(output),
    }
    report["status"] = "passed"
    report_path = output / "results.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": "passed", "results": str(report_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

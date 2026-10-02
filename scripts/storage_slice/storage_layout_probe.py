#!/usr/bin/env python3
"""Compare cell-table b-tree layouts on a read-only, bounded project fixture.

This is a storage experiment, not a Frisket migration.  It copies only the
two populated cell relations into isolated SQLite files, proves every copied
value/reference field by ordered digest, and reports table/index page costs
and representative read plans.  It never opens the source except read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Iterable


CELL_COLUMNS = ("row_id", "column_id", "value", "producer_id")
CURRENT_COLUMNS = (
    "column_id",
    "row_id",
    "value",
    "origin_kind",
    "origin_op_id",
    "origin_run_id",
    "base_producer_id",
    "validity",
)


def _readonly(path: Path) -> sqlite3.Connection:
    # `immutable=1` prevents lock/journal writes to the shared fixture. The
    # probe requires a quiescent, fully checkpointed input and verifies that
    # condition by retaining the source only as an immutable read.
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _digest(rows: Iterable[tuple[Any, ...]]) -> tuple[int, str]:
    hasher = hashlib.sha256()
    count = 0
    for row in rows:
        for value in row:
            if value is None:
                encoded = b"N"
            elif isinstance(value, int):
                encoded = f"I{value}".encode()
            else:
                encoded = b"T" + str(value).encode("utf-8")
            hasher.update(len(encoded).to_bytes(8, "big"))
            hasher.update(encoded)
        count += 1
    return count, hasher.hexdigest()


def _source_digest(source: sqlite3.Connection, table: str, columns: tuple[str, ...]):
    order = ",".join(columns[:2])
    selected = ",".join(columns)
    return _digest(source.execute(f"SELECT {selected} FROM {table} ORDER BY {order}"))


def _schema(layout: str) -> str:
    cells_tail = "WITHOUT ROWID" if layout == "without_rowid" else ""
    current_tail = "WITHOUT ROWID" if layout == "without_rowid" else ""
    cells_key = "PRIMARY KEY (row_id,column_id)" if layout == "without_rowid" else ""
    current_key = "PRIMARY KEY (column_id,row_id)" if layout == "without_rowid" else ""
    cells_key_prefix = "," if cells_key else ""
    current_key_prefix = "," if current_key else ""
    return f"""
    CREATE TABLE cells (
      row_id INTEGER NOT NULL,
      column_id INTEGER NOT NULL,
      value TEXT,
      producer_id INTEGER{cells_key_prefix}{cells_key}
    ) {cells_tail};
    CREATE TABLE current_cells (
      column_id INTEGER NOT NULL,
      row_id INTEGER NOT NULL,
      value TEXT,
      origin_kind TEXT NOT NULL,
      origin_op_id INTEGER,
      origin_run_id INTEGER,
      base_producer_id INTEGER,
      validity TEXT NOT NULL{current_key_prefix}{current_key}
    ) {current_tail};
    """


def _create_indexes(db: sqlite3.Connection, layout: str) -> None:
    # Rowid tables need explicit unique b-trees to retain the two identity
    # invariants.  Naming the current-column index also keeps the product's
    # forced narrow index scan comparable between layouts.
    if layout == "rowid":
        db.execute("CREATE UNIQUE INDEX uq_cells_row_column ON cells(row_id,column_id)")
        db.execute(
            "CREATE UNIQUE INDEX idx_current_cells_column_row "
            "ON current_cells(column_id,row_id)"
        )
    else:
        db.execute(
            "CREATE INDEX idx_current_cells_column_row "
            "ON current_cells(column_id,row_id)"
        )
    db.execute("CREATE INDEX idx_cells_column ON cells(column_id,row_id)")
    # A WITHOUT ROWID secondary index carries its primary-key suffix, so the
    # product's `(row_id)` index also emits column order for one-row reads.
    # Keep that ordering property in the rowid comparison instead of accepting
    # a temp sort as an accidental representation regression.
    row_index = "row_id" if layout == "without_rowid" else "row_id,column_id"
    db.execute(f"CREATE INDEX idx_current_cells_row ON current_cells({row_index})")


def _copy_table(
    source: sqlite3.Connection, dest: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> None:
    selected = ",".join(columns)
    placeholders = ",".join("?" for _ in columns)
    rows = source.execute(f"SELECT {selected} FROM {table}")
    dest.executemany(f"INSERT INTO {table}({selected}) VALUES ({placeholders})", rows)


def _dbstat(db: sqlite3.Connection) -> dict[str, Any]:
    entries = db.execute(
        "SELECT name,pagetype,count(*) pages,sum(pgsize) bytes,sum(payload) payload_bytes,"
        "sum(unused) unused_bytes,max(mx_payload) max_payload "
        "FROM dbstat GROUP BY name,pagetype ORDER BY name,pagetype"
    ).fetchall()
    by_name: dict[str, dict[str, Any]] = {}
    for name, pagetype, pages, size, payload, unused, maximum in entries:
        item = by_name.setdefault(
            str(name),
            {"bytes": 0, "payload_bytes": 0, "unused_bytes": 0, "pages": 0, "page_types": {}},
        )
        item["bytes"] += int(size)
        item["payload_bytes"] += int(payload)
        item["unused_bytes"] += int(unused)
        item["pages"] += int(pages)
        item["page_types"][str(pagetype)] = {
            "pages": int(pages),
            "bytes": int(size),
            "payload_bytes": int(payload),
            "unused_bytes": int(unused),
            "max_payload": int(maximum or 0),
        }
    return by_name


def _plans_and_timings(db: sqlite3.Connection, samples: dict[str, tuple[int, int]]) -> dict[str, Any]:
    queries = {
        "cells_exact": ("SELECT value,producer_id FROM cells WHERE row_id=? AND column_id=?", samples["base"]),
        "current_exact": (
            "SELECT value,origin_kind,origin_op_id,origin_run_id,base_producer_id,validity "
            "FROM current_cells WHERE column_id=? AND row_id=?",
            samples["current"],
        ),
        "current_column_page": (
            "SELECT row_id FROM current_cells INDEXED BY idx_current_cells_column_row "
            "WHERE column_id=? AND row_id>? ORDER BY row_id LIMIT 500",
            (samples["current"][0], 0),
        ),
        "current_row": (
            "SELECT column_id,value,validity FROM current_cells WHERE row_id=? ORDER BY column_id",
            (samples["base"][0],),
        ),
    }
    result: dict[str, Any] = {}
    for label, (query, params) in queries.items():
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query, params)]
        elapsed: list[float] = []
        result_count = 0
        for _ in range(40):
            started = time.perf_counter_ns()
            rows = db.execute(query, params).fetchall()
            elapsed.append((time.perf_counter_ns() - started) / 1_000_000)
            result_count = len(rows)
        result[label] = {
            "plan": plan,
            "result_count": result_count,
            "median_ms": statistics.median(elapsed),
            "max_ms": max(elapsed),
        }
    return result


def _variant(
    source: sqlite3.Connection,
    path: Path,
    *,
    layout: str,
    page_size: int,
    expected: dict[str, tuple[int, str]],
    samples: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    try:
        db.execute(f"PRAGMA page_size={page_size}")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=OFF")
        db.execute("PRAGMA temp_store=MEMORY")
        db.executescript(_schema(layout))
        _copy_table(source, db, "cells", CELL_COLUMNS)
        _copy_table(source, db, "current_cells", CURRENT_COLUMNS)
        _create_indexes(db, layout)
        db.commit()
        db.execute("VACUUM")
        db.commit()
        actual = {
            "cells": _source_digest(db, "cells", CELL_COLUMNS),
            "current_cells": _source_digest(db, "current_cells", CURRENT_COLUMNS),
        }
        if actual != expected:
            raise RuntimeError(f"copy mismatch for {path.name}: {actual!r} != {expected!r}")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check failed for {path.name}: {integrity}")
        return {
            "layout": layout,
            "page_size": page_size,
            "file_bytes": path.stat().st_size,
            "page_count": int(db.execute("PRAGMA page_count").fetchone()[0]),
            "freelist_count": int(db.execute("PRAGMA freelist_count").fetchone()[0]),
            "object_bytes": _dbstat(db),
            "plans_and_timings": _plans_and_timings(db, samples),
            "copy_digest": {key: {"count": value[0], "sha256": value[1]} for key, value in actual.items()},
        }
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--page-sizes", type=int, nargs="+", default=[4096, 8192])
    parser.add_argument("--replace-output", action="store_true")
    parser.add_argument("--cleanup-output", action="store_true")
    args = parser.parse_args()
    source_path = args.source.resolve()
    output = args.out_dir.resolve()
    expected_names = {
        *(f"{layout}-{page_size}.db" for page_size in args.page_sizes for layout in ("without_rowid", "rowid")),
        "report.json",
    }
    if args.cleanup_output:
        if not output.is_dir():
            raise SystemExit(f"no probe output directory to clean: {output}")
        entries = {entry.name: entry for entry in output.iterdir()}
        if set(entries) - expected_names:
            raise SystemExit(f"refusing to clean unknown output: {output}")
        for name in sorted(entries):
            entries[name].unlink()
        output.rmdir()
        return
    if not source_path.is_file():
        raise SystemExit(f"source does not exist: {source_path}")
    if output.exists():
        # The only deletion this probe permits is its prior, explicitly named
        # scratch output. Refuse unknown content rather than sweeping a path.
        entries = {entry.name: entry for entry in output.iterdir()}
        if not args.replace_output or set(entries) - expected_names:
            raise SystemExit(f"refusing existing output directory: {output}")
        for name in sorted(entries):
            entries[name].unlink()
        output.rmdir()
    output.mkdir(parents=True)
    source = _readonly(source_path)
    try:
        expected = {
            "cells": _source_digest(source, "cells", CELL_COLUMNS),
            "current_cells": _source_digest(source, "current_cells", CURRENT_COLUMNS),
        }
        base = source.execute(
            "SELECT row_id,column_id FROM cells ORDER BY length(COALESCE(value,'')) DESC,row_id LIMIT 1"
        ).fetchone()
        current = source.execute(
            "SELECT column_id,row_id FROM current_cells ORDER BY length(COALESCE(value,'')) DESC,row_id LIMIT 1"
        ).fetchone()
        if base is None or current is None:
            raise SystemExit("fixture has no populated cells")
        samples = {"base": (int(base[0]), int(base[1])), "current": (int(current[0]), int(current[1]))}
        variants = []
        for page_size in args.page_sizes:
            if page_size < 512 or page_size & (page_size - 1):
                raise SystemExit(f"invalid SQLite page size: {page_size}")
            for layout in ("without_rowid", "rowid"):
                variants.append(
                    _variant(
                        source,
                        output / f"{layout}-{page_size}.db",
                        layout=layout,
                        page_size=page_size,
                        expected=expected,
                        samples=samples,
                    )
                )
        report = {
            "source": str(source_path),
            "source_bytes": source_path.stat().st_size,
            "source_digest": {key: {"count": value[0], "sha256": value[1]} for key, value in expected.items()},
            "sample_keys": samples,
            "variants": variants,
        }
        (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    finally:
        source.close()


if __name__ == "__main__":
    main()

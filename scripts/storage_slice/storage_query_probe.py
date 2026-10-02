#!/usr/bin/env python3
"""Bounded actual-Project query-plan probe for grid and analytics reads.

This is an opt-in research helper.  It creates a disposable Frisket bundle,
uses public query entry points, records SQLite's expanded SQL trace, and runs
EXPLAIN QUERY PLAN for each read statement.  It never opens an existing bundle.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import resource
import statistics
import tempfile
import time
from typing import Any, Callable

from frisket.engine.store import Project
from frisket.querysets import resolve_sheet_filter_rows
from frisket.server.services.project_qa_analytics import evaluate_analytics


MIB = 1024**2
MAX_ROWS = 10_000
MAX_SCRATCH_BYTES = 500 * MIB
MAX_RSS_BYTES = 1024 * MIB
PAGE_ROWS = 500

COLUMNS = (
    ("record_id", "integer"),
    ("category", "category"),
    ("amount", "integer"),
    ("published_at", "date"),
    ("status", "category"),
)
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


def _category(row_number: int) -> str:
    bucket = row_number % 100
    return next(
        category
        for category, threshold in zip(CATEGORIES, CATEGORY_THRESHOLDS, strict=True)
        if bucket < threshold
    )


def _amount(row_number: int) -> int | str | None:
    state = row_number % 10
    if state == 0:
        return None
    if state == 1:
        return "not-stated"
    return ((row_number * 7919) % 9_000_000) + 10_000


def _records(start: int, count: int) -> list[dict[str, Any]]:
    return [
        {
            "record_id": row_number,
            "category": _category(row_number),
            "amount": _amount(row_number),
            "published_at": (
                f"2026-{row_number % 12 + 1:02d}-{row_number % 28 + 1:02d}"
            ),
            "status": ("open", "review", "closed")[row_number % 3],
        }
        for row_number in range(start, start + count)
    ]


def _directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _rss_bytes() -> int:
    # Linux reports ru_maxrss in KiB.
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def _guard(root: Path) -> None:
    scratch = _directory_bytes(root)
    rss = _rss_bytes()
    if scratch > MAX_SCRATCH_BYTES:
        raise RuntimeError(f"scratch limit exceeded: {scratch} bytes")
    if rss > MAX_RSS_BYTES:
        raise RuntimeError(f"RSS limit exceeded: {rss} bytes")


def _create_fixture(root: Path, rows: int) -> tuple[Project, int, dict[str, int]]:
    project = Project.create(root / "query-probe.frisket", name="query plan probe")
    sheet_id = project.add_sheet("Records")
    columns = {
        name: project.add_column(sheet_id, name, type_, commit=False)
        for name, type_ in COLUMNS
    }
    project.db.commit()
    for start in range(1, rows + 1, PAGE_ROWS):
        count = min(PAGE_ROWS, rows - start + 1)
        project.add_rows(sheet_id, _records(start, count), columns)
        _guard(root)
    project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return project, sheet_id, columns


class _TracingProject:
    def __init__(self, project: Project, statements: list[str]):
        self._project = project
        self._statements = statements

    @contextmanager
    def read_snapshot(self):
        snapshot = self._project.read_snapshot()
        snapshot.db.set_trace_callback(self._statements.append)
        try:
            with snapshot:
                yield snapshot
        finally:
            try:
                snapshot.db.set_trace_callback(None)
            except RuntimeError:
                pass


def _read_statements(statements: list[str]) -> list[str]:
    return [
        statement.strip()
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        and "sqlite_master" not in statement
    ]


def _timed(call: Callable[[], Any], *, samples: int) -> tuple[Any, dict[str, Any]]:
    durations: list[float] = []
    result: Any = None
    for _ in range(samples):
        started = time.perf_counter()
        result = call()
        durations.append(time.perf_counter() - started)
    return result, {
        "samples": samples,
        "first_ms": durations[0] * 1000,
        "median_ms": statistics.median(durations) * 1000,
        "max_ms": max(durations) * 1000,
    }


def _explain(project: Project, statement: str) -> list[dict[str, Any]]:
    rows = project.db.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
    return [
        {
            "id": int(row[0]),
            "parent": int(row[1]),
            "detail": str(row[3]),
        }
        for row in rows
    ]


def _time_statement(
    project: Project, statement: str
) -> tuple[dict[str, Any], list[tuple[Any, ...]]]:
    started = time.perf_counter()
    rows = project.db.execute(statement).fetchall()
    return (
        {
            "ms": (time.perf_counter() - started) * 1000,
            "returned": len(rows),
        },
        [tuple(row) for row in rows],
    )


def _statement_record(project: Project, statement: str) -> dict[str, Any]:
    standalone, baseline_rows = _time_statement(project, statement)
    record: dict[str, Any] = {
        "sql": statement,
        "plan": _explain(project, statement),
        "standalone": standalone,
    }
    marker = "prepared AS ("
    if marker in statement:
        candidate = statement.replace(marker, "prepared AS MATERIALIZED (", 1)
        candidate_timing, candidate_rows = _time_statement(project, candidate)
        if candidate_rows != baseline_rows:
            raise AssertionError("AS MATERIALIZED changed statement results")
        record["prepared_materialized"] = {
            "sql": candidate,
            "plan": _explain(project, candidate),
            "standalone": candidate_timing,
            "exact_rows_equal": True,
        }
    return record


def _trace_grid(
    project: Project,
    sheet_id: int,
    *,
    filter_: str | None = None,
    sort: str | None = None,
    samples: int,
) -> dict[str, Any]:
    trace: list[str] = []
    project.db.set_trace_callback(trace.append)
    try:
        result, timing = _timed(
            lambda: resolve_sheet_filter_rows(
                project,
                sheet_id,
                filter_=filter_,
                sort=sort,
                limit=50,
            ),
            samples=samples,
        )
    finally:
        project.db.set_trace_callback(None)
    statements = _read_statements(trace)
    # Every sample emits the same count and page pair.  Keep the first pair.
    unique: list[str] = []
    for statement in statements:
        if statement not in unique:
            unique.append(statement)
    return {
        **timing,
        "total": result.total,
        "row_ids": result.row_ids,
        "statements": [_statement_record(project, item) for item in unique],
    }


def _trace_analytics(
    project: Project,
    sheet_id: int,
    columns: dict[str, int],
    *,
    filtered: bool,
    samples: int,
) -> dict[str, Any]:
    metrics = [
        {"id": "rows", "kind": "count"},
        {"id": "values", "kind": "value_count", "column_id": columns["amount"]},
        {"id": "missing", "kind": "missing_count", "column_id": columns["amount"]},
        {"id": "sum", "kind": "sum", "column_id": columns["amount"]},
        {"id": "mean", "kind": "mean", "column_id": columns["amount"]},
        {"id": "median", "kind": "median", "column_id": columns["amount"]},
    ]
    request: dict[str, Any] = {"sheet_id": sheet_id, "metrics": metrics}
    if filtered:
        request["filter"] = {"category": {"eq": "environment"}}
    else:
        request.update(
            {
                "groups": [{"column_id": columns["category"]}],
                "sort": [{"kind": "group", "group_index": 0, "direction": "asc"}],
                "limit": 20,
            }
        )
    trace: list[str] = []
    traced = _TracingProject(project, trace)
    result, timing = _timed(
        lambda: evaluate_analytics(
            traced, request, {"kind": "sheet", "sheet_id": sheet_id}
        ),
        samples=samples,
    )
    statements = _read_statements(trace)
    unique: list[str] = []
    for statement in statements:
        if statement not in unique:
            unique.append(statement)
    return {
        **timing,
        "row_count": result["row_count"],
        "groups": len(result["groups"]),
        "statements": [_statement_record(project, item) for item in unique],
    }


def _validate_result(result: dict[str, Any], rows: int) -> None:
    narrow = result["grid"]["filter_0_1_percent"]
    expected_total = max(1, math.ceil(rows * 0.001))
    if narrow["total"] != expected_total:
        raise AssertionError((narrow["total"], expected_total))
    if result["analytics"]["broad_grouped"]["row_count"] != rows:
        raise AssertionError("broad analytics row count changed")
    expected_environment = sum(
        _category(row) == "environment" for row in range(1, rows + 1)
    )
    if result["analytics"]["filtered_10_percent"]["row_count"] != expected_environment:
        raise AssertionError("filtered analytics row count changed")


def run(rows: int, work_root: Path, *, samples: int) -> dict[str, Any]:
    if rows < 1 or rows > MAX_ROWS:
        raise ValueError(f"rows must be between 1 and {MAX_ROWS}")
    if samples < 1 or samples > 5:
        raise ValueError("samples must be between 1 and 5")
    work_root.mkdir(parents=True, exist_ok=True)
    if any(work_root.iterdir()):
        raise FileExistsError(f"work root must be empty: {work_root}")
    sqlite_tmp = work_root / "sqlite-tmp"
    sqlite_tmp.mkdir()
    old_tmp = os.environ.get("SQLITE_TMPDIR")
    os.environ["SQLITE_TMPDIR"] = str(sqlite_tmp)
    project: Project | None = None
    try:
        project, sheet_id, columns = _create_fixture(work_root, rows)
        narrow_start = rows - max(1, math.ceil(rows * 0.001)) + 1
        result = {
            "rows": rows,
            "sqlite_version": project.db.execute("SELECT sqlite_version()").fetchone()[
                0
            ],
            "grid": {
                "filter_0_1_percent": _trace_grid(
                    project,
                    sheet_id,
                    filter_=json.dumps({"record_id": {"gte": str(narrow_start)}}),
                    samples=samples,
                ),
                "filter_10_percent": _trace_grid(
                    project,
                    sheet_id,
                    filter_=json.dumps({"category": {"eq": "environment"}}),
                    samples=samples,
                ),
                "amount_asc": _trace_grid(
                    project,
                    sheet_id,
                    sort=json.dumps([{"column": "amount", "dir": "asc"}]),
                    samples=samples,
                ),
            },
            "analytics": {
                "broad_grouped": _trace_analytics(
                    project, sheet_id, columns, filtered=False, samples=samples
                ),
                "filtered_10_percent": _trace_analytics(
                    project, sheet_id, columns, filtered=True, samples=samples
                ),
            },
        }
        _guard(work_root)
        result["scratch_bytes"] = _directory_bytes(work_root)
        result["peak_rss_bytes"] = _rss_bytes()
        _validate_result(result, rows)
        return result
    finally:
        if project is not None:
            project.close()
        if old_tmp is None:
            os.environ.pop("SQLITE_TMPDIR", None)
        else:
            os.environ["SQLITE_TMPDIR"] = old_tmp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.work_root is None:
        with tempfile.TemporaryDirectory(prefix="frisket-query-probe-") as raw:
            result = run(args.rows, Path(raw), samples=args.samples)
    else:
        result = run(args.rows, args.work_root.resolve(), samples=args.samples)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

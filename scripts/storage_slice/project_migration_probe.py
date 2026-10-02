"""Bounded in-place migration probe for one disposable actual-Project bundle."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any, Callable

from frisket.engine.store import Project
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY

from .benchmark import PhaseSampler, directory_bytes
from .project_benchmark import (
    GIB,
    HARD_RSS_KIB,
    HOST_FREE_RESERVE,
    MAX_SCRATCH,
    MIN_AVAILABLE_KIB,
    ResourceStop,
    SOFT_RSS_KIB,
    _dbstat_accounting,
    _file_sizes,
    _mem_available_kib,
    _phase_record,
    _preflight,
)


_TABLES = {
    "cells": ("row_id", "column_id", "value", "producer_id"),
    "current_cells": (
        "column_id",
        "row_id",
        "value",
        "origin_kind",
        "origin_op_id",
        "origin_run_id",
        "base_producer_id",
        "validity",
    ),
}


def _table_digest(db: sqlite3.Connection, table: str, fields: tuple[str, ...]) -> dict:
    digest = hashlib.sha256()
    digest.update((table + "\0" + ",".join(fields) + "\n").encode())
    query = f"SELECT {','.join(fields)} FROM {table} ORDER BY {','.join(fields[:2])}"
    count = 0
    for row in db.execute(query):
        digest.update(
            json.dumps(
                list(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
            + b"\n"
        )
        count += 1
    return {"rows": count, "sha256": digest.hexdigest(), "fields": list(fields)}


def _raw_digests(bundle: Path) -> dict[str, dict]:
    database = bundle / "project.db"
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {
            table: _table_digest(connection, table, fields)
            for table, fields in _TABLES.items()
        }
    finally:
        connection.close()


def _raw_schema_digest(bundle: Path) -> str | None:
    database = bundle / "project.db"
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone()
        return None if row is None else str(row[0])
    finally:
        connection.close()


def _project_digests(project: Project) -> dict[str, dict]:
    return {
        table: _table_digest(project.db, table, fields)
        for table, fields in _TABLES.items()
    }


def _database_accounting(bundle: Path) -> dict[str, dict]:
    report: dict[str, dict] = {}
    for name in ("project.db", "project.search.db"):
        database = bundle / name
        if database.exists():
            accounting = _dbstat_accounting(database)
            connection = sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro", uri=True
            )
            try:
                accounting["freelist_pages"] = int(
                    connection.execute("PRAGMA freelist_count").fetchone()[0]
                )
            finally:
                connection.close()
            report[name] = accounting
    return report


def _public_smoke(project: Project) -> dict[str, Any]:
    """Use a small public read instead of repeating the qualification workload."""

    sheets = project.sheets()
    result: dict[str, Any] = {
        "sheets": len(sheets),
        "history_total": project.history_total(),
    }
    if not sheets:
        return result
    sheet_id = int(sheets[0]["id"])
    columns = project.columns(sheet_id)
    result["first_sheet_id"] = sheet_id
    result["first_sheet_rows"] = project.row_count(sheet_id)
    result["first_sheet_columns"] = len(columns)
    if not columns:
        return result
    row = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY id LIMIT 1",
        (sheet_id,),
    ).fetchone()
    if row is None:
        return result
    row_id = int(row[0])
    column_id = int(columns[0]["id"])
    values, refs = project.get_values_with_refs(
        sheet_id,
        column_id,
        [row_id],
        preserve_invalid=True,
        include_validity=True,
    )
    assert row_id in refs
    result["first_value"] = {
        "row_id": row_id,
        "column_id": column_id,
        "present": row_id in values,
        "origin_kind": refs[row_id]["kind"],
        "validity": refs[row_id]["validity"],
    }
    return result


def _bounded_call(
    project: Project,
    sampler: PhaseSampler,
    call: Callable[[], Any],
) -> Any:
    """Let the existing sampler interrupt integrity and checkpoint statements."""

    sampler.on_limit = project.db.interrupt
    project.db.set_progress_handler(lambda: int(sampler.hard_limit.is_set()), 10_000)
    try:
        value = call()
    except sqlite3.OperationalError as exc:
        if sampler.hard_limit.is_set():
            raise ResourceStop(
                sampler.limit_reason or "sampled resource limit reached"
            ) from exc
        raise
    finally:
        project.db.set_progress_handler(None, 0)
        sampler.on_limit = None
    if sampler.hard_limit.is_set():
        raise ResourceStop(sampler.limit_reason or "sampled resource limit reached")
    return value


def run_migration_probe(
    bundle: Path,
    work_root: Path,
    *,
    candidate_sha: str,
    expected_old_schema_digest: str,
    max_scratch_gib: int = MAX_SCRATCH // GIB,
) -> dict[str, Any]:
    """Open one old bundle with candidate source and prove the physical swap."""

    bundle = bundle.resolve(strict=True)
    work_root = work_root.resolve(strict=True)
    if not bundle.is_dir() or not (bundle / "project.db").is_file():
        raise ValueError(f"not a Project bundle: {bundle}")
    if not bundle.is_relative_to(work_root):
        raise ValueError("bundle must be inside the owned work directory")
    if max_scratch_gib < 3:
        raise ValueError("max_scratch_gib must be at least 3")

    free_at_start = shutil.disk_usage(work_root).free
    initial_work_bytes = directory_bytes(work_root)
    growth_headroom = free_at_start - HOST_FREE_RESERVE
    if growth_headroom < 3 * GIB:
        raise ResourceStop("less than 3 GiB available inside host reserve")
    hard_scratch = min(max_scratch_gib * GIB, initial_work_bytes + growth_headroom)
    soft_scratch = max(2 * GIB, hard_scratch - 2 * GIB)
    report: dict[str, Any] = {
        "schema": "frisket.project-migration-probe.v1",
        "status": "running",
        "bundle": str(bundle),
        "candidate_sha": candidate_sha,
        "expected_old_schema_digest": expected_old_schema_digest,
        "candidate_schema_digest": SCHEMA_DIGEST,
        "project_module": inspect.getfile(Project),
        "open_migration_cancellation": (
            "observation only: Project constructor owns this connection; "
            "progress-handler interruption begins after open"
        ),
        "limits": {
            "scope": "whole probe process and its owned work directory",
            "initial_work_bytes": initial_work_bytes,
            "growth_headroom_bytes": growth_headroom,
            "max_scratch_gib": max_scratch_gib,
            "hard_scratch_bytes": hard_scratch,
            "soft_scratch_bytes": soft_scratch,
            "host_free_reserve_bytes": HOST_FREE_RESERVE,
        },
        "preflight": _preflight(work_root),
        "phases": {},
    }
    sampler = PhaseSampler(
        work_root,
        hard_limit_bytes=hard_scratch,
        limit_probe=lambda observed: _sample_limit(work_root, soft_scratch, observed),
    )
    project: Project | None = None
    sampler.start()
    try:
        sampler.set_phase("before")
        started = time.perf_counter()
        report["before"] = {
            "schema_digest": _raw_schema_digest(bundle),
            "digests": _raw_digests(bundle),
            "files": _file_sizes(bundle),
            "physical_accounting": _database_accounting(bundle),
        }
        assert report["before"]["schema_digest"] == expected_old_schema_digest
        report["phases"]["before"] = _phase_record(bundle, started)
        _raise_if_limited(sampler)

        sampler.set_phase("open_migration")
        started = time.perf_counter()
        project = Project(bundle)
        _raise_if_limited(sampler)
        report["phases"]["open_migration"] = _phase_record(bundle, started)

        sampler.set_phase("after")
        started = time.perf_counter()
        report["after"] = {
            "schema_digest": project.get_meta(SCHEMA_DIGEST_META_KEY),
            "digests": _project_digests(project),
            "files": _file_sizes(bundle),
            "physical_accounting": _database_accounting(bundle),
            "public_smoke": _public_smoke(project),
        }
        assert report["after"]["schema_digest"] == SCHEMA_DIGEST
        assert report["after"]["digests"] == report["before"]["digests"]
        report["checks"] = {
            "foreign_key_check": _bounded_call(
                project,
                sampler,
                lambda: project.db.execute("PRAGMA foreign_key_check").fetchone(),
            )
            is None,
            "integrity_check": _bounded_call(
                project,
                sampler,
                lambda: project.db.execute("PRAGMA integrity_check").fetchone()[0],
            )
            == "ok",
        }
        assert all(report["checks"].values())
        report["phases"]["after"] = _phase_record(bundle, started)

        sampler.set_phase("checkpoint")
        started = time.perf_counter()
        report["checkpoint"] = {
            "result": list(
                _bounded_call(
                    project,
                    sampler,
                    lambda: project.db.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone(),
                )
            ),
        }
        report["checkpoint"]["files"] = _file_sizes(bundle)
        report["checkpoint"]["physical_accounting"] = _database_accounting(bundle)
        report["phases"]["checkpoint"] = _phase_record(bundle, started)
        report["status"] = "completed"
    except ResourceStop as exc:
        report["status"] = "resource_stopped"
        report["stop_reason"] = str(exc)
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["stop_reason"] = "received KeyboardInterrupt"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if project is not None:
            project.close()
        sampler.stop()
        if report["status"] == "completed" and sampler.hard_limit.is_set():
            report["status"] = "resource_stopped"
            report["stop_reason"] = (
                sampler.limit_reason or "sampled resource limit reached"
            )
        report["sampled_phase_peaks"] = sampler.peaks
        report["host_free_bytes_min"] = min(
            phase["host_free_bytes_min"] for phase in sampler.peaks.values()
        )
    return report


def _sample_limit(
    work_root: Path, soft_scratch: int, observed: dict[str, Any]
) -> str | None:
    if shutil.disk_usage(work_root).free < HOST_FREE_RESERVE:
        return "host free-space reserve reached"
    if observed["project_bytes"] >= soft_scratch:
        return "soft scratch limit reached"
    if observed["rss_kib"] >= HARD_RSS_KIB:
        return "hard RSS limit reached"
    if observed["rss_kib"] >= SOFT_RSS_KIB:
        return "soft RSS limit reached"
    available = _mem_available_kib()
    if available is not None and available < MIN_AVAILABLE_KIB:
        return "host MemAvailable floor reached"
    return None


def _raise_if_limited(sampler: PhaseSampler) -> None:
    sampler.sample()
    if sampler.hard_limit.is_set():
        raise ResourceStop(sampler.limit_reason or "sampled resource limit reached")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--expected-old-schema-digest", required=True)
    parser.add_argument("--max-scratch-gib", type=int, default=MAX_SCRATCH // GIB)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = run_migration_probe(
        args.bundle,
        args.work_dir,
        candidate_sha=args.candidate_sha,
        expected_old_schema_digest=args.expected_old_schema_digest,
        max_scratch_gib=args.max_scratch_gib,
    )
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": result["status"], "output": str(args.output)}))


if __name__ == "__main__":
    main()

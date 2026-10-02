"""Bounded qualification of Frisket's actual Project schema and store."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import heapq
import json
import os
import platform
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.search_index_work import latest_revision
from frisket.search_index import index_batch
from frisket.server.services.project_qa_analytics import (
    AnalyticsCancelled,
)

from .benchmark import PhaseSampler, current_rss_kib, directory_bytes
from .fixtures import stress_project_records
from .project_workloads import (
    apply_expected_amount_edits,
    assert_edit_values,
    probe_current_pages,
    run_analytics_workload,
    run_grid_workload,
    run_mutation_workload,
    run_reopen_marker_check,
    run_search_workload,
    verify_history,
)


GIB = 1024**3
MIB = 1024**2
PAGE_ROWS = 500
HARD_RSS_KIB = 8 * 1024**2
SOFT_RSS_KIB = 6 * 1024**2
MIN_AVAILABLE_KIB = 4 * 1024**2
HOST_FREE_RESERVE = 15 * GIB
MAX_SCRATCH = 18 * GIB

COLUMNS = [
    {"name": "record_id", "type": "integer"},
    {"name": "title", "type": "text", "format": "plain_text"},
    {"name": "body", "type": "text", "format": "plain_text"},
    {"name": "category", "type": "category"},
    {"name": "amount", "type": "integer"},
    {"name": "published_at", "type": "date"},
    {"name": "status", "type": "category"},
]


class ResourceStop(RuntimeError):
    """A sampled host or experiment resource limit was reached."""


def _mem_available_kib() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _file_sizes(bundle: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for item in sorted(bundle.rglob("*")):
        if item.is_file():
            sizes[str(item.relative_to(bundle))] = item.stat().st_size
    return sizes


def _dbstat_accounting(database: Path) -> dict[str, Any]:
    """Report persistent SQLite objects after the checkpoint has closed the DB."""

    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        schema_kinds = {
            str(row["name"]): str(row["type"])
            for row in connection.execute(
                "SELECT name, type FROM sqlite_schema WHERE name IS NOT NULL"
            )
        }
        objects = [
            {
                "name": str(row["name"]),
                "kind": schema_kinds.get(str(row["name"]), "internal"),
                "pages": int(row["pages"]),
                "bytes": int(row["bytes"]),
            }
            for row in connection.execute(
                "SELECT name, COUNT(*) AS pages, SUM(pgsize) AS bytes "
                "FROM dbstat GROUP BY name ORDER BY bytes DESC, name"
            )
        ]
        return {
            "path": database.name,
            "file_bytes": database.stat().st_size,
            "page_size": page_size,
            "page_count": page_count,
            "page_bytes": page_size * page_count,
            "objects": objects,
        }
    finally:
        connection.close()


def _make_tree_read_only(root: Path) -> None:
    """Make a retained artifact safe to hand to read-only measurement lanes."""

    items = sorted(root.rglob("*"), reverse=True)
    if next((item for item in items if item.is_symlink()), None) is not None:
        raise RuntimeError("retained bundle contains unsupported symlink")
    for item in items:
        item.chmod(0o555 if item.is_dir() else 0o444)
    root.chmod(0o555)


def _remove_tree(root: Path) -> None:
    for item in sorted(root.rglob("*"), reverse=True):
        if not item.is_symlink():
            item.chmod(0o700 if item.is_dir() else 0o600)
    root.chmod(0o700)
    shutil.rmtree(root)


def _retain_completed_bundle(bundle: Path, work_root: Path) -> dict[str, Any]:
    """Move only a completed closed bundle out of the owned scratch directory."""

    retained_root = Path(
        tempfile.mkdtemp(prefix="project-qualification-retained-", dir=work_root)
    )
    retained_bundle = retained_root / bundle.name
    try:
        shutil.move(str(bundle), retained_bundle)
        _make_tree_read_only(retained_bundle)
    except BaseException:
        _remove_tree(retained_root)
        raise
    return {
        "bundle_path": str(retained_bundle),
        "owner": "storage-layout columnar and physical measurement lanes",
        "read_only": True,
    }


def _phase_record(bundle: Path, started: float) -> dict[str, Any]:
    return {
        "seconds": time.perf_counter() - started,
        "rss_kib": current_rss_kib(),
        "bundle_bytes": directory_bytes(bundle),
        "files": _file_sizes(bundle),
        "mem_available_kib": _mem_available_kib(),
        "host_free_bytes": shutil.disk_usage(bundle).free,
    }


def _preflight(work_root: Path) -> dict[str, Any]:
    try:
        filesystem = subprocess.check_output(
            ["findmnt", "-n", "-o", "SOURCE,FSTYPE,TARGET", "--target", work_root],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        filesystem = "unavailable"
    return {
        "qualification_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "filesystem": filesystem,
        "host_free_bytes": shutil.disk_usage(work_root).free,
        "mem_available_kib": _mem_available_kib(),
        "swap": {
            line.split(":", 1)[0]: int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("SwapTotal:", "SwapFree:"))
        },
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "import_page_rows": PAGE_ROWS,
        "index_batch_size": 5000,
        "index_max_bytes": 8 * MIB,
    }


def _guard(sampler: PhaseSampler, *, soft_scratch: int) -> None:
    sampler.sample()
    rss = current_rss_kib()
    available = _mem_available_kib()
    used = directory_bytes(sampler.root)
    if sampler.hard_limit.is_set():
        raise ResourceStop(sampler.limit_reason or "sampled resource limit reached")
    if used >= sampler.hard_limit_bytes:
        raise ResourceStop("hard scratch limit reached")
    if used >= soft_scratch:
        raise ResourceStop("soft scratch limit reached")
    if rss is not None and rss >= HARD_RSS_KIB:
        raise ResourceStop("hard RSS limit reached")
    if rss is not None and rss >= SOFT_RSS_KIB:
        raise ResourceStop("soft RSS limit reached")
    if available is not None and available < MIN_AVAILABLE_KIB:
        raise ResourceStop("host MemAvailable floor reached")


def _drain_index(project: Project, sampler: PhaseSampler, soft_scratch: int) -> dict:
    batches = cells = payload_bytes = 0
    batch_records = []
    started = time.perf_counter()
    while True:
        batch_started = time.perf_counter()
        progress = index_batch(project, batch_size=5000, max_bytes=8 * MIB)
        batches += 1
        cells += progress.processed
        payload_bytes += progress.processed_bytes
        _guard(sampler, soft_scratch=soft_scratch)
        batch_records.append(
            {
                "seconds": time.perf_counter() - batch_started,
                "processed_units": progress.processed,
                "processed_value_bytes": progress.processed_bytes,
                "complete": progress.complete,
                "scratch_bytes": directory_bytes(sampler.root),
                "rss_kib": current_rss_kib(),
                "source_revision": latest_revision(project.db),
                "files": _file_sizes(sampler.root),
            }
        )
        if progress.complete:
            break
        if progress.processed <= 0:
            raise AssertionError("incomplete index batch made no progress")
    return {
        "seconds": time.perf_counter() - started,
        "batches": batches,
        "processed_units": cells,
        "processed_value_bytes": payload_bytes,
        "batch_records": batch_records,
    }


def _expected_record(row_number: int) -> dict:
    return next(stress_project_records(row_number, 1))


def _governed_query(project: Project, sampler: PhaseSampler, soft_scratch: int, call):
    """Interrupt an active SQLite statement when the sampler trips."""

    _guard(sampler, soft_scratch=soft_scratch)
    sampler.on_limit = project.db.interrupt
    try:
        value = call()
    except (AnalyticsCancelled, sqlite3.OperationalError) as exc:
        if sampler.hard_limit.is_set():
            raise ResourceStop(
                sampler.limit_reason or "sampled resource limit reached"
            ) from exc
        raise
    finally:
        sampler.on_limit = None
    _guard(sampler, soft_scratch=soft_scratch)
    return value


def _check_values(
    project: Project,
    sheet_id: int,
    columns: dict[str, int],
    row_map: dict[int, int],
    *,
    body_overrides: dict[int, str] | None = None,
    origin_kind: str | None = None,
) -> None:
    for number, row_id in row_map.items():
        expected = _expected_record(number)
        if body_overrides and number in body_overrides:
            expected["body"] = body_overrides[number]
        for name in (
            "record_id",
            "title",
            "body",
            "category",
            "amount",
            "published_at",
            "status",
        ):
            values, refs = project.get_values_with_refs(
                sheet_id,
                columns[name],
                [row_id],
                preserve_invalid=True,
                include_validity=True,
            )
            if expected[name] is None:
                assert values.get(row_id) is None
                continue
            assert values[row_id] == expected[name], (number, name, values.get(row_id))
            validity = refs[row_id]["validity"]
            assert validity == (
                "invalid" if expected[name] == "not-stated" else "valid"
            )
            if origin_kind is not None:
                assert refs[row_id]["kind"] == origin_kind


def _verify_persisted_corpus(
    project: Project,
    sheet_id: int,
    columns: dict[str, int],
    rows: int,
    sampler: PhaseSampler,
    soft_scratch: int,
) -> dict[str, Any]:
    """Compare every stored current value to a regenerated bounded page."""

    expected_hash = hashlib.sha256()
    actual_hash = hashlib.sha256()
    names = [spec["name"] for spec in COLUMNS]
    compared = 0
    last_position = last_id = 0
    for start in range(1, rows + 1, PAGE_ROWS):
        expected_page = list(
            stress_project_records(start, min(PAGE_ROWS, rows - start + 1))
        )
        row_ids = [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id,position FROM rows WHERE sheet_id=? AND hidden=0 "
                "AND (position>? OR (position=? AND id>?)) "
                "ORDER BY position,id LIMIT ?",
                (sheet_id, last_position, last_position, last_id, len(expected_page)),
            )
        ]
        assert len(row_ids) == len(expected_page)
        last_row = project.db.execute(
            "SELECT position,id FROM rows WHERE id=?", (row_ids[-1],)
        ).fetchone()
        last_position, last_id = int(last_row["position"]), int(last_row["id"])
        values_by_name: dict[str, dict[int, Any]] = {}
        refs_by_name: dict[str, dict[int, dict[str, Any]]] = {}
        for name in names:
            values, refs = project.get_values_with_refs(
                sheet_id,
                columns[name],
                row_ids,
                preserve_invalid=True,
                include_validity=True,
            )
            values_by_name[name] = values
            refs_by_name[name] = refs
        for row_id, expected in zip(row_ids, expected_page, strict=True):
            actual = {name: values_by_name[name].get(row_id) for name in names}
            for name in names:
                assert actual[name] == expected[name], (start, row_id, name)
                if expected[name] is not None:
                    assert refs_by_name[name][row_id]["validity"] == (
                        "invalid" if expected[name] == "not-stated" else "valid"
                    )
            for digest, record in (
                (expected_hash, expected),
                (actual_hash, actual),
            ):
                digest.update(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )
            compared += 1
        _guard(sampler, soft_scratch=soft_scratch)
    expected_cells = rows * len(COLUMNS) - rows // 10
    counts = {
        "rows": int(
            project.db.execute(
                "SELECT COUNT(*) FROM rows WHERE sheet_id=? AND hidden=0", (sheet_id,)
            ).fetchone()[0]
        ),
        "cells": int(
            project.db.execute(
                "SELECT COUNT(*) FROM cells c JOIN rows r ON r.id=c.row_id "
                "WHERE r.sheet_id=?",
                (sheet_id,),
            ).fetchone()[0]
        ),
        "current_cells": int(
            project.db.execute(
                "SELECT COUNT(*) FROM current_cells cc JOIN rows r ON r.id=cc.row_id "
                "WHERE r.sheet_id=?",
                (sheet_id,),
            ).fetchone()[0]
        ),
    }
    assert counts == {
        "rows": rows,
        "cells": expected_cells,
        "current_cells": expected_cells,
    }
    assert actual_hash.digest() == expected_hash.digest()
    return {
        "rows_compared": compared,
        "expected_cells": expected_cells,
        "table_counts": counts,
        "sha256": actual_hash.hexdigest(),
    }


def _verify_import_metadata(
    project: Project,
    publication,
    columns: dict[str, int],
    rows: int,
) -> dict[str, Any]:
    session = project.db.execute(
        "SELECT state,cursor,committed_rows,committed_bytes FROM import_sessions "
        "WHERE id='qualification-import'"
    ).fetchone()
    assert session is not None
    assert (session["state"], session["cursor"], session["committed_rows"]) == (
        "completed",
        rows,
        rows,
    )
    receipt = ReceiptStore(project).find_by_id(publication.receipt_id)
    assert receipt is not None and receipt.status == "completed"
    assert receipt.parsed().op_ids == [publication.op_id]
    op = project.db.execute(
        "SELECT kind,status,barrier FROM ops WHERE id=?", (publication.op_id,)
    ).fetchone()
    assert op is not None and op["status"] == "applied" and not op["barrier"]
    by_column: dict[str, dict[str, Any]] = {}
    for name, column_id in columns.items():
        cells = int(
            project.db.execute(
                "SELECT COUNT(*) FROM cells WHERE column_id=?", (column_id,)
            ).fetchone()[0]
        )
        groups = {
            (str(row["origin_kind"]), str(row["validity"])): int(row["n"])
            for row in project.db.execute(
                "SELECT origin_kind,validity,COUNT(*) AS n FROM current_cells "
                "WHERE column_id=? GROUP BY origin_kind,validity",
                (column_id,),
            )
        }
        expected_cells = rows - rows // 10 if name == "amount" else rows
        assert cells == expected_cells
        assert groups == (
            {
                ("source_cell", "valid"): rows - 2 * (rows // 10),
                ("source_cell", "invalid"): rows // 10,
            }
            if name == "amount"
            else {("source_cell", "valid"): rows}
        )
        by_column[name] = {
            "cells": cells,
            "current_origin_validity": {
                f"{origin}:{validity}": count
                for (origin, validity), count in groups.items()
            },
        }
    return {
        "session": dict(session),
        "receipt_id": publication.receipt_id,
        "op_id": publication.op_id,
        "op_kind": op["kind"],
        "columns": by_column,
    }


def run_qualification(
    rows: int,
    work_root: Path,
    *,
    composition_sha: str | None = None,
    retain_success: bool = False,
) -> dict[str, Any]:
    if not 200 <= rows <= 500_000:
        raise ValueError("rows must be between 200 and 500000")
    free_at_start = shutil.disk_usage(work_root).free
    hard_scratch = min(MAX_SCRATCH, free_at_start - HOST_FREE_RESERVE)
    if hard_scratch < 3 * GIB:
        raise ResourceStop("less than 3 GiB available inside host reserve")
    soft_scratch = max(2 * GIB, hard_scratch - 2 * GIB)
    report: dict[str, Any] = {
        "schema": "frisket.project-storage-qualification.v1",
        "status": "running",
        "rows_requested": rows,
        "limits": {
            "scope": "whole runner process and its generated work directory",
            "max_rows": 500_000,
            "hard_scratch_bytes": hard_scratch,
            "soft_scratch_bytes": soft_scratch,
            "host_free_reserve_bytes": HOST_FREE_RESERVE,
            "soft_rss_kib": SOFT_RSS_KIB,
            "hard_rss_kib": HARD_RSS_KIB,
            "mem_available_floor_kib": MIN_AVAILABLE_KIB,
        },
        "phases": {},
        "preflight": _preflight(work_root),
        "retention": {
            "requested": retain_success,
            "bundle": None,
        },
        "profile_note": (
            "Retains the first 300k runner's 500-row import and 5000-unit/8 MiB "
            "index quanta so the corrected run remains comparable; index_batch "
            "semantics and progress assertions are unchanged."
        ),
    }
    report["preflight"]["production_composition_sha"] = composition_sha
    with tempfile.TemporaryDirectory(
        prefix="project-qualification-", dir=work_root
    ) as raw:
        owned = Path(raw)
        bundle = owned / "qualification.frisket"
        sqlite_temp = owned / "sqlite-tmp"
        sqlite_temp.mkdir()
        previous_sqlite_tmpdir = os.environ.get("SQLITE_TMPDIR")
        os.environ["SQLITE_TMPDIR"] = str(sqlite_temp)

        def sampled_limit(observed: dict[str, Any]) -> str | None:
            if shutil.disk_usage(owned).free < HOST_FREE_RESERVE:
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

        sampler = PhaseSampler(
            owned,
            hard_limit_bytes=hard_scratch,
            limit_probe=sampled_limit,
        )
        sampler.start()
        project: Project | None = None
        try:
            sampler.set_phase("create")
            started = time.perf_counter()
            project = Project.create(bundle, name="storage qualification")
            writer = StreamingSheetWriter.start_session(
                project,
                session_id="qualification-import",
                writer_authority="qualification-runner",
                sheet_name="Investigative records",
                columns=COLUMNS,
                project_id="storage-project-qualification",
                action_kind="import.files",
                idempotency_key="qualification-import-v1",
                params_hash="sha256:qualification-import-v1",
                action_id="act:qualification-import-v1",
                receipt_id="receipt:qualification-import-v1",
                source_ref={"kind": "generated_fixture", "rows": rows},
            )
            report["phases"]["create"] = _phase_record(bundle, started)

            sampler.set_phase("import")
            started = time.perf_counter()
            input_bytes = body_bytes = 0
            text_lengths = Counter()
            amount_states = Counter()
            category_counts = Counter()
            aggregates: dict[str, dict[str, int]] = {}
            top_amounts: list[tuple[int, int, int]] = []
            bottom_amounts: list[tuple[int, int, int]] = []
            date_first: list[tuple[str, int]] = []
            category_first: list[tuple[str, int]] = []
            environment_first: list[int] = []
            import_pages: list[dict[str, Any]] = []
            generation_seconds = 0.0
            row_map: dict[int, int] = {}
            sample_numbers = sorted(
                {1, min(rows, 2), min(rows, 199), (rows + 1) // 2, rows}
            )
            cursor = 0
            for start in range(1, rows + 1, PAGE_ROWS):
                generation_started = time.perf_counter()
                page = list(
                    stress_project_records(start, min(PAGE_ROWS, rows - start + 1))
                )
                encoded = [
                    json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode()
                    + b"\n"
                    for item in page
                ]
                committed = sum(map(len, encoded))
                generated = time.perf_counter() - generation_started
                generation_seconds += generated
                append_started = time.perf_counter()
                ids = writer.append_page(
                    page,
                    expected_cursor=cursor,
                    next_cursor=cursor + len(page),
                    committed_bytes=committed,
                )
                append_seconds = time.perf_counter() - append_started
                for number, row_id, item in zip(
                    range(start, start + len(page)), ids, page, strict=True
                ):
                    if number in sample_numbers:
                        row_map[number] = row_id
                    length = len(item["body"].encode())
                    body_bytes += length
                    text_lengths[
                        "under_2k"
                        if length < 2048
                        else "under_16k"
                        if length < 16384
                        else "under_96k"
                        if length < 98304
                        else "at_least_96k"
                    ] += 1
                    amount_states[
                        "missing"
                        if item["amount"] is None
                        else "invalid"
                        if item["amount"] == "not-stated"
                        else "valid"
                    ] += 1
                    category = str(item["category"])
                    category_counts[category] += 1
                    facts = aggregates.setdefault(
                        category,
                        {
                            "rows": 0,
                            "valid": 0,
                            "missing": 0,
                            "invalid": 0,
                            "sum": 0,
                            "values": [],
                        },
                    )
                    facts["rows"] += 1
                    amount = item["amount"]
                    if amount is None:
                        facts["missing"] += 1
                    elif amount == "not-stated":
                        facts["invalid"] += 1
                    else:
                        amount = int(amount)
                        facts["valid"] += 1
                        facts["sum"] += amount
                        facts["values"].append(amount)
                        heapq.heappush(top_amounts, (amount, -number, number))
                        if len(top_amounts) > 50:
                            heapq.heappop(top_amounts)
                        heapq.heappush(bottom_amounts, (-amount, -number, number))
                        if len(bottom_amounts) > 50:
                            heapq.heappop(bottom_amounts)
                    date_first.append((str(item["published_at"]), number))
                    if len(date_first) > 50:
                        date_first.remove(max(date_first))
                    category_first.append((category, number))
                    if len(category_first) > 50:
                        category_first.remove(max(category_first))
                    if category == "environment" and len(environment_first) < 50:
                        environment_first.append(number)
                input_bytes += committed
                cursor += len(page)
                _guard(sampler, soft_scratch=soft_scratch)
                import_pages.append(
                    {
                        "start_record": start,
                        "rows": len(page),
                        "generation_seconds": generated,
                        "append_seconds": append_seconds,
                        "committed_bytes": committed,
                        "scratch_bytes": directory_bytes(owned),
                        "rss_kib": current_rss_kib(),
                    }
                )
            publication = writer.finalize_session(expected_cursor=cursor)
            assert publication.row_count == rows
            columns = {
                str(c["name"]): int(c["id"])
                for c in project.columns(publication.sheet_id)
            }
            assert set(columns) == {spec["name"] for spec in COLUMNS}
            assert project.row_count(publication.sheet_id) == rows
            _check_values(project, publication.sheet_id, columns, row_map)
            report["import_metadata"] = _verify_import_metadata(
                project, publication, columns, rows
            )
            report["phases"]["import"] = _phase_record(bundle, started)

            sampler.set_phase("current_reopen")
            started_reopen = time.perf_counter()
            project.close()
            project = Project(bundle)
            _check_values(
                project,
                publication.sheet_id,
                columns,
                row_map,
                origin_kind="source_cell",
            )
            pages = probe_current_pages(project, publication.sheet_id, columns, rows)
            body_started = time.perf_counter()
            bodies = project.get_values(
                publication.sheet_id, columns["body"], list(row_map.values())
            )
            assert all(
                bodies[row_id] == _expected_record(number)["body"]
                for number, row_id in row_map.items()
            )
            report["current_reopen"] = {
                "projected_pages": pages,
                "full_body_sample": {
                    "rows": len(bodies),
                    "utf8_bytes": sum(
                        len(str(value).encode()) for value in bodies.values()
                    ),
                    "seconds": time.perf_counter() - body_started,
                },
            }
            report["phases"]["current_reopen"] = _phase_record(bundle, started_reopen)
            sampler.set_phase("persisted_readback")
            started_readback = time.perf_counter()
            report["persisted_corpus"] = _verify_persisted_corpus(
                project,
                publication.sheet_id,
                columns,
                rows,
                sampler,
                soft_scratch,
            )
            report["phases"]["persisted_readback"] = _phase_record(
                bundle, started_readback
            )
            amount_values_by_category = {}
            for category, facts in aggregates.items():
                values = facts.pop("values")
                amount_values_by_category[category] = values
                facts["mean"] = statistics.mean(values)
                facts["median"] = statistics.median(values)
            expected_queries = {
                "rows": rows,
                "categories": dict(category_counts),
                "aggregates": aggregates,
                "amount_values_by_category": amount_values_by_category,
                "amount_desc_record_ids": [
                    entry[2]
                    for entry in sorted(
                        top_amounts, key=lambda value: (-value[0], value[2])
                    )
                ],
                "amount_asc_record_ids": [
                    entry[2]
                    for entry in sorted(
                        bottom_amounts, key=lambda value: (-value[0], value[2])
                    )
                ],
                "date_asc_record_ids": [number for _date, number in sorted(date_first)],
                "category_asc_record_ids": [
                    number for _category, number in sorted(category_first)
                ],
                "environment_first_record_ids": environment_first,
            }
            report["fixture"] = {
                "input_ndjson_bytes": input_bytes,
                "body_utf8_bytes": body_bytes,
                "text_lengths": dict(text_lengths),
                "amount_states": dict(amount_states),
                "generation_seconds": generation_seconds,
                "import_pages": import_pages,
            }
            sampler.set_phase("index")
            started = time.perf_counter()
            report["phases"]["index"] = _drain_index(project, sampler, soft_scratch)
            report["phases"]["index"].update(_phase_record(bundle, started))
            assert (
                project.db.execute(
                    "SELECT COUNT(*) FROM search_dirty_scopes"
                ).fetchone()[0]
                == 0
            )
            sidecar = sqlite3.connect(bundle / "project.search.db")
            try:
                fts_cells = int(
                    sidecar.execute("SELECT COUNT(*) FROM cell_fts_docsize").fetchone()[0]
                )
                complete_revision = int(
                    sidecar.execute(
                        "SELECT value FROM fts_state WHERE key='complete_revision'"
                    ).fetchone()[0]
                )
            finally:
                sidecar.close()
            revision = latest_revision(project.db)
            assert fts_cells == rows * 4
            assert complete_revision == revision
            report["index_validation"] = {
                "searchable_cells": fts_cells,
                "expected_searchable_cells": rows * 4,
                "complete_revision": complete_revision,
                "project_revision": revision,
                "dirty_scopes": 0,
            }

            def governed(call):
                return _governed_query(project, sampler, soft_scratch, call)

            sampler.set_phase("search")
            started = time.perf_counter()
            report["search"] = run_search_workload(
                project,
                publication.sheet_id,
                columns,
                rows,
                governed,
            )
            report["phases"]["search"] = _phase_record(bundle, started)

            sampler.set_phase("grid_queries")
            started = time.perf_counter()
            report["grid_queries"] = run_grid_workload(
                project,
                publication.sheet_id,
                columns,
                expected_queries,
                governed,
            )
            report["phases"]["grid_queries"] = _phase_record(bundle, started)

            sampler.set_phase("analytics")
            started = time.perf_counter()
            report["analytics"] = run_analytics_workload(
                project,
                publication.sheet_id,
                columns,
                expected_queries,
                governed,
                cancel_event=sampler.hard_limit,
            )
            report["phases"]["analytics"] = _phase_record(bundle, started)

            sampler.set_phase("edit_undo_redo")
            started = time.perf_counter()
            report["mutations"], mutation_state = run_mutation_workload(
                project,
                publication.sheet_id,
                columns,
                rows,
                lambda active: _drain_index(active, sampler, soft_scratch),
                lambda: _guard(sampler, soft_scratch=soft_scratch),
            )
            apply_expected_amount_edits(
                expected_queries, mutation_state["ordinary_edits"]
            )
            report["phases"]["edit_undo_redo"] = _phase_record(bundle, started)

            sampler.set_phase("history")
            started = time.perf_counter()
            report["history"] = verify_history(project, mutation_state)
            report["phases"]["history"] = _phase_record(bundle, started)

            sampler.set_phase("checkpoint_reopen")
            started = time.perf_counter()
            before_checkpoint = _file_sizes(bundle)
            checkpoint_started = time.perf_counter()
            checkpoint_result = list(
                project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            )
            checkpoint_seconds = time.perf_counter() - checkpoint_started
            after_checkpoint = _file_sizes(bundle)
            project.close()
            closed_files = _file_sizes(bundle)
            physical_accounting = {
                "project": _dbstat_accounting(bundle / "project.db"),
                "search": _dbstat_accounting(bundle / "project.search.db"),
            }
            project = Project(bundle)
            assert project.row_count(publication.sheet_id) == rows
            _assertions = [
                *mutation_state["ordinary_edits"],
                *mutation_state["batch_edits"],
            ]
            assert_edit_values(
                project,
                publication.sheet_id,
                _assertions,
                edited=True,
            )
            marker_edit = next(
                edit
                for edit in mutation_state["batch_edits"]
                if edit["number"] == report["mutations"]["marker_number"]
            )
            reopened_search = run_reopen_marker_check(
                project,
                report["mutations"]["new_marker"],
                report["mutations"]["old_marker"],
                int(marker_edit["row_id"]),
            )
            reopened_history = verify_history(project, mutation_state)
            reopened_grid = run_grid_workload(
                project,
                publication.sheet_id,
                columns,
                expected_queries,
                governed,
                samples=1,
            )
            reopened_analytics = run_analytics_workload(
                project,
                publication.sheet_id,
                columns,
                expected_queries,
                governed,
                samples=1,
                cancel_event=sampler.hard_limit,
            )
            report["checkpoint_reopen"] = {
                "checkpoint_seconds": checkpoint_seconds,
                "checkpoint_result": checkpoint_result,
                "files_before": before_checkpoint,
                "files_after": after_checkpoint,
                "closed_steady_files": closed_files,
                "physical_accounting": physical_accounting,
                "grid": reopened_grid,
                "analytics": reopened_analytics,
                "history": reopened_history,
                "marker_search": reopened_search,
            }
            report["phases"]["checkpoint_reopen"] = _phase_record(bundle, started)
            report["status"] = "completed"
        except ResourceStop as exc:
            report["status"] = "resource_stopped"
            report["stop_reason"] = str(exc)
        except KeyboardInterrupt:
            report["status"] = "interrupted"
            report["stop_reason"] = "received KeyboardInterrupt"
        finally:
            if project is not None:
                project.close()
            sampler.stop()
            if previous_sqlite_tmpdir is None:
                os.environ.pop("SQLITE_TMPDIR", None)
            else:
                os.environ["SQLITE_TMPDIR"] = previous_sqlite_tmpdir
            if report["status"] == "completed" and sampler.hard_limit.is_set():
                report["status"] = "resource_stopped"
                report["stop_reason"] = (
                    sampler.limit_reason or "sampled resource limit reached"
                )
            report["sampled_phase_peaks"] = sampler.peaks
            report["host_free_bytes_min"] = min(
                phase["host_free_bytes_min"] for phase in sampler.peaks.values()
            )
            report["peak_rss_kib"] = (
                __import__("resource")
                .getrusage(__import__("resource").RUSAGE_SELF)
                .ru_maxrss
            )
            if report["status"] == "completed" and retain_success:
                report["retention"]["bundle"] = _retain_completed_bundle(
                    bundle, work_root
                )
    report["owned_scratch_removed"] = True
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--composition-sha", required=True)
    parser.add_argument(
        "--retain-success",
        action="store_true",
        help="retain one completed, read-only bundle for follow-on measurement",
    )
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    result = run_qualification(
        args.rows,
        args.work_dir,
        composition_sha=args.composition_sha,
        retain_success=args.retain_success,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    summary = {"status": result["status"], "output": str(args.output)}
    if result["retention"]["bundle"] is not None:
        summary["retained_bundle"] = result["retention"]["bundle"]
    print(json.dumps(summary))


if __name__ == "__main__":
    main()

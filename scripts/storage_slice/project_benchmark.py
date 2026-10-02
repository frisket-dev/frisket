"""Bounded qualification of Frisket's actual Project schema and store."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import heapq
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import tempfile
import time
from typing import Any

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.querysets import resolve_sheet_filter_rows
from frisket.search import search_project_page
from frisket.search_index import index_batch
from frisket.server.run_payloads import history_page_payload
from frisket.server.services.project_qa_analytics import evaluate_analytics

from .benchmark import PhaseSampler, current_rss_kib, directory_bytes
from .fixtures import stress_project_marker, stress_project_records


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


def _phase_record(bundle: Path, started: float) -> dict[str, Any]:
    return {
        "seconds": time.perf_counter() - started,
        "rss_kib": current_rss_kib(),
        "bundle_bytes": directory_bytes(bundle),
        "files": _file_sizes(bundle),
        "mem_available_kib": _mem_available_kib(),
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
    started = time.perf_counter()
    while True:
        progress = index_batch(project, batch_size=5000, max_bytes=8 * MIB)
        batches += 1
        cells += progress.processed
        payload_bytes += progress.processed_bytes
        _guard(sampler, soft_scratch=soft_scratch)
        if progress.complete:
            break
        if progress.processed <= 0:
            raise AssertionError("incomplete index batch made no progress")
    return {
        "seconds": time.perf_counter() - started,
        "batches": batches,
        "processed_units": cells,
        "processed_value_bytes": payload_bytes,
    }


def _action(project: Project, action_id: str, params: dict, key: str):
    result = run_action_spec(
        project,
        {
            "action_id": action_id,
            "scope": {"kind": "project"},
            "params": params,
            "idempotency_key": key,
        },
        project_id="storage-project-qualification",
    )
    if result.status != "completed":
        raise AssertionError(f"{action_id} failed: {result.model_dump(mode='json')}")
    return result


def _expected_record(row_number: int) -> dict:
    return next(stress_project_records(row_number, 1))


def _governed_query(project: Project, sampler: PhaseSampler, soft_scratch: int, call):
    """Interrupt an active SQLite statement when the sampler trips."""

    _guard(sampler, soft_scratch=soft_scratch)
    sampler.on_limit = project.db.interrupt
    try:
        value = call()
    except sqlite3.OperationalError as exc:
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


def _query_workload(
    project: Project,
    sheet_id: int,
    columns: dict[str, int],
    expected: dict[str, Any],
    sampler: PhaseSampler,
    soft_scratch: int,
) -> dict[str, Any]:
    timings: dict[str, list[float]] = {
        "sort": [],
        "filter": [],
        "aggregate": [],
        "median": [],
    }
    sort_spec = json.dumps([{"column": "amount", "dir": "desc"}])
    filter_spec = json.dumps({"category": {"eq": "courts"}})
    sorted_page = filtered = analytics = median = None
    for _ in range(5):
        started = time.perf_counter()
        sorted_page = _governed_query(
            project,
            sampler,
            soft_scratch,
            lambda: resolve_sheet_filter_rows(
                project, sheet_id, sort=sort_spec, limit=50
            ),
        )
        timings["sort"].append(time.perf_counter() - started)
        started = time.perf_counter()
        filtered = _governed_query(
            project,
            sampler,
            soft_scratch,
            lambda: resolve_sheet_filter_rows(
                project, sheet_id, filter_=filter_spec, limit=50
            ),
        )
        timings["filter"].append(time.perf_counter() - started)
        started = time.perf_counter()
        analytics = _governed_query(
            project,
            sampler,
            soft_scratch,
            lambda: evaluate_analytics(
                project,
                {
                    "sheet_id": sheet_id,
                    "groups": [{"column_id": columns["category"]}],
                    "metrics": [
                        {"id": "rows", "kind": "count"},
                        {
                            "id": "values",
                            "kind": "value_count",
                            "column_id": columns["amount"],
                        },
                        {
                            "id": "missing",
                            "kind": "missing_count",
                            "column_id": columns["amount"],
                        },
                        {
                            "id": "sum",
                            "kind": "sum",
                            "column_id": columns["amount"],
                        },
                    ],
                    "sort": [{"kind": "group", "group_index": 0, "direction": "asc"}],
                    "limit": 20,
                },
                {"kind": "sheet", "sheet_id": sheet_id},
                cancel_event=sampler.hard_limit,
            ),
        )
        timings["aggregate"].append(time.perf_counter() - started)
        started = time.perf_counter()
        median = _governed_query(
            project,
            sampler,
            soft_scratch,
            lambda: evaluate_analytics(
                project,
                {
                    "sheet_id": sheet_id,
                    "filter": {"category": {"eq": "transport"}},
                    "metrics": [
                        {
                            "id": "median",
                            "kind": "median",
                            "column_id": columns["amount"],
                        }
                    ],
                },
                {"kind": "sheet", "sheet_id": sheet_id},
                cancel_event=sampler.hard_limit,
            ),
        )
        timings["median"].append(time.perf_counter() - started)
    assert sorted_page is not None and sorted_page.total == expected["rows"]
    sorted_ids = project.get_values(sheet_id, columns["record_id"], sorted_page.row_ids)
    assert [sorted_ids[row_id] for row_id in sorted_page.row_ids] == expected[
        "top_amount_record_ids"
    ]
    assert filtered is not None
    assert filtered.total == expected["categories"]["courts"]
    filtered_categories = project.get_values(
        sheet_id, columns["category"], filtered.row_ids
    )
    assert set(filtered_categories.values()) == {"courts"}
    assert analytics is not None and analytics["row_count"] == sorted_page.total
    groups = {group["group"][0]["value"]: group for group in analytics["groups"]}
    assert set(groups) == set(expected["aggregates"])
    for category, facts in expected["aggregates"].items():
        group = groups[category]
        assert group["metrics"] == {
            "rows": facts["rows"],
            "values": facts["valid"],
            "missing": facts["missing"],
            "sum": facts["sum"],
        }
        assert group["quality"][str(columns["amount"])] == {
            "column_id": columns["amount"],
            "present": facts["valid"],
            "missing": facts["missing"],
            "invalid": facts["invalid"],
        }
    assert median is not None
    assert median["groups"][0]["metrics"]["median"] == expected["transport_median"]
    return {
        name: {
            "samples": len(samples),
            "median_ms": sorted(samples)[len(samples) // 2] * 1000,
            "max_ms": max(samples) * 1000,
        }
        for name, samples in timings.items()
    } | {"filtered_rows": filtered.total, "analytics_groups": len(analytics["groups"])}


def run_qualification(rows: int, work_root: Path) -> dict[str, Any]:
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
    }
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
            transport_amounts: list[int] = []
            row_map: dict[int, int] = {}
            sample_numbers = sorted(
                {1, min(rows, 2), min(rows, 199), (rows + 1) // 2, rows}
            )
            cursor = 0
            for start in range(1, rows + 1, PAGE_ROWS):
                page = list(
                    stress_project_records(start, min(PAGE_ROWS, rows - start + 1))
                )
                encoded = [
                    json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode()
                    + b"\n"
                    for item in page
                ]
                committed = sum(map(len, encoded))
                ids = writer.append_page(
                    page,
                    expected_cursor=cursor,
                    next_cursor=cursor + len(page),
                    committed_bytes=committed,
                )
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
                        heapq.heappush(top_amounts, (amount, -number, number))
                        if len(top_amounts) > 50:
                            heapq.heappop(top_amounts)
                        if category == "transport":
                            transport_amounts.append(amount)
                input_bytes += committed
                cursor += len(page)
                _guard(sampler, soft_scratch=soft_scratch)
            publication = writer.finalize_session(expected_cursor=cursor)
            assert publication.row_count == rows
            columns = {
                str(c["name"]): int(c["id"])
                for c in project.columns(publication.sheet_id)
            }
            assert set(columns) == {spec["name"] for spec in COLUMNS}
            assert project.row_count(publication.sheet_id) == rows
            _check_values(project, publication.sheet_id, columns, row_map)
            report["phases"]["import"] = _phase_record(bundle, started)
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
            expected_queries = {
                "rows": rows,
                "categories": dict(category_counts),
                "aggregates": aggregates,
                "top_amount_record_ids": [
                    entry[2]
                    for entry in sorted(
                        top_amounts, key=lambda value: (-value[0], value[2])
                    )
                ],
                "transport_median": statistics.median(transport_amounts),
            }
            report["fixture"] = {
                "input_ndjson_bytes": input_bytes,
                "body_utf8_bytes": body_bytes,
                "text_lengths": dict(text_lengths),
                "amount_states": dict(amount_states),
            }
            sampler.set_phase("index")
            started = time.perf_counter()
            report["phases"]["index"] = _drain_index(project, sampler, soft_scratch)
            report["phases"]["index"].update(_phase_record(bundle, started))

            marker_number = (
                max(number for number in range(1, rows + 1) if number % 200 == 199)
                if rows >= 199
                else None
            )
            if marker_number is not None:
                marker = stress_project_marker(marker_number)
                search = search_project_page(project, marker, limit=10, rerank="off")
                assert search["complete"] and any(
                    marker in hit["snip"] for hit in search["hits"]
                )
                assert max(len(hit["snip"].encode()) for hit in search["hits"]) < 4096
                marker_row_id = project.db.execute(
                    "SELECT row_id FROM current_cells WHERE column_id=? AND value=json(?)",
                    (columns["record_id"], marker_number),
                ).fetchone()[0]
            else:
                marker_number, marker_row_id, marker = 1, row_map[1], None

            sampler.set_phase("queries")
            started = time.perf_counter()
            report["queries"] = _query_workload(
                project,
                publication.sheet_id,
                columns,
                expected_queries,
                sampler,
                soft_scratch,
            )
            report["phases"]["queries"] = _phase_record(bundle, started)

            sampler.set_phase("edit_undo_redo")
            started = time.perf_counter()
            edited_marker = f"frisketedited{marker_number:08d}"
            original_body = _expected_record(marker_number)["body"]
            edited_body = (
                original_body
                if marker is None
                else original_body.replace(marker, edited_marker)
            )
            edit = _action(
                project,
                "cell.edit",
                {
                    "edits": [
                        {
                            "row_id": int(marker_row_id),
                            "column_id": columns["body"],
                            "value": edited_body,
                        }
                    ]
                },
                "qualification-edit-v1",
            )
            assert len(edit.op_ids) == 1
            report["edit_index"] = _drain_index(project, sampler, soft_scratch)
            if marker is not None:
                assert not search_project_page(project, marker, rerank="off")["hits"]
                assert search_project_page(project, edited_marker, rerank="off")["hits"]
            _action(
                project,
                "operation.undo",
                {"expected_op_id": edit.op_ids[0]},
                "qualification-undo-v1",
            )
            _drain_index(project, sampler, soft_scratch)
            if marker is not None:
                assert search_project_page(project, marker, rerank="off")["hits"]
                assert not search_project_page(project, edited_marker, rerank="off")[
                    "hits"
                ]
            _action(
                project,
                "operation.redo",
                {"expected_op_id": edit.op_ids[0]},
                "qualification-redo-v1",
            )
            _drain_index(project, sampler, soft_scratch)
            if marker is not None:
                assert search_project_page(project, edited_marker, rerank="off")["hits"]
            history = history_page_payload(project, limit=20)
            assert any(
                item["id"] == edit.op_ids[0] and item["status"] == "applied"
                for item in history["ops"]
            )
            report["history"] = {
                "total": history["total"],
                "returned": len(history["ops"]),
            }
            report["phases"]["edit_undo_redo"] = _phase_record(bundle, started)

            sampler.set_phase("checkpoint_reopen")
            started = time.perf_counter()
            project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            project.close()
            project = Project(bundle)
            assert project.row_count(publication.sheet_id) == rows
            _check_values(
                project,
                publication.sheet_id,
                columns,
                row_map,
                body_overrides={marker_number: edited_body},
            )
            if marker is not None:
                reopened = search_project_page(project, edited_marker, rerank="off")
                assert reopened["complete"] and reopened["hits"]
            assert history_page_payload(project, limit=20)["total"] == history["total"]
            report["phases"]["checkpoint_reopen"] = _phase_record(bundle, started)
            report["status"] = "completed"
        except ResourceStop as exc:
            report["status"] = "resource_stopped"
            report["stop_reason"] = str(exc)
        finally:
            if project is not None:
                project.close()
            sampler.stop()
            if previous_sqlite_tmpdir is None:
                os.environ.pop("SQLITE_TMPDIR", None)
            else:
                os.environ["SQLITE_TMPDIR"] = previous_sqlite_tmpdir
            report["sampled_phase_peaks"] = sampler.peaks
            report["peak_rss_kib"] = (
                __import__("resource")
                .getrusage(__import__("resource").RUSAGE_SELF)
                .ru_maxrss
            )
    report["owned_scratch_removed"] = True
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    result = run_qualification(args.rows, args.work_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": result["status"], "output": str(args.output)}))


if __name__ == "__main__":
    main()

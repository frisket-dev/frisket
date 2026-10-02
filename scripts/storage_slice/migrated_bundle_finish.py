"""One-off completion of the measured main/search migration; no product code."""

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.search_storage import configure_search_connection, reclaim_search_storage
from scripts.storage_slice.benchmark import PhaseSampler, directory_bytes
from scripts.storage_slice.project_benchmark import (
    GIB,
    HOST_FREE_RESERVE,
    _dbstat_accounting,
    _drain_index,
    _file_sizes,
    _guard,
    _governed_query,
    _mem_available_kib,
)
from scripts.storage_slice.project_migration_probe import _raw_digests
from scripts.storage_slice.project_workloads import (
    _record_row_ids,
    run_search_workload,
    run_reopen_marker_check,
)

parser = argparse.ArgumentParser()
parser.add_argument("--bundle", type=Path, required=True)
parser.add_argument("--work-dir", type=Path, required=True)
parser.add_argument("--baseline", type=Path, required=True)
parser.add_argument("--migration", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
assert args.bundle.resolve().is_relative_to(args.work_dir.resolve())
baseline = json.loads(args.baseline.read_text())
migration = json.loads(args.migration.read_text())
assert baseline["status"] == migration["status"] == "completed"
initial_bytes = directory_bytes(args.work_dir)
free = shutil.disk_usage(args.work_dir).free
assert free + initial_bytes - HOST_FREE_RESERVE >= 28 * GIB
soft = 26 * GIB
report = {
    "status": "running",
    "candidate_sha": migration["candidate_sha"],
    "before_files": _file_sizes(args.bundle),
    "free_before": free,
    "initial_work_bytes": initial_bytes,
    "phases": {},
}


def limit(observed):
    if shutil.disk_usage(args.work_dir).free < HOST_FREE_RESERVE:
        return "host free-space reserve"
    if observed["project_bytes"] >= soft:
        return "26 GiB observed scratch ceiling"
    if observed["rss_kib"] >= 6 * 1024**2:
        return "6 GiB RSS ceiling"
    available = _mem_available_kib()
    if available is not None and available < 4 * 1024**2:
        return "4 GiB MemAvailable floor"
    return None


sampler = PhaseSampler(args.work_dir, hard_limit_bytes=28 * GIB, limit_probe=limit)
sampler.start()
project = None
try:
    project = Project(args.bundle)
    sampler.on_limit = project.db.interrupt
    project.db.set_progress_handler(lambda: int(sampler.hard_limit.is_set()), 10000)
    sampler.set_phase("compact_main")
    started = time.perf_counter()
    report["compact"] = project.compact()
    _guard(sampler, soft_scratch=soft)
    report["main_checkpoint"] = list(
        project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    )
    report["phases"]["compact_main_seconds"] = time.perf_counter() - started
    report["after_main_files"] = _file_sizes(args.bundle)
    project.db.set_progress_handler(None, 0)
    sampler.on_limit = None

    sampler.set_phase("rebuild_search")
    report["index"] = _drain_index(project, sampler, soft)
    sampler.set_phase("reclaim_search")
    started = time.perf_counter()
    assert reclaim_search_storage(args.bundle / "project.search.db")
    report["phases"]["reclaim_search_seconds"] = time.perf_counter() - started
    _guard(sampler, soft_scratch=soft)

    sampler.set_phase("search_verification")
    db = sqlite3.connect(args.bundle / "project.search.db")
    configure_search_connection(db)
    db.set_progress_handler(lambda: int(sampler.hard_limit.is_set()), 10000)
    try:
        assert (
            db.execute("SELECT COUNT(*) FROM cell_fts_docsize").fetchone()[0]
            == baseline["index_validation"]["expected_searchable_cells"]
        )
        db.execute("INSERT INTO cell_fts(cell_fts,rank) VALUES ('integrity-check',1)")
        db.commit()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    sheet = next(s for s in project.sheets() if s["name"] == "Investigative records")
    columns = {c["name"]: int(c["id"]) for c in project.columns(sheet["id"])}
    report["search"] = run_search_workload(
        project,
        sheet["id"],
        columns,
        baseline["rows_requested"],
        lambda call: _governed_query(project, sampler, soft, call),
    )
    mutations = baseline["mutations"]
    marker_row = _record_row_ids(
        project, columns["record_id"], [mutations["marker_number"]]
    )[mutations["marker_number"]]
    report["edited_marker"] = run_reopen_marker_check(
        project, mutations["new_marker"], mutations["old_marker"], marker_row
    )
    report["history_total"] = project.history_total()
    assert report["history_total"] == baseline["history"]["total"]
    for receipt_id in baseline["history"]["checked_receipt_ids"]:
        receipt = ReceiptStore(project).find_by_id(receipt_id)
        assert receipt is not None and receipt.status == "completed"
    project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    project.close()
    project = None
    assert _raw_digests(args.bundle) == migration["after"]["digests"]
    report["closed_files"] = _file_sizes(args.bundle)
    report["physical_accounting"] = {
        name: _dbstat_accounting(args.bundle / name)
        for name in ("project.db", "project.search.db")
    }
    report["status"] = "completed"
except BaseException as exc:
    report["status"] = "failed"
    report["error"] = f"{type(exc).__name__}: {exc}"
    raise
finally:
    if project is not None:
        project.close()
    sampler.stop()
    if sampler.hard_limit.is_set():
        report["status"] = "resource_stopped"
        report["stop_reason"] = sampler.limit_reason
    report["peaks"] = sampler.peaks
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
print(json.dumps({"status": report["status"], "output": str(args.output)}))

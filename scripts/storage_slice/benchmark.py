"""Run one bounded storage slice per fresh process; emits measurements, not SLAs."""

from __future__ import annotations

import argparse
from collections import Counter
import concurrent.futures
import importlib.metadata
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .fixtures import (
    sample_documents,
    sample_extractions,
    stress_documents,
    stress_extractions,
    synthetic_documents,
    synthetic_extractions,
)
from .store import ProjectStore


def distribution(samples):
    if not samples:
        return {"count": 0}
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "mean_ms": statistics.mean(ordered) * 1000,
        **{
            f"p{percent}_ms": ordered[
                max(0, math.ceil(percent / 100 * len(ordered)) - 1)
            ]
            * 1000
            for percent in ((50, 95, 99) if len(ordered) >= 100 else (50,))
        },
        "max_ms": ordered[-1] * 1000,
    }


def directory_bytes(path):
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except FileNotFoundError:
            pass
    return total


def temporary_bytes(path):
    total = 0
    for item in path.rglob("*"):
        try:
            relative = str(item.relative_to(path))
            if item.is_file() and (
                relative.endswith(("-wal", "-shm", ".wal"))
                or ".tmp" in item.name
                or any(".tmp" in part for part in item.parts)
            ):
                total += item.stat().st_size
        except FileNotFoundError:
            pass
    return total


def current_rss_kib():
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


class PhaseSampler:
    def __init__(self, root, *, hard_limit_bytes=10 * 1024**3, limit_probe=None):
        self.root = Path(root)
        self.hard_limit_bytes = hard_limit_bytes
        self.limit_probe = limit_probe
        self.limit_reason = None
        self.on_limit = None
        self.phase = "setup"
        self.peaks = {}
        self.stop_event = threading.Event()
        self.hard_limit = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def set_phase(self, phase):
        self.phase = phase
        self.sample()

    def sample(self):
        phase = self.phase
        observed = self.peaks.setdefault(
            phase,
            {
                "rss_kib": 0,
                "project_bytes": 0,
                "temporary_bytes": 0,
                "host_load_1m_max": 0,
            },
        )
        rss = current_rss_kib()
        if rss is not None:
            observed["rss_kib"] = max(observed["rss_kib"], rss)
        size = directory_bytes(self.root)
        observed["project_bytes"] = max(observed["project_bytes"], size)
        observed["temporary_bytes"] = max(
            observed["temporary_bytes"], temporary_bytes(self.root)
        )
        if hasattr(os, "getloadavg"):
            observed["host_load_1m_max"] = max(
                observed["host_load_1m_max"], os.getloadavg()[0]
            )
        reason = None
        if size >= self.hard_limit_bytes:
            reason = "hard scratch limit reached"
        elif self.limit_probe is not None:
            reason = self.limit_probe(observed)
        if reason and not self.hard_limit.is_set():
            self.limit_reason = str(reason)
            self.hard_limit.set()
            if self.on_limit is not None:
                self.on_limit()

    def _run(self):
        while not self.stop_event.wait(0.25):
            self.sample()

    def stop(self):
        self.sample()
        self.stop_event.set()
        self.thread.join(timeout=2)


class ScratchLimit(RuntimeError):
    pass


class TimedDocuments:
    def __init__(self, source):
        self.source = iter(source)
        self.generation_seconds = 0.0
        self.rows = 0
        self.text_bytes = 0
        self.categories = Counter()
        self.amount_states = Counter()
        self.text_lengths = Counter()

    def __iter__(self):
        return self

    def __next__(self):
        started = time.perf_counter()
        document = next(self.source)
        self.generation_seconds += time.perf_counter() - started
        length = len(document.text.encode())
        self.rows += 1
        self.text_bytes += length
        self.categories[document.category] += 1
        self.amount_states[
            "missing"
            if document.raw_amount is None
            else "invalid"
            if document.raw_amount == "not-stated"
            else "valid"
        ] += 1
        self.text_lengths[
            "under_2k"
            if length < 2 * 1024
            else "under_16k"
            if length < 16 * 1024
            else "under_96k"
            if length < 96 * 1024
            else "at_least_96k"
        ] += 1
        return document

    def report(self):
        return {
            "rows": self.rows,
            "text_bytes": self.text_bytes,
            "generation_seconds": self.generation_seconds,
            "categories": dict(sorted(self.categories.items())),
            "amount_states": dict(sorted(self.amount_states.items())),
            "text_lengths": dict(sorted(self.text_lengths.items())),
        }


def measured(call):
    started = time.perf_counter()
    value = call()
    return value, time.perf_counter() - started


def concurrent_probe(
    store,
    *,
    documents,
    first_row_id,
    total_rows,
    ui_operations,
    operation_prefix,
    load_active,
    first_batch,
    timeout,
):
    gate = threading.Barrier(3)
    importing = threading.Event()
    background_rows = max(500, min(total_rows, 5000))
    import_duration = 0.0
    scan_samples = []

    def append():
        nonlocal import_duration
        gate.wait(timeout=20)
        importing.set()
        load_active.set()
        try:
            _, import_duration = measured(
                lambda: store.import_documents(
                    f"{operation_prefix}-background-import",
                    documents(first_row_id, background_rows),
                    batch_size=250,
                )
            )
        finally:
            importing.clear()

    def scan():
        gate.wait(timeout=20)
        if not first_batch.wait(timeout=30):
            raise RuntimeError("background import did not publish a staging batch")
        for _ in range(ui_operations):
            _, duration = measured(store.aggregate)
            scan_samples.append(duration)
            time.sleep(0.02)

    ui_reads, ui_edits, overlap_reads, overlap_edits = [], [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(append)
        reader = pool.submit(scan)
        gate.wait(timeout=20)
        if not first_batch.wait(timeout=30):
            raise RuntimeError("background import did not publish a staging batch")
        try:
            for index in range(ui_operations):
                overlap = importing.is_set()
                row, duration = measured(lambda: store.read_rows(limit=1)[0])
                ui_reads.append(duration)
                if overlap:
                    overlap_reads.append(duration)
                overlap = importing.is_set()
                _, duration = measured(
                    lambda: store.review_value(
                        f"{operation_prefix}-ui-edit-{index}",
                        row["row_id"],
                        "edit",
                        expected_version=row["current_version"],
                        value=12345 + index,
                    )
                )
                ui_edits.append(duration)
                if overlap:
                    overlap_edits.append(duration)
                time.sleep(0.02)
        finally:
            load_active.clear()
        writer.result(timeout=timeout)
        reader.result(timeout=timeout)
    return {
        "ui_reads": distribution(ui_reads),
        "ui_edits": distribution(ui_edits),
        "reads_started_during_import": distribution(overlap_reads),
        "edits_started_during_import": distribution(overlap_edits),
        "import_overlap_observed": bool(overlap_reads and overlap_edits),
        "background_import_rows": background_rows,
        "background_import_seconds": import_duration,
        "background_scans": len(scan_samples),
        "background_scan_latency": distribution(scan_samples),
        "note": "Fixed logical UI/aggregate work with 20ms pacing; operations begin after the first durable background-import batch. One capped import; overlap differs with engine throughput and only observed subsets are reported. Warm local-file workload, no cold-cache claim.",
    }


def run_engine(engine, rows, parent, ui_operations):
    import sqlite3

    with tempfile.TemporaryDirectory(
        prefix=f"slice-{engine}-", dir=parent
    ) as temporary:
        root = Path(temporary)
        path = root / "project"
        report = {
            "engine": engine,
            "sqlite_version": sqlite3.sqlite_version,
            "duckdb_version": importlib.metadata.version("duckdb"),
            "requested_synthetic_rows": rows,
        }
        load_active = threading.Event()
        first_batch = threading.Event()

        def checkpoint(label):
            if label == "import.batch_committed" and load_active.is_set():
                first_batch.set()

        with ProjectStore(path, engine, checkpoint=checkpoint) as store:
            sample = sample_documents()
            _, report["sample_import_seconds"] = measured(
                lambda: store.import_documents("sample-import", sample)
            )
            _, report["sample_extraction_seconds"] = measured(
                lambda: store.publish_extraction(
                    "sample-extract", sample_extractions(store.read_rows())
                )
            )
            original = store.read_rows(limit=1)[0]
            evidence = store.citations(original["row_id"])
            assert evidence, "sample flow must have a source citation"
            citation = store.resolve_citation(evidence[0]["citation_id"])
            assert citation["text"][citation["start"] : citation["end"]], (
                "sample quote must resolve"
            )
            store.review_value(
                "sample-review",
                original["row_id"],
                "edit",
                expected_version=original["current_version"],
                value=12345,
            )
            assert store.read_rows(limit=1)[0]["amount_cents"] == 12345
            assert store.resolve_citation(evidence[0]["citation_id"])["stale"]
            report["sample_flow"] = {
                "documents": len(sample),
                "citation_excerpt": citation["text"][
                    citation["start"] : citation["end"]
                ][:200],
                "edited_amount_cents": 12345,
            }

            _, report["bulk_import_seconds"] = measured(
                lambda: store.import_documents(
                    "bulk-import", synthetic_documents(100, rows), batch_size=500
                )
            )
            after = 99
            started = time.perf_counter()
            extracted = 0
            while page := store.read_rows(after_row_id=after, limit=500):
                store.publish_extraction(
                    f"bulk-extract-{after}", synthetic_extractions(page)
                )
                extracted += len(page)
                after = page[-1]["row_id"]
            report["bulk_extraction_seconds"] = time.perf_counter() - started
            report["extracted_synthetic_rows"] = extracted
            report["before_load_counts"] = store.logical_counts()
            report["before_load_bytes"] = directory_bytes(path)
            aggregates = []
            points = []
            sorts = []
            for _ in range(10):
                _, duration = measured(store.aggregate)
                aggregates.append(duration)
                _, duration = measured(lambda: store.read_rows(limit=1))
                points.append(duration)
                _, duration = measured(
                    lambda: store.read_rows(limit=50, sort="amount_cents")
                )
                sorts.append(duration)
            report["warm_aggregate"] = distribution(aggregates)
            report["warm_point"] = distribution(points)
            report["warm_sort_page"] = distribution(sorts)

            report["concurrent_load"] = concurrent_probe(
                store,
                documents=synthetic_documents,
                first_row_id=rows + 1000,
                total_rows=rows,
                ui_operations=ui_operations,
                operation_prefix="baseline",
                load_active=load_active,
                first_batch=first_batch,
                timeout=120,
            )
            expected = store.aggregate()
            report["after_load_counts"] = store.logical_counts()
            report["final_aggregates"] = expected
            _, report["export_seconds"] = measured(
                lambda: store.export_project(root / "export")
            )
            report["export_bytes"] = directory_bytes(root / "export")
            with ProjectStore.restore_project(
                root / "export", root / "restored", engine
            ) as restored:
                assert restored.aggregate() == expected, "restored aggregates differ"
                assert restored.logical_counts() == store.logical_counts(), (
                    "restored logical counts differ"
                )
                old = restored.resolve_citation(evidence[0]["citation_id"])
                assert old["text"] == citation["text"], "restored citation drifted"
            report["restored_verified"] = True
            report["terminal_original_export_restore_bytes"] = directory_bytes(root)
        report["closed_project_bytes"] = directory_bytes(path)
        report["process_maxrss_kib"] = resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_maxrss
        report["environment"] = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
        }
        return report


def publish_stress_generation(
    store, *, rows, generation, modulus=1, page_committed=None
):
    after = 0
    published = 0
    started = time.perf_counter()
    while page := store.read_rows(after_row_id=after, limit=500):
        selected = [row for row in page if int(row["row_id"]) % modulus == 0]
        if selected:
            store.publish_extraction(
                f"stress-extract-{generation}-{after}",
                stress_extractions(selected, generation=generation),
            )
            published += len(selected)
            if page_committed:
                page_committed(f"history.generation_{generation}.page_committed")
        after = page[-1]["row_id"]
        if after >= rows:
            break
    return {
        "generation": generation,
        "modulus": modulus,
        "published": published,
        "seconds": time.perf_counter() - started,
    }


def run_stress_engine(engine, rows, parent, ui_operations):
    import sqlite3

    with tempfile.TemporaryDirectory(
        prefix=f"slice-stress-{engine}-", dir=parent
    ) as temporary:
        root = Path(temporary)
        path = root / "project"
        report = {
            "profile": "stress",
            "status": "running",
            "engine": engine,
            "sqlite_version": sqlite3.sqlite_version,
            "duckdb_version": importlib.metadata.version("duckdb"),
            "requested_synthetic_rows": rows,
            "scratch_soft_limit_bytes": 8 * 1024**3,
            "scratch_hard_limit_bytes": 10 * 1024**3,
        }
        sampler = PhaseSampler(root)
        sampler.start()
        load_active = threading.Event()
        first_batch = threading.Event()

        def checkpoint(label):
            if label == "import.batch_committed" and load_active.is_set():
                first_batch.set()
            size = directory_bytes(root)
            if size >= 8 * 1024**3 or sampler.hard_limit.is_set():
                raise ScratchLimit(
                    f"scratch guard stopped {engine} at {size} bytes during {label}"
                )

        try:
            with ProjectStore(path, engine, checkpoint=checkpoint) as store:
                sampler.set_phase("import")
                documents = TimedDocuments(stress_documents(1, rows))
                _, report["bulk_import_seconds"] = measured(
                    lambda: store.import_documents(
                        "stress-import", documents, batch_size=500
                    )
                )
                report["fixture"] = documents.report()

                sampler.set_phase("history")
                report["extraction_generations"] = [
                    publish_stress_generation(
                        store,
                        rows=rows,
                        generation=0,
                        modulus=1,
                        page_committed=checkpoint,
                    ),
                    publish_stress_generation(
                        store,
                        rows=rows,
                        generation=1,
                        modulus=5,
                        page_committed=checkpoint,
                    ),
                    publish_stress_generation(
                        store,
                        rows=rows,
                        generation=2,
                        modulus=20,
                        page_committed=checkpoint,
                    ),
                ]
                report["after_history_counts"] = store.logical_counts()

                sampler.set_phase("queries")
                aggregates, joins = [], []
                join_start = max(1, rows - max(1, rows // 100) + 1)
                report["before_concurrent_aggregates"] = store.aggregate()
                expected_join = store.history_join(
                    row_id_start=join_start, row_id_end=rows
                )
                for _ in range(10):
                    _, duration = measured(store.aggregate)
                    aggregates.append(duration)
                    _, duration = measured(
                        lambda: store.history_join(
                            row_id_start=join_start, row_id_end=rows
                        )
                    )
                    joins.append(duration)
                report["warm_aggregate"] = distribution(aggregates)
                report["selective_history_join"] = {
                    "row_id_start": join_start,
                    "row_id_end": rows,
                    "latency": distribution(joins),
                    "result": expected_join,
                }

                sampler.set_phase("concurrent")
                report["concurrent_load"] = concurrent_probe(
                    store,
                    documents=stress_documents,
                    first_row_id=rows + 1,
                    total_rows=rows,
                    ui_operations=ui_operations,
                    operation_prefix="stress",
                    load_active=load_active,
                    first_batch=first_batch,
                    timeout=300,
                )

                report["final_counts"] = store.logical_counts()
                report["final_aggregates"] = store.aggregate()
                report["before_checkpoint_bytes"] = directory_bytes(path)
                sampler.set_phase("checkpoint")
                _, report["checkpoint_seconds"] = measured(store.checkpoint_storage)
                report["after_checkpoint_bytes"] = directory_bytes(path)
            sampler.set_phase("closed")
            report["closed_project_bytes"] = directory_bytes(path)
            report["status"] = "complete"
        except ScratchLimit as exc:
            report["status"] = "scratch_limit"
            report["error"] = str(exc)
        finally:
            sampler.stop()
        report["phase_peaks"] = sampler.peaks
        report["process_maxrss_kib"] = resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_maxrss
        report["environment"] = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
            "host_note": "Host load is observational only; the runner does not reserve CPUs, memory, or I/O and cannot attribute competing background work.",
        }
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine", choices=("sqlite", "duckdb", "both"), default="both"
    )
    parser.add_argument("--profile", choices=("baseline", "stress"), default="baseline")
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--ui-operations", type=int, default=100)
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="Disposable databases; use a real filesystem for disk measurements.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    max_rows = 500000 if args.profile == "stress" else 100000
    if not 1 <= args.rows <= max_rows or not 1 <= args.ui_operations <= 1000:
        parser.error(
            f"bounded {args.profile} profile: rows 1..{max_rows}; ui-operations 1..1000"
        )
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.engine == "both":
        reports = []
        for engine in ("sqlite", "duckdb"):
            child_output = args.output.with_name(f"{args.output.stem}-{engine}.json")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.storage_slice.benchmark",
                    "--engine",
                    engine,
                    "--profile",
                    args.profile,
                    "--rows",
                    str(args.rows),
                    "--ui-operations",
                    str(args.ui_operations),
                    "--work-dir",
                    str(args.work_dir),
                    "--output",
                    str(child_output),
                ],
                check=True,
                timeout=1800 if args.profile == "stress" else 300,
            )
            reports.append(json.loads(child_output.read_text()))
        complete = all(
            report.get("status", "complete") == "complete" for report in reports
        )
        aggregates_equal = complete and (
            reports[0]["final_aggregates"] == reports[1]["final_aggregates"]
        )
        history_equal = args.profile != "stress" or (
            complete
            and reports[0]["selective_history_join"]["result"]
            == reports[1]["selective_history_join"]["result"]
        )
        if complete:
            assert aggregates_equal, "engine aggregates differ"
            assert history_equal, "engine history joins differ"
        result = {
            "profile": args.profile,
            "reports": reports,
            "cross_engine_final_aggregates_equal": aggregates_equal,
            "cross_engine_history_join_equal": history_equal,
        }
    else:
        runner = run_stress_engine if args.profile == "stress" else run_engine
        result = runner(args.engine, args.rows, args.work_dir, args.ui_operations)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"{args.engine}: wrote {args.output}", flush=True)


if __name__ == "__main__":
    main()

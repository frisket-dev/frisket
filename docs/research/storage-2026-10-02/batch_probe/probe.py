#!/usr/bin/env python3
"""Measure real local whole-column clustering and its terminal payloads."""
from __future__ import annotations
import argparse, gc, json, resource, threading, time
from pathlib import Path
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.actions import _default_map_runner_factory
from frisket.engine.executor.cluster_action import run_typed_cluster_action
from frisket.engine.store import Project
def rss_kib() -> int:
    lines = Path("/proc/self/status").read_text().splitlines()
    return int(next(line for line in lines if line.startswith("VmRSS:")).split()[1])
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    if args.rows <= 0:
        raise SystemExit("--rows must be positive")
    started = time.perf_counter()
    project = Project.create(args.project, name=f"cluster-probe-{args.rows}")
    sheet = project.add_sheet("Names")
    source = project.add_column(sheet, "name", type="text")
    records = [
        {"name": f"{'Entity' if i % 2 == 0 else 'entity'} {i // 2:06d}"}
        for i in range(args.rows)
    ]
    project.add_rows(sheet, records, {"name": source})
    del records
    gc.collect()
    fixture_seconds = time.perf_counter() - started
    stop, peak = threading.Event(), [rss_kib()]
    def sample() -> None:
        while not stop.wait(0.005):
            peak[0] = max(peak[0], rss_kib())
    baseline_rss = rss_kib()
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    registered = ACTION_REGISTRY.get("cluster.values")
    request = ActionRequest(
        action_id="cluster.values",
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        params={"source": "name", "method": "fingerprint"},
        output_names={"canonical": "Reviewed"},
        idempotency_key=f"cluster-probe-{args.rows}",
    )
    action_started = time.perf_counter()
    result = run_typed_cluster_action(
        project, "batch-probe", BoundTypedActionRequest.bind(registered, request),
        None, _default_map_runner_factory,
    )
    action_seconds = time.perf_counter() - action_started
    stop.set()
    sampler.join()
    peak[0] = max(peak[0], rss_kib())
    if result.status != "completed":
        raise RuntimeError(result.model_dump_json())
    body_text = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()["body"]
    body = json.loads(body_text)
    fact = next(x["ref"] for x in body["evidence"] if x["ref"].get("kind") == "value_clusters")
    spec_text = project.db.execute(
        "SELECT ops.spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
        (result.run_id,),
    ).fetchone()["spec"]
    op_spec = json.loads(spec_text)
    op_fact = op_spec.get("value_clusters_result")
    project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dbstat = {
        row["name"]: {"bytes": int(row["bytes"]), "payload": int(row["payload"])}
        for row in project.db.execute(
            "SELECT name,sum(pgsize) bytes,sum(payload) payload FROM dbstat "
            "WHERE name IN ('ops','receipts','results','current_cells') GROUP BY name"
        )
    }
    stats = project.db.execute(
        "SELECT count(*) cells,coalesce(sum(length(CAST(value AS BLOB))),0) value_bytes "
        "FROM results WHERE run_id=?", (result.run_id,),
    ).fetchone()
    size = lambda value: len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
    metrics = {
        "rows": args.rows, "fixture_seconds": fixture_seconds, "action_seconds": action_seconds,
        "rss_before_action_kib": baseline_rss, "rss_sampled_peak_kib": peak[0],
        "rss_sampled_delta_kib": max(0, peak[0] - baseline_rss),
        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "canonical_values_json_bytes": size(fact["canonical_values"]),
        "clusters_json_bytes": size(fact["clusters"]), "fact_json_bytes": size(fact),
        "ops_spec_bytes": len(spec_text.encode()), "receipt_body_bytes": len(body_text.encode()),
        "result_cells": int(stats["cells"]), "result_value_bytes": int(stats["value_bytes"]),
        "model_calls": project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0],
        "op_fact_equals_receipt_fact": op_fact == fact,
        "op_result_storage": op_spec.get("value_clusters_result_storage"), "dbstat": dbstat,
    }
    print(json.dumps(metrics, sort_keys=True))
    project.close()
if __name__ == "__main__":
    main()

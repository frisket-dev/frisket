#!/usr/bin/env python3
"""Evaluate located video-retrieval rankings from the production-backed driver."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def interval(item: dict[str, Any]) -> tuple[float, float]:
    start = float(item["start"])
    end = float(item.get("end", start))
    if not math.isfinite(start) or not math.isfinite(end) or end < start:
        raise ValueError(f"invalid result interval: {item!r}")
    return start, end


def overlaps(item: dict[str, Any], target: dict[str, Any]) -> bool:
    if item["source_id"] != target["source_id"]:
        return False
    start, end = interval(item)
    target_start, target_end = float(target["start"]), float(target["end"])
    if start == end:
        return target_start <= start <= target_end
    return min(end, target_end) > max(start, target_start)


def temporal_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    if left["source_id"] != right["source_id"]:
        return 0.0
    left_start, left_end = interval(left)
    right_start, right_end = interval(right)
    intersection = max(0.0, min(left_end, right_end) - max(left_start, right_start))
    union = max(left_end, right_end) - min(left_start, right_start)
    return intersection / union if union > 0 else float(left_start == right_start)


def relevant(item: dict[str, Any], targets: list[dict[str, Any]]) -> bool:
    return any(overlaps(item, target) for target in targets)


def seek_error(item: dict[str, Any], targets: list[dict[str, Any]]) -> float:
    start, _ = interval(item)
    matching = [
        target for target in targets if target["source_id"] == item["source_id"]
    ]
    if not matching:
        return math.inf
    return min(
        abs(start - float(target.get("seek", target["start"]))) for target in matching
    )


def duplicate_rate(items: list[dict[str, Any]]) -> float:
    if len(items) < 2:
        return 0.0
    duplicates = 0
    for index, item in enumerate(items[1:], start=1):
        # Any repeated timeline coverage is duplicate exposure to a user. A
        # 20% recipe overlap has a low interval IoU (1/9), so a 0.5 cutoff
        # would incorrectly report no duplication for adjacent fixed clips.
        if any(temporal_iou(item, earlier) > 0.0 for earlier in items[:index]):
            duplicates += 1
    return duplicates / (len(items) - 1)


def target_coverage(
    items: list[dict[str, Any]], targets: list[dict[str, Any]]
) -> float:
    if not targets:
        return 0.0
    hit_count = sum(any(overlaps(item, target) for item in items) for target in targets)
    return hit_count / len(targets)


def evaluate_arm(
    arm: dict[str, Any], queries: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    query_rows: list[dict[str, Any]] = []
    for result in arm["query_results"]:
        query = queries[result["query_id"]]
        if query["family"] != arm["family"]:
            raise ValueError(
                f"query {query['id']} family {query['family']} does not match arm {arm['id']}"
            )
        ranking = result["ranking"]
        top_one = ranking[:1]
        top_three = ranking[:3]
        targets = query["targets"]
        first = top_one[0] if top_one else None
        first_relevant = bool(first and relevant(first, targets))
        first_seek_error = seek_error(first, targets) if first_relevant else math.inf
        query_rows.append(
            {
                "query_id": query["id"],
                "kind": query["kind"],
                "recall_at_1": first_relevant,
                "recall_at_3": any(relevant(item, targets) for item in top_three),
                "useful_seek_at_1": first_relevant and first_seek_error <= 5.0,
                "seek_error_seconds": first_seek_error
                if math.isfinite(first_seek_error)
                else None,
                "duplicate_rate_at_3": duplicate_rate(top_three),
                "relevant_hits_at_3": sum(
                    relevant(item, targets) for item in top_three
                ),
                "target_coverage_at_3": target_coverage(top_three, targets),
                "top_results": top_three,
            }
        )

    count = len(query_rows)
    finite_seek_errors = [
        row["seek_error_seconds"]
        for row in query_rows
        if row["seek_error_seconds"] is not None
    ]
    aggregate = {
        "query_count": count,
        "recall_at_1": sum(row["recall_at_1"] for row in query_rows) / count
        if count
        else 0.0,
        "recall_at_3": sum(row["recall_at_3"] for row in query_rows) / count
        if count
        else 0.0,
        "useful_seek_at_1": (
            sum(row["useful_seek_at_1"] for row in query_rows) / count if count else 0.0
        ),
        "median_seek_error_seconds": (
            statistics.median(finite_seek_errors) if finite_seek_errors else None
        ),
        "mean_duplicate_rate_at_3": (
            statistics.fmean(row["duplicate_rate_at_3"] for row in query_rows)
            if count
            else 0.0
        ),
        "mean_relevant_hits_at_3": (
            statistics.fmean(row["relevant_hits_at_3"] for row in query_rows)
            if count
            else 0.0
        ),
        "mean_target_coverage_at_3": (
            statistics.fmean(row["target_coverage_at_3"] for row in query_rows)
            if count
            else 0.0
        ),
    }
    return {
        "id": arm["id"],
        "family": arm["family"],
        "strategy": arm["strategy"],
        "aggregate": aggregate,
        "queries": query_rows,
        "cost": arm["cost"],
    }


def main() -> None:
    args = parse_args()
    corpus = load_json(args.corpus)
    run = load_json(args.run)
    if corpus.get("schema_version") != "video_retrieval_corpus.v1":
        raise ValueError("unsupported corpus schema")
    if run.get("schema_version") != "video_retrieval_run.v1":
        raise ValueError("unsupported run schema")
    queries = {query["id"]: query for query in corpus["queries"]}
    arms = [evaluate_arm(arm, queries) for arm in run["arms"]]
    output = {
        "schema_version": "video_retrieval_metrics.v1",
        "run_id": run["run_id"],
        "model": corpus["model"],
        "arms": arms,
    }
    serialized = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")


if __name__ == "__main__":
    main()

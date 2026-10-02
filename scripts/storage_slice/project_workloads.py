"""Bounded public/runtime query workloads for the actual-Project experiment."""

from __future__ import annotations

import json
import heapq
import math
import statistics
import time
from typing import Any, Callable

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.querysets import resolve_sheet_filter_rows
from frisket.search import search_cells_scoped, search_project_page, search_sheet
from frisket.server.run_payloads import history_page_payload
from frisket.server.services.project_qa_analytics import evaluate_analytics

from .fixtures import (
    STRESS_BOUNDED_SEARCH_ROWS,
    STRESS_BOUNDED_SEARCH_TOKEN,
    stress_project_marker,
    stress_project_amount,
    stress_project_category,
    stress_project_records,
    stress_project_search_tokens,
)


def sample_distribution(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    result: dict[str, float | int] = {
        "samples": len(samples),
        "first_ms": samples[0] * 1000,
        "median_ms": statistics.median(ordered) * 1000,
        "max_ms": ordered[-1] * 1000,
    }
    if len(samples) >= 20:
        result["p95_ms"] = ordered[math.ceil(0.95 * len(ordered)) - 1] * 1000
    return result


def _timed_samples(call: Callable[[], Any], count: int) -> tuple[Any, dict]:
    call()  # discarded warm-up; the next call is merely the first observation
    samples = []
    value = None
    for _ in range(count):
        started = time.perf_counter()
        value = call()
        samples.append(time.perf_counter() - started)
    return value, sample_distribution(samples)


def _record_row_ids(project, column_id: int, numbers: list[int]) -> dict[int, int]:
    placeholders = ",".join("?" for _ in numbers)
    rows = project.db.execute(
        "SELECT row_id,json_extract(value,'$') AS record_id FROM current_cells "
        f"WHERE column_id=? AND json_extract(value,'$') IN ({placeholders})",
        (column_id, *numbers),
    ).fetchall()
    result = {int(row["record_id"]): int(row["row_id"]) for row in rows}
    assert set(result) == set(numbers)
    return result


def run_search_workload(
    project,
    sheet_id: int,
    columns: dict[str, int],
    rows: int,
    governed: Callable[[Callable[[], Any]], Any],
) -> dict[str, Any]:
    unique_number = 199
    bounded_numbers = [
        number for number in STRESS_BOUNDED_SEARCH_ROWS if number <= rows
    ]
    numbers = sorted({unique_number, *bounded_numbers})
    row_ids = _record_row_ids(project, columns["record_id"], numbers)
    tokens = stress_project_search_tokens(unique_number)
    shapes: dict[str, tuple[str, set[int]]] = {
        f"unique_{position}": (token, {row_ids[unique_number]})
        for position, token in tokens.items()
    }
    shapes["bounded_membership"] = (
        STRESS_BOUNDED_SEARCH_TOKEN,
        {row_ids[number] for number in bounded_numbers},
    )
    report: dict[str, Any] = {}
    for name, (token, expected) in shapes.items():
        snippet_bytes: list[int] = []

        def search():
            result = governed(
                lambda: search_project_page(
                    project, token, limit=max(10, len(expected)), rerank="off"
                )
            )
            assert result["complete"]
            assert {int(hit["row_id"]) for hit in result["hits"]} == expected
            assert all(token in str(hit["snip"]) for hit in result["hits"])
            size = sum(len(str(hit["snip"]).encode()) for hit in result["hits"])
            assert size < 4096 * max(1, len(result["hits"]))
            snippet_bytes.append(size)
            return result

        result, timings = _timed_samples(search, 20)
        report[name] = {
            **timings,
            "query": token,
            "expected_row_ids": sorted(expected),
            "hit_count": len(result["hits"]),
            "snippet_bytes_first": snippet_bytes[1],
            "snippet_bytes_max": max(snippet_bytes[1:]),
        }

    late = tokens["late"]
    assert search_sheet(project, sheet_id, late, limit=10) == [row_ids[unique_number]]
    scoped = search_cells_scoped(
        project,
        sheet_id,
        late,
        [row_ids[unique_number]],
        {(row_ids[unique_number], columns["body"])},
        limit=10,
    )
    assert len(scoped) == 1 and int(scoped[0]["row_id"]) == row_ids[unique_number]
    assert late in str(scoped[0]["snip"])
    report["scopes"] = {
        "sheet_row_ids": [row_ids[unique_number]],
        "exact_cell": [row_ids[unique_number], columns["body"]],
    }
    return report


def _projected_page(project, sheet_id: int, columns: dict[str, int], **kwargs):
    rowset = resolve_sheet_filter_rows(project, sheet_id, limit=50, **kwargs)
    projected = {
        name: project.get_values(sheet_id, column_id, rowset.row_ids)
        for name, column_id in columns.items()
        if name != "body"
    }
    return rowset, projected


def probe_current_pages(
    project, sheet_id: int, columns: dict[str, int], rows: int
) -> dict[str, Any]:
    offsets = {"first": 0, "middle": max(0, rows // 2 - 25), "final": max(0, rows - 50)}
    report = {}
    for name, offset in offsets.items():
        started = time.perf_counter()
        rowset, projected = _projected_page(project, sheet_id, columns, offset=offset)
        record_ids = [int(projected["record_id"][row_id]) for row_id in rowset.row_ids]
        assert record_ids == list(range(offset + 1, min(rows, offset + 50) + 1))
        report[name] = {
            "seconds": time.perf_counter() - started,
            "offset": offset,
            "returned": len(rowset.row_ids),
            "projected_columns": len(projected),
        }
    return report


def run_grid_workload(
    project,
    sheet_id: int,
    columns: dict[str, int],
    expected: dict[str, Any],
    governed: Callable[[Callable[[], Any]], Any],
    *,
    samples: int = 20,
) -> dict[str, Any]:
    rows = int(expected["rows"])
    middle = max(0, rows // 2 - 25)
    final = max(0, rows - 50)
    narrow_start = rows - max(1, math.ceil(rows * 0.001)) + 1
    shapes = {
        "amount_asc": {
            "sort": json.dumps([{"column": "amount", "dir": "asc"}]),
            "expected": expected["amount_asc_record_ids"],
        },
        "amount_desc": {
            "sort": json.dumps([{"column": "amount", "dir": "desc"}]),
            "expected": expected["amount_desc_record_ids"],
        },
        "published_at_asc": {
            "sort": json.dumps([{"column": "published_at", "dir": "asc"}]),
            "expected": expected["date_asc_record_ids"],
        },
        "filter_0_1_percent": {
            "filter_": json.dumps({"record_id": {"gte": str(narrow_start)}}),
            "expected": list(range(narrow_start, min(rows, narrow_start + 49) + 1)),
            "total": rows - narrow_start + 1,
        },
        "filter_10_percent": {
            "filter_": json.dumps({"category": {"eq": "environment"}}),
            "expected": expected["environment_first_record_ids"],
            "total": expected["categories"]["environment"],
        },
        "page_first": {"offset": 0, "expected": list(range(1, min(rows, 50) + 1))},
        "page_middle": {
            "offset": middle,
            "expected": list(range(middle + 1, min(rows, middle + 50) + 1)),
        },
        "page_final": {
            "offset": final,
            "expected": list(range(final + 1, rows + 1)),
        },
    }
    report: dict[str, Any] = {}
    for name, spec in shapes.items():
        kwargs = {
            key: value
            for key, value in spec.items()
            if key not in {"expected", "total"}
        }

        def query():
            rowset, projected = governed(
                lambda: _projected_page(project, sheet_id, columns, **kwargs)
            )
            record_ids = [
                int(projected["record_id"][row_id]) for row_id in rowset.row_ids
            ]
            assert record_ids == spec["expected"]
            if "total" in spec:
                assert rowset.total == spec["total"]
            return rowset

        rowset, timings = _timed_samples(query, samples)
        report[name] = {
            **timings,
            "returned": len(rowset.row_ids),
            "total": rowset.total,
        }
    return report


def _assert_metric(actual: Any, expected: Any) -> None:
    if isinstance(expected, float):
        assert math.isclose(float(actual), expected, rel_tol=1e-12, abs_tol=1e-9)
    else:
        assert actual == expected


def run_analytics_workload(
    project,
    sheet_id: int,
    columns: dict[str, int],
    expected: dict[str, Any],
    governed: Callable[[Callable[[], Any]], Any],
    *,
    samples: int = 10,
    cancel_event=None,
) -> dict[str, Any]:
    metrics = [
        {"id": "rows", "kind": "count"},
        {"id": "values", "kind": "value_count", "column_id": columns["amount"]},
        {"id": "missing", "kind": "missing_count", "column_id": columns["amount"]},
        {"id": "sum", "kind": "sum", "column_id": columns["amount"]},
        {"id": "mean", "kind": "mean", "column_id": columns["amount"]},
        {"id": "median", "kind": "median", "column_id": columns["amount"]},
    ]
    variants = {
        "broad_grouped": {
            "groups": [{"column_id": columns["category"]}],
            "sort": [{"kind": "group", "group_index": 0, "direction": "asc"}],
            "limit": 20,
        },
        "filtered_10_percent": {"filter": {"category": {"eq": "environment"}}},
    }
    report: dict[str, Any] = {}
    for name, extra in variants.items():
        request = {"sheet_id": sheet_id, "metrics": metrics, **extra}

        def query():
            return governed(
                lambda: evaluate_analytics(
                    project,
                    request,
                    {"kind": "sheet", "sheet_id": sheet_id},
                    cancel_event=cancel_event,
                )
            )

        result, timings = _timed_samples(query, samples)
        groups = result["groups"]
        if name == "broad_grouped":
            actual = {group["group"][0]["value"]: group for group in groups}
            assert set(actual) == set(expected["aggregates"])
            checks = actual.items()
        else:
            assert len(groups) == 1
            checks = [("environment", groups[0])]
        for category, group in checks:
            facts = expected["aggregates"][category]
            wanted = {
                "rows": facts["rows"],
                "values": facts["valid"],
                "missing": facts["missing"],
                "sum": facts["sum"],
                "mean": facts["mean"],
                "median": facts["median"],
            }
            for metric, value in wanted.items():
                _assert_metric(group["metrics"][metric], value)
            quality = group.get("quality", result.get("quality", {}))
            assert quality[str(columns["amount"])] == {
                "column_id": columns["amount"],
                "present": facts["valid"],
                "missing": facts["missing"],
                "invalid": facts["invalid"],
            }
        report[name] = {
            **timings,
            "row_count": result["row_count"],
            "groups": len(groups),
        }
    return report


def _expected_record(row_number: int) -> dict:
    return next(stress_project_records(row_number, 1))


def _action(project, action_id: str, params: dict, key: str):
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


def assert_edit_values(
    project: Project,
    sheet_id: int,
    edits: list[dict[str, Any]],
    *,
    edited: bool,
) -> None:
    by_column: dict[int, list[dict[str, Any]]] = {}
    for edit in edits:
        by_column.setdefault(int(edit["column_id"]), []).append(edit)
    for column_id, column_edits in by_column.items():
        row_ids = [int(edit["row_id"]) for edit in column_edits]
        values, refs = project.get_values_with_refs(
            sheet_id,
            column_id,
            row_ids,
            preserve_invalid=True,
            include_validity=True,
        )
        for edit in column_edits:
            row_id = int(edit["row_id"])
            expected = edit["value"] if edited else edit["before"]
            assert values.get(row_id) == expected
            if edited:
                assert refs[row_id]["kind"] == "manual_edit"
                assert int(refs[row_id]["op_id"]) == int(edit["op_id"])
            elif expected is not None:
                assert refs[row_id]["kind"] == "source_cell"


def _timed_search(project: Project, token: str, expected_row_id: int) -> dict[str, Any]:
    started = time.perf_counter()
    result = search_project_page(project, token, limit=10, rerank="off")
    seconds = time.perf_counter() - started
    assert result["complete"]
    assert {int(hit["row_id"]) for hit in result["hits"]} == {expected_row_id}
    assert all(token in str(hit["snip"]) for hit in result["hits"])
    return {
        "seconds": seconds,
        "hits": len(result["hits"]),
        "snippet_bytes": sum(len(str(hit["snip"]).encode()) for hit in result["hits"]),
    }


def run_mutation_workload(
    project: Project,
    sheet_id: int,
    columns: dict[str, int],
    rows: int,
    drain_index: Callable[[Any], dict[str, Any]],
    guard: Callable[[], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ordinary_count = 200 if rows >= 2_000 else 5
    batch_count = 1_000 if rows >= 2_000 else 20
    ordinary_space = rows - batch_count
    ordinary_edits: list[dict[str, Any]] = []
    action_seconds: list[float] = []
    receipt_by_op: dict[int, str] = {}
    for index in range(ordinary_count):
        number = 1 + (index * 1499) % ordinary_space
        row_id = int(
            project.db.execute(
                "SELECT row_id FROM current_cells WHERE column_id=? AND value=json(?)",
                (columns["record_id"], number),
            ).fetchone()[0]
        )
        source = _expected_record(number)
        if index % 3 == 0:
            name = "body"
            value = f"{source[name]}\nfrisketordinary{index:04d}"
        elif index % 3 == 1:
            name = "amount"
            value = 20_000_000 + index
        else:
            name = "status"
            value = f"qualified-{index % 7}"
        started = time.perf_counter()
        result = _action(
            project,
            "cell.edit",
            {"edits": [{"row_id": row_id, "column_id": columns[name], "value": value}]},
            f"qualification-ordinary-{index:04d}",
        )
        action_seconds.append(time.perf_counter() - started)
        assert len(result.op_ids) == 1 and result.receipt_id is not None
        receipt_by_op[result.op_ids[0]] = result.receipt_id
        ordinary_edits.append(
            {
                "number": number,
                "row_id": row_id,
                "column_id": columns[name],
                "name": name,
                "before": source[name],
                "value": value,
                "op_id": result.op_ids[0],
            }
        )
        guard()
    assert_edit_values(project, sheet_id, ordinary_edits, edited=True)
    ordinary_index = drain_index(project)
    first_body = next(
        edit for edit in ordinary_edits if edit["column_id"] == columns["body"]
    )

    ordinary_search = _timed_search(
        project,
        f"frisketordinary{ordinary_edits.index(first_body):04d}",
        int(first_body["row_id"]),
    )

    batch_edits: list[dict[str, Any]] = []
    marker_number = max(
        number
        for number in range(rows - batch_count + 1, rows + 1)
        if number % 200 == 199
    )
    old_marker = stress_project_marker(marker_number)
    new_marker = f"frisketbatchmarker{marker_number:08d}"
    for number in range(rows - batch_count + 1, rows + 1):
        row_id = int(
            project.db.execute(
                "SELECT row_id FROM current_cells WHERE column_id=? AND value=json(?)",
                (columns["record_id"], number),
            ).fetchone()[0]
        )
        before = _expected_record(number)["body"]
        value = f"{before}\nfrisketbatch{number:08d}"
        if number == marker_number:
            value = value.replace(old_marker, new_marker)
        batch_edits.append(
            {
                "number": number,
                "row_id": row_id,
                "column_id": columns["body"],
                "before": before,
                "value": value,
            }
        )
    started = time.perf_counter()
    batch_result = _action(
        project,
        "cell.edit",
        {
            "edits": [
                {key: edit[key] for key in ("row_id", "column_id", "value")}
                for edit in batch_edits
            ]
        },
        "qualification-batch-1000",
    )
    batch_action_seconds = time.perf_counter() - started
    assert len(batch_result.op_ids) == 1 and batch_result.receipt_id is not None
    batch_op_id = batch_result.op_ids[0]
    receipt_by_op[batch_op_id] = batch_result.receipt_id
    for edit in batch_edits:
        edit["op_id"] = batch_op_id
    assert_edit_values(project, sheet_id, batch_edits, edited=True)
    batch_index = drain_index(project)
    assert batch_index["processed_units"] == batch_count
    assert not search_project_page(project, old_marker, rerank="off")["hits"]
    after_edit_search = _timed_search(
        project,
        new_marker,
        next(
            int(edit["row_id"])
            for edit in batch_edits
            if edit["number"] == marker_number
        ),
    )

    started = time.perf_counter()
    undo = _action(
        project,
        "operation.undo",
        {"expected_op_id": batch_op_id},
        "qualification-batch-undo",
    )
    undo_action_seconds = time.perf_counter() - started
    assert undo.op_ids == [batch_op_id]
    assert_edit_values(project, sheet_id, batch_edits, edited=False)
    undo_index = drain_index(project)
    assert undo_index["processed_units"] == batch_count
    after_undo_search = _timed_search(
        project,
        old_marker,
        next(
            int(edit["row_id"])
            for edit in batch_edits
            if edit["number"] == marker_number
        ),
    )
    assert not search_project_page(project, new_marker, rerank="off")["hits"]

    started = time.perf_counter()
    redo = _action(
        project,
        "operation.redo",
        {"expected_op_id": batch_op_id},
        "qualification-batch-redo",
    )
    redo_action_seconds = time.perf_counter() - started
    assert redo.op_ids == [batch_op_id]
    assert_edit_values(project, sheet_id, batch_edits, edited=True)
    redo_index = drain_index(project)
    assert redo_index["processed_units"] == batch_count
    after_redo_search = _timed_search(
        project,
        new_marker,
        next(
            int(edit["row_id"])
            for edit in batch_edits
            if edit["number"] == marker_number
        ),
    )
    assert not search_project_page(project, old_marker, rerank="off")["hits"]
    return (
        {
            "projection_timing": "included in action_seconds; the public action boundary does not expose a separate projection timer",
            "ordinary": {
                "count": ordinary_count,
                "action": sample_distribution(action_seconds),
                "index": ordinary_index,
                "search": ordinary_search,
            },
            "batch": {
                "count": batch_count,
                "action_seconds": batch_action_seconds,
                "index": batch_index,
                "search": after_edit_search,
            },
            "undo": {
                "action_seconds": undo_action_seconds,
                "index": undo_index,
                "search": after_undo_search,
            },
            "redo": {
                "action_seconds": redo_action_seconds,
                "index": redo_index,
                "search": after_redo_search,
            },
            "batch_op_id": batch_op_id,
            "marker_number": marker_number,
            "old_marker": old_marker,
            "new_marker": new_marker,
            "transition_receipt_ids": [undo.receipt_id, redo.receipt_id],
        },
        {
            "receipt_by_op": receipt_by_op,
            "ordinary_edits": ordinary_edits,
            "batch_edits": batch_edits,
            "transition_receipt_ids": [undo.receipt_id, redo.receipt_id],
        },
    )


def apply_expected_amount_edits(
    expected: dict[str, Any], edits: list[dict[str, Any]]
) -> None:
    overrides: dict[int, int] = {}
    for edit in edits:
        if edit.get("name") != "amount":
            continue
        number = int(edit["number"])
        category = stress_project_category(number)
        facts = expected["aggregates"][category]
        values = expected["amount_values_by_category"][category]
        before = stress_project_amount(number)
        if before is None:
            facts["missing"] -= 1
        elif before == "not-stated":
            facts["invalid"] -= 1
        else:
            before = int(before)
            facts["valid"] -= 1
            facts["sum"] -= before
            values.remove(before)
        value = int(edit["value"])
        facts["valid"] += 1
        facts["sum"] += value
        values.append(value)
        facts["mean"] = statistics.mean(values)
        facts["median"] = statistics.median(values)
        overrides[number] = value

    smallest: list[tuple[int, int, int]] = []
    largest: list[tuple[int, int, int]] = []
    for number in range(1, int(expected["rows"]) + 1):
        amount = overrides.get(number, stress_project_amount(number))
        if not isinstance(amount, int):
            continue
        heapq.heappush(largest, (amount, -number, number))
        if len(largest) > 50:
            heapq.heappop(largest)
        heapq.heappush(smallest, (-amount, -number, number))
        if len(smallest) > 50:
            heapq.heappop(smallest)
    expected["amount_desc_record_ids"] = [
        item[2] for item in sorted(largest, key=lambda item: (-item[0], item[2]))
    ]
    expected["amount_asc_record_ids"] = [
        item[2] for item in sorted(smallest, key=lambda item: (-item[0], item[2]))
    ]


def verify_history(project: Project, mutation_state: dict[str, Any]) -> dict[str, Any]:
    receipt_by_op = mutation_state["receipt_by_op"]
    expected_total = 1 + len(receipt_by_op)
    actual_total = project.history_total()
    assert actual_total == expected_total
    offsets = {
        "start": 0,
        "edit_region": max(0, expected_total - 125),
        "final": max(0, expected_total - 50),
    }
    pages = {
        name: history_page_payload(project, offset=offset, limit=50)
        for name, offset in offsets.items()
    }
    for name, page in pages.items():
        assert page["total"] == expected_total
        assert page["offset"] == offsets[name]
        assert all(item["label"] for item in page["ops"])
    batch_op_id = int(mutation_state["batch_edits"][0]["op_id"])
    final_ops = {int(item["id"]): item for item in pages["final"]["ops"]}
    assert final_ops[batch_op_id]["status"] == "applied"
    assert pages["final"]["cursor_op"]["id"] == batch_op_id
    checked_receipts = []
    op_ids = sorted(receipt_by_op)
    for op_id in {op_ids[0], op_ids[len(op_ids) // 2], op_ids[-1]}:
        receipt_id = receipt_by_op[op_id]
        stored = ReceiptStore(project).find_by_id(receipt_id)
        assert stored is not None and stored.status == "completed"
        assert stored.parsed().op_ids == [op_id]
        checked_receipts.append(receipt_id)
    for receipt_id in mutation_state["transition_receipt_ids"]:
        stored = ReceiptStore(project).find_by_id(receipt_id)
        assert stored is not None and stored.status == "completed"
        assert stored.parsed().op_ids == [batch_op_id]
        checked_receipts.append(receipt_id)
    return {
        "total": expected_total,
        "cursor_index": pages["final"]["cursor_index"],
        "pages": {
            name: {
                "offset": page["offset"],
                "returned": len(page["ops"]),
                "first_op_id": page["ops"][0]["id"] if page["ops"] else None,
                "last_op_id": page["ops"][-1]["id"] if page["ops"] else None,
            }
            for name, page in pages.items()
        },
        "checked_receipt_ids": checked_receipts,
    }

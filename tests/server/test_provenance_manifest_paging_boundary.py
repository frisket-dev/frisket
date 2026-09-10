from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store.runs import RunResultStore


# mock.ts (?mock=1 adapter) retired in onboard-sample-project-v1


def _assert_no_recipe_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in {"recipe", "recipe_version"}
            _assert_no_recipe_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_recipe_keys(child)


def _seed_provenance_history(
    tmp_path: Path,
) -> tuple[TestClient, str, list[int], list[str]]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Provenance paging"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("stories")
    project.add_column(sheet_id, "story")
    base = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    run_ids: list[int] = []
    receipt_ids: list[str] = []
    for index in range(64):
        op_id = project.append_op(
            "map",
            {"action_kind": "map.classify", "seed": index},
            label=f"classify {index}",
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.classify",
            model=f"gemini/model-{index % 4}",
            params={"fields": [{"name": "beat", "type": "category"}]},
            total_rows=10 + index,
        )
        stamp = (base + timedelta(minutes=index)).isoformat()
        project.db.execute(
            "UPDATE runs SET status='completed', completed_rows=?, failed_rows=?, "
            "cost_actual=?, started_at=?, finished_at=? WHERE id=?",
            (
                10 + index,
                index % 3,
                round(index / 100, 4),
                stamp,
                stamp,
                run_id,
            ),
        )
        receipt_id = f"receipt_prov_{index:03d}"
        project.db.execute(
            "INSERT INTO receipts (id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                run_id,
                "map.classify",
                f"action-{index}",
                f"provenance-paging@sha256:{index:03d}",
                f"sha256:{index:03d}",
                "completed",
                json.dumps({"schema_version": "frisket.receipt.v1"}),
                stamp,
            ),
        )
        run_ids.append(run_id)
        receipt_ids.append(receipt_id)
    project.db.commit()
    return client, project_id, run_ids, receipt_ids


def test_provenance_manifest_returns_bounded_run_and_receipt_pages(
    tmp_path: Path,
) -> None:
    client, project_id, run_ids, receipt_ids = _seed_provenance_history(tmp_path)

    response = client.get(
        f"/api/projects/{project_id}/provenance",
        params={
            "runs_offset": 10,
            "runs_limit": 7,
            "receipts_offset": 20,
            "receipts_limit": 5,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_no_recipe_keys(body)
    assert body["project_id"] == project_id
    assert body["models"]
    assert body["providers"] == ["gemini"]
    assert body["action_kinds"][0]["action_kind"] == "map.classify"
    assert body["action_kinds"][0]["action_name"] == "Classify rows"
    # Every seeded run has a known cost: the honesty flag stays down.
    assert body["has_unknown_costs"] is False
    assert body["unknown_cost_runs"] == 0

    newest_runs = list(reversed(run_ids))
    newest_receipts = list(reversed(receipt_ids))
    assert body["runs_page"] == {
        "schema_version": "frisket.provenance_runs_page.v1",
        "order": "desc",
        "offset": 10,
        "limit": 7,
        "total": len(run_ids),
        "has_more": True,
        "next_offset": 17,
    }
    assert [run["run_id"] for run in body["runs"]] == newest_runs[10:17]
    assert body["receipts_page"] == {
        "schema_version": "frisket.provenance_receipts_page.v1",
        "order": "desc",
        "offset": 20,
        "limit": 5,
        "total": len(receipt_ids),
        "has_more": True,
        "next_offset": 25,
    }
    assert [receipt["receipt_id"] for receipt in body["receipts"]] == newest_receipts[
        20:25
    ]

    tail = client.get(
        f"/api/projects/{project_id}/provenance",
        params={
            "runs_offset": 63,
            "runs_limit": 10,
            "receipts_offset": 63,
            "receipts_limit": 10,
        },
    )
    assert tail.status_code == 200, tail.text
    tail_body = tail.json()
    assert tail_body["runs_page"]["has_more"] is False
    assert tail_body["runs_page"]["next_offset"] is None
    assert [run["run_id"] for run in tail_body["runs"]] == newest_runs[63:]
    assert tail_body["receipts_page"]["has_more"] is False
    assert tail_body["receipts_page"]["next_offset"] is None
    assert [
        receipt["receipt_id"] for receipt in tail_body["receipts"]
    ] == newest_receipts[63:]

    for params in (
        {"runs_offset": -1},
        {"runs_limit": 0},
        {"runs_limit": 101},
        {"receipts_offset": -1},
        {"receipts_limit": 0},
        {"receipts_limit": 101},
    ):
        invalid = client.get(f"/api/projects/{project_id}/provenance", params=params)
        assert invalid.status_code == 422


def test_provenance_manifest_reports_unknown_costs_honestly(tmp_path: Path) -> None:
    """A NULL cost_actual crosses the wire as null and raises the flag.

    Totals stay sums of the known values only — the flag and the excluded-run
    count carry the honesty; nothing fabricates a $0.
    """
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Provenance unknown cost"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("stories")
    project.add_column(sheet_id, "story")
    base = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    costs = [1.25, None, 0.0]  # known, unknown, known-free
    for index, cost in enumerate(costs):
        op_id = project.append_op(
            "map",
            {"action_kind": "map.classify", "seed": index},
            label=f"classify {index}",
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.classify",
            model="gemini/model-a",
            params={"fields": [{"name": "beat", "type": "category"}]},
            total_rows=5,
        )
        stamp = (base + timedelta(minutes=index)).isoformat()
        project.db.execute(
            "UPDATE runs SET status='completed', completed_rows=5, failed_rows=0, "
            "cost_actual=?, started_at=?, finished_at=? WHERE id=?",
            (cost, stamp, stamp, run_id),
        )
    project.db.commit()

    body = client.get(f"/api/projects/{project_id}/provenance").json()

    # Per-run cost is null-preserving on the wire (newest first).
    assert [run["cost"] for run in body["runs"]] == [0.0, None, 1.25]
    assert body["has_unknown_costs"] is True
    assert body["unknown_cost_runs"] == 1
    # Totals sum only the known values; the NULL run is excluded, not zeroed.
    assert body["total_cost"] == 1.25
    assert [model["cost"] for model in body["models"]] == [1.25]
    assert [kind["cost"] for kind in body["action_kinds"]] == [1.25]

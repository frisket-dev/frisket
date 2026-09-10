from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.sources import SourceStore
from helpers import write_claimed_test_results


def _seed_offset_pages(tmp_path: Path) -> tuple[TestClient, str, int, int, int]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Offset page envelope"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("stories")
    story_col = project.add_column(sheet_id, "story")
    topic_col = project.add_column(sheet_id, "topic", ai_generated=True)
    row_ids = project.add_rows(
        sheet_id,
        [{"story": f"story {index}"} for index in range(5)],
        {"story": story_col},
    )
    run_ids: list[int] = []
    for index in range(5):
        op_id = project.append_op(
            "map",
            {"action_kind": "map.classify", "index": index},
            label=f"classify {index}",
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.classify",
            model="anthropic/test",
            params={"fields": [{"name": "topic"}]},
            total_rows=len(row_ids),
            row_ids=row_ids,
        )
        write_claimed_test_results(
            project,
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": topic_col,
                    "value": f"topic-{index}-{position}",
                    "confidence": 0.4 + index / 100,
                    "justification": f"review {index}-{position}",
                }
                for position, row_id in enumerate(row_ids)
            ],
        )
        RunResultStore(project).finish_run(run_id)
        RunResultStore(project).point_column_at_run(op_id, topic_col, run_id)
        project.db.execute(
            "INSERT INTO receipts (id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                f"receipt_offset_{index}",
                run_id,
                "map.classify",
                f"action-{index}",
                f"offset-page@sha256:{index}",
                f"sha256:{index}",
                "completed",
                json.dumps({"schema_version": "frisket.receipt.v1"}),
            ),
        )
        run_ids.append(run_id)
    source_id = SourceStore(project).add_source(
        "Offset feed",
        kind="rss",
        url="https://example.test/feed.xml",
        schedule="@hourly",
    )
    for index in range(5):
        SourceStore(project).record_source_run(source_id, status="ok", new_rows=index)
    project.db.commit()
    return client, project_id, run_ids[-1], topic_col, source_id


def _assert_forward_page(
    page: dict[str, Any],
    *,
    offset: int,
    limit: int,
    total: int,
    item_count: int,
) -> None:
    next_offset = offset + item_count
    assert page["offset"] == offset
    assert page["limit"] == limit
    assert page["total"] == total
    assert page["has_more"] is (next_offset < total)
    assert page["next_offset"] == (next_offset if next_offset < total else None)


def test_offset_page_payload_helper_uses_returned_item_count() -> None:
    from frisket.server.paging import offset_page_payload

    page = offset_page_payload(
        "frisket.test_page.v1",
        items=[{"id": 10}, {"id": 11}, {"id": 12}],
        item_key="rows",
        offset=10,
        limit=25,
        total=20,
        order="desc",
    )

    assert page == {
        "schema_version": "frisket.test_page.v1",
        "order": "desc",
        "offset": 10,
        "limit": 25,
        "total": 20,
        "has_more": True,
        "next_offset": 13,
        "rows": [{"id": 10}, {"id": 11}, {"id": 12}],
    }


def test_offset_page_routes_share_metadata_and_validate_params(tmp_path: Path) -> None:
    client, project_id, run_id, column_id, source_id = _seed_offset_pages(tmp_path)

    run_rows = client.get(
        f"/api/projects/{project_id}/actions/runs/{run_id}/rows",
        params={"offset": 0, "limit": 2},
    )
    assert run_rows.status_code == 200, run_rows.text
    run_rows_body = run_rows.json()
    _assert_forward_page(
        run_rows_body,
        offset=0,
        limit=2,
        total=5,
        item_count=len(run_rows_body["rows"]),
    )

    column_runs = client.get(
        f"/api/projects/{project_id}/columns/{column_id}/runs",
        params={"offset": 1, "limit": 2},
    )
    assert column_runs.status_code == 200, column_runs.text
    column_runs_body = column_runs.json()
    _assert_forward_page(
        column_runs_body,
        offset=1,
        limit=2,
        total=5,
        item_count=len(column_runs_body["runs"]),
    )

    review_bundles = client.get(
        f"/api/projects/{project_id}/review/bundles",
        params={"offset": 3, "limit": 2},
    )
    assert review_bundles.status_code == 200, review_bundles.text
    review_body = review_bundles.json()
    _assert_forward_page(
        review_body,
        offset=3,
        limit=2,
        total=5,
        item_count=len(review_body["bundles"]),
    )

    source_detail = client.get(
        f"/api/projects/{project_id}/sources/{source_id}",
        params={"runs_offset": 2, "runs_limit": 2},
    )
    assert source_detail.status_code == 200, source_detail.text
    source_body = source_detail.json()
    _assert_forward_page(
        source_body["runs_page"],
        offset=2,
        limit=2,
        total=5,
        item_count=len(source_body["runs"]),
    )
    assert source_body["runs_page"]["latest_run"] is not None

    provenance = client.get(
        f"/api/projects/{project_id}/provenance",
        params={
            "runs_offset": 2,
            "runs_limit": 2,
            "receipts_offset": 2,
            "receipts_limit": 2,
        },
    )
    assert provenance.status_code == 200, provenance.text
    provenance_body = provenance.json()
    _assert_forward_page(
        provenance_body["runs_page"],
        offset=2,
        limit=2,
        total=5,
        item_count=len(provenance_body["runs"]),
    )
    _assert_forward_page(
        provenance_body["receipts_page"],
        offset=2,
        limit=2,
        total=5,
        item_count=len(provenance_body["receipts"]),
    )

    invalid_requests = [
        (f"/api/projects/{project_id}/actions/runs/{run_id}/rows", {"offset": -1}),
        (f"/api/projects/{project_id}/actions/runs/{run_id}/rows", {"limit": 0}),
        (f"/api/projects/{project_id}/actions/runs/{run_id}/rows", {"limit": 501}),
        (f"/api/projects/{project_id}/columns/{column_id}/runs", {"offset": -1}),
        (f"/api/projects/{project_id}/columns/{column_id}/runs", {"limit": 0}),
        (f"/api/projects/{project_id}/columns/{column_id}/runs", {"limit": 101}),
        (f"/api/projects/{project_id}/review/bundles", {"offset": -1}),
        (f"/api/projects/{project_id}/review/bundles", {"limit": 0}),
        (f"/api/projects/{project_id}/review/bundles", {"limit": 101}),
        (f"/api/projects/{project_id}/sources/{source_id}", {"runs_offset": -1}),
        (f"/api/projects/{project_id}/sources/{source_id}", {"runs_limit": 0}),
        (f"/api/projects/{project_id}/sources/{source_id}", {"runs_limit": 101}),
        (f"/api/projects/{project_id}/provenance", {"runs_offset": -1}),
        (f"/api/projects/{project_id}/provenance", {"runs_limit": 0}),
        (f"/api/projects/{project_id}/provenance", {"runs_limit": 101}),
        (f"/api/projects/{project_id}/provenance", {"receipts_offset": -1}),
        (f"/api/projects/{project_id}/provenance", {"receipts_limit": 0}),
        (f"/api/projects/{project_id}/provenance", {"receipts_limit": 101}),
    ]
    for path, params in invalid_requests:
        response = client.get(path, params=params)
        assert response.status_code == 422, f"{path} {params}: {response.text}"

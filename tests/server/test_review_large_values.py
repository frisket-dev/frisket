from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.engine.store.runs import RunResultStore
from frisket.server.app import create_app
from helpers import write_claimed_test_results


_LARGE_BYTES = 20 * 1024 * 1024


def _seed_large_review(tmp_path: Path) -> tuple[TestClient, str, Any, dict[str, int]]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Bounded Review"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("stories")
    source_id = project.add_column(sheet_id, "story")
    output_id = project.add_column(sheet_id, "summary", ai_generated=True)
    large = "z" * _LARGE_BYTES
    [row_id] = project.add_rows(sheet_id, [{"story": large}], {"story": source_id})
    op_id = project.append_op(
        "map", {"action_kind": "map.classify"}, label="Review large value"
    )
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.classify",
        params={
            "input_columns": ["story"],
            "fields": [{"name": "summary"}],
        },
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output_id,
                "value": large,
                "confidence": 0.7,
                "justification": "j" * (65_536 + 1),
            }
        ],
    )
    RunResultStore(project).point_column_at_run(op_id, output_id, run_id)
    RunResultStore(project).finish_run(run_id, "completed")
    return (
        client,
        project_id,
        project,
        {
            "sheet_id": sheet_id,
            "source_id": source_id,
            "output_id": output_id,
            "row_id": row_id,
            "run_id": run_id,
        },
    )


def _assert_bounded_bundle(bundle: dict[str, Any]) -> str:
    assert bundle["source"] == {"story": None}
    [source] = bundle["sources"]
    assert source["value"] is None
    assert source["bounded_value"] == {
        "preview": "z" * 2_048,
        "total_characters": _LARGE_BYTES,
        "total_bytes": _LARGE_BYTES,
        "truncated": True,
        "download_href": source["bounded_value"]["download_href"],
    }
    assert source["bounded_value"]["download_href"] is not None

    [field] = bundle["fields"]
    assert field["value"] is None
    assert field["bounded_value"]["preview"] == "z" * 2_048
    assert field["bounded_value"]["total_characters"] == _LARGE_BYTES
    assert field["bounded_value"]["total_bytes"] == _LARGE_BYTES
    assert field["bounded_value"]["truncated"] is True
    assert field["bounded_value"]["download_href"] is not None
    assert field["justification"] is None
    assert field["bounded_justification"] == {
        "preview": "j" * 2_048,
        "total_characters": 65_537,
        "total_bytes": 65_537,
        "truncated": True,
        "download_href": None,
    }
    assert field["can_edit"] is False
    assert bundle["items"][0]["bounded_value"] == field["bounded_value"]
    return str(field["bounded_value"]["download_href"])


def test_review_get_and_post_bound_large_values_before_full_decode(
    tmp_path: Path, monkeypatch
) -> None:
    client, project_id, project, ids = _seed_large_review(tmp_path)

    def full_decode_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Review bundle hydration must use bounded reads")

    monkeypatch.setattr(RunResultStore, "decoded_result_rows", full_decode_forbidden)
    monkeypatch.setattr(project, "get_values", full_decode_forbidden)
    monkeypatch.setattr(project, "get_values_with_refs", full_decode_forbidden)

    get_response = client.get(
        f"/api/projects/{project_id}/review/bundles",
        params={"run_id": ids["run_id"]},
    )
    assert get_response.status_code == 200, get_response.text
    assert len(get_response.content) < 100_000
    [get_bundle] = get_response.json()["bundles"]
    pinned_download = _assert_bounded_bundle(get_bundle)

    post_response = client.post(
        f"/api/projects/{project_id}/review/bundles",
        json={
            "kind": "rows",
            "run_id": ids["run_id"],
            "row_ids": [ids["row_id"]],
        },
    )
    assert post_response.status_code == 200, post_response.text
    assert len(post_response.content) < 100_000
    [post_bundle] = post_response.json()["bundles"]
    assert post_bundle == get_bundle

    monkeypatch.undo()
    project.apply_edits(
        [
            {
                "row_id": ids["row_id"],
                "column_id": ids["output_id"],
                "value": "new current value",
            }
        ],
        label="Replace current output",
    )
    assert client.get(pinned_download).status_code == 409

    historical_response = client.get(
        f"/api/projects/{project_id}/review/bundles",
        params={"run_id": ids["run_id"]},
    )
    assert historical_response.status_code == 200, historical_response.text
    [historical_bundle] = historical_response.json()["bundles"]
    [historical_field] = historical_bundle["fields"]
    assert historical_field["value"] is None
    assert historical_field["bounded_value"]["preview"] == "z" * 2_048
    assert historical_field["bounded_value"]["download_href"] is None
    assert historical_field["changed"] is False
    assert historical_field["can_edit"] is False

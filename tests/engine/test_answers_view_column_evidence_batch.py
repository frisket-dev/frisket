from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionError
from frisket.engine.executor.actions import run_action_spec
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_span,
    record_source_artifact,
)
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


def _seed_two_generated_rows(project: Project) -> dict[str, Any]:
    sheet_id = project.add_sheet("Documents")
    source_col = project.add_column(sheet_id, "Source", "file")
    output_col = project.add_column(
        sheet_id, "Contract value", "text", ai_generated=True
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"Source": "a.pdf"}, {"Source": "b.pdf"}],
        {"Source": source_col},
    )
    op_id = project.append_op(
        "map.extract",
        {
            "schema_version": "frisket.action.v2",
            "kind": "map.extract",
            "params": {"output_column": "Contract value"},
        },
        label="extract contract value",
    )
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.extract",
        model="provider/model",
        params={"output_column": "Contract value"},
        total_rows=2,
        row_ids=row_ids,
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": output_col,
                "value": "$1,250,000",
                "confidence": 0.9,
            },
            {
                "row_id": row_ids[1],
                "column_id": output_col,
                "value": "$980,000",
                "confidence": 0.9,
            },
        ],
    )
    RunResultStore(project).finish_run(run_id)
    RunResultStore(project).point_column_at_run(op_id, output_col, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, output_col, row_ids=row_ids)

    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        title="Contract",
        filename="a.pdf",
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="region",
        page_start=1,
        page_end=1,
        quote="$1,250,000",
    )

    links = {}
    for row_id in row_ids:
        links[row_id] = record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref=refs[row_id],
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=output_col,
            run_id=run_id,
            op_id=op_id,
            link_role="primary_support",
            confidence=0.9,
            spans=[{"span_id": span["id"], "rank": 0}],
        )
    return {
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "source_col": source_col,
        "output_col": output_col,
        "op_id": op_id,
        "run_id": run_id,
        "links": links,
        "refs": refs,
    }


def test_batch_returns_links_per_row_field_identical_to_per_cell_endpoint(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Column batch"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    seed = _seed_two_generated_rows(project)

    batch = client.get(
        f"/api/projects/{project_id}/sheets/{seed['sheet_id']}"
        f"/columns/{seed['output_col']}/evidence"
    )
    assert batch.status_code == 200, batch.text
    payload = batch.json()
    assert payload["schema_version"] == "frisket.column_evidence_batch.v1"
    assert payload["sheet_id"] == seed["sheet_id"]
    assert payload["column_id"] == seed["output_col"]
    by_row = {row["row_id"]: row for row in payload["rows"]}
    assert set(by_row.keys()) == set(seed["row_ids"])
    for row_id in seed["row_ids"]:
        assert [link["stable_id"] for link in by_row[row_id]["links"]] == [
            seed["links"][row_id]["stable_id"]
        ]

    # A chip's fields here must be byte-identical to the SAME link's fields
    # from the per-cell endpoint; both surfaces must be field-identical.
    per_cell = client.get(
        f"/api/projects/{project_id}/cells/{seed['row_ids'][0]}/{seed['output_col']}/evidence"
    )
    assert per_cell.status_code == 200, per_cell.text
    assert by_row[seed["row_ids"][0]]["links"][0] == per_cell.json()["links"][0]


def test_batch_honors_row_ids_window_filter(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Column batch window"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    seed = _seed_two_generated_rows(project)

    windowed = client.get(
        f"/api/projects/{project_id}/sheets/{seed['sheet_id']}"
        f"/columns/{seed['output_col']}/evidence",
        params={"row_ids": str(seed["row_ids"][0])},
    )
    assert windowed.status_code == 200, windowed.text
    payload = windowed.json()
    assert [row["row_id"] for row in payload["rows"]] == [seed["row_ids"][0]]


def test_batch_excludes_stale_superseded_links_current_value_ref_scoped(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Column batch stale"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    seed = _seed_two_generated_rows(project)

    edit_result = run_action_spec(
        project,
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "idempotency_key": "column-batch-stale@v1",
            "params": {
                "edits": [
                    {
                        "row_id": seed["row_ids"][0],
                        "column_id": seed["output_col"],
                        "value": "manually corrected",
                    }
                ]
            },
        },
        project_id=project_id,
    )
    assert edit_result.status == "completed"

    batch = client.get(
        f"/api/projects/{project_id}/sheets/{seed['sheet_id']}"
        f"/columns/{seed['output_col']}/evidence"
    )
    assert batch.status_code == 200, batch.text
    by_row = {row["row_id"]: row for row in batch.json()["rows"]}
    # row 0's link is now stale (its subject_ref no longer matches the
    # manually-edited current value) — it must be ABSENT, not a zero-links
    # entry; row 1's link is untouched and still active.
    assert seed["row_ids"][0] not in by_row
    assert seed["row_ids"][1] in by_row


def test_batch_zero_row_ids_short_circuits_to_empty_rows(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Column batch empty"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    seed = _seed_two_generated_rows(project)

    empty = client.get(
        f"/api/projects/{project_id}/sheets/{seed['sheet_id']}"
        f"/columns/{seed['output_col']}/evidence",
        params={"row_ids": ""},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["rows"] == []


def test_batch_unknown_column_is_typed_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Column batch 404"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    seed = _seed_two_generated_rows(project)

    response = client.get(
        f"/api/projects/{project_id}/sheets/{seed['sheet_id']}/columns/999999/evidence"
    )
    assert response.status_code == 404
    error = ActionError.model_validate(response.json())
    assert error.schema_version == "frisket.action_error.v1"
    assert error.code == "column_not_found"
    assert error.field == "column_id"


def test_batch_invalid_row_ids_token_is_typed_400(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Column batch bad row_ids"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    seed = _seed_two_generated_rows(project)

    response = client.get(
        f"/api/projects/{project_id}/sheets/{seed['sheet_id']}"
        f"/columns/{seed['output_col']}/evidence",
        params={"row_ids": "abc"},
    )
    assert response.status_code == 400
    error = ActionError.model_validate(response.json())
    assert error.code == "invalid_row_ids"

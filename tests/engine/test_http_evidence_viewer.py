from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionError
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


def _seed_generated_cell(
    project: Project,
) -> tuple[int, int, dict[str, int], int, int, dict[str, Any]]:
    sheet_id = project.add_sheet("Documents")
    source_column_id = project.add_column(sheet_id, "Source", "file")
    output_column_id = project.add_column(
        sheet_id, "Contract value", "text", ai_generated=True
    )
    row_id = project.add_rows(
        sheet_id,
        [{"Source": "contract.pdf"}],
        {"Source": source_column_id},
    )[0]
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
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output_column_id,
                "value": "$1,250,000",
                "confidence": 0.92,
            }
        ],
    )
    RunResultStore(project).finish_run(run_id)
    RunResultStore(project).point_column_at_run(op_id, output_column_id, run_id)
    _values, refs = project.get_values_with_refs(
        sheet_id, output_column_id, row_ids=[row_id]
    )
    return (
        sheet_id,
        row_id,
        {"source": source_column_id, "output": output_column_id},
        op_id,
        run_id,
        refs[row_id],
    )


def _seed_evidence(
    project: Project,
    *,
    current_ref: dict[str, Any],
    sheet_id: int,
    row_id: int,
    column_id: int,
    run_id: int,
    op_id: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    pdf_hash = project.add_blob(
        b"%PDF-1.4 public contract", "contract.pdf", "application/pdf"
    )
    page_image_hash = project.add_blob(
        b"fake png bytes", "contract-p3.png", "image/png"
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=pdf_hash,
        title="Contract",
        filename="contract.pdf",
        page_count=9,
        metadata={
            "page_images": {
                "3": {
                    "blob_hash": page_image_hash,
                    "width": 1700,
                    "height": 2200,
                }
            },
            "text_pages": {
                "3": "The total contract value is $1,250,000 payable in March."
            },
        },
    )
    region = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="region",
        page_start=3,
        page_end=3,
        bbox=[
            {
                "space": "page_normalized",
                "x0": 0.57,
                "y0": 0.40,
                "x1": 0.73,
                "y1": 0.45,
            }
        ],
        quote="$1,250,000",
        metadata={
            "raw": {
                "provider_space": "pixels",
                "x0": 969,
                "y0": 880,
                "x1": 1241,
                "y1": 990,
            }
        },
    )
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref=current_ref,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        run_id=run_id,
        op_id=op_id,
        receipt_id="receipt-map-extract",
        link_role="primary_support",
        confidence=0.91,
        producer={
            "action_kind": "map.extract",
            "model": "provider/model",
            "grounding_method": "model_bbox",
        },
        spans=[{"span_id": region["id"], "rank": 0, "required": True}],
    )
    record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref={**current_ref, "run_id": run_id + 999},
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        run_id=run_id + 999,
        op_id=op_id,
        link_role="support",
        spans=[{"span_id": region["id"]}],
    )
    return link, {"pdf_hash": pdf_hash, "page_image_hash": page_image_hash}


def _evidence_counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "source_artifacts",
            "source_spans",
            "evidence_links",
            "evidence_link_spans",
        )
    }


def test_http_cell_evidence_route_lists_current_links_and_stale_audit(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Evidence HTTP"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_id, columns, op_id, run_id, current_ref = _seed_generated_cell(
        project
    )
    link, hashes = _seed_evidence(
        project,
        current_ref=current_ref,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=columns["output"],
        run_id=run_id,
        op_id=op_id,
    )

    before_counts = _evidence_counts(project)
    default_response = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{columns['output']}/evidence"
    )
    empty_response = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{columns['source']}/evidence"
    )
    stable_viewer_response = client.get(
        f"/api/projects/{project_id}/evidence/links/{link['stable_id']}/viewer"
    )
    integer_viewer_response = client.get(
        f"/api/projects/{project_id}/evidence/links/{link['id']}/viewer"
    )
    after_counts = _evidence_counts(project)

    assert default_response.status_code == 200, default_response.text
    default_payload = default_response.json()
    assert default_payload["schema_version"] == "frisket.cell_evidence.v1"
    assert default_payload["sheet_id"] == sheet_id
    assert default_payload["row_id"] == row_id
    assert default_payload["column_id"] == columns["output"]
    assert default_payload["current_value_ref"] == current_ref
    assert default_payload["stale_count"] == 0
    assert [item["stable_id"] for item in default_payload["links"]] == [
        link["stable_id"]
    ]
    assert default_payload["links"][0]["status"] == "active"
    assert default_payload["links"][0]["viewer_href"] == (
        f"/api/projects/{project_id}/evidence/links/{link['stable_id']}/viewer"
    )

    assert empty_response.status_code == 200, empty_response.text
    empty_payload = empty_response.json()
    assert empty_payload["current_value_ref"]["kind"] == "source_cell"
    assert empty_payload["links"] == []
    assert empty_payload["stale_count"] == 0

    assert stable_viewer_response.status_code == 200, stable_viewer_response.text
    assert integer_viewer_response.status_code == 200, integer_viewer_response.text
    stable_viewer = stable_viewer_response.json()
    integer_viewer = integer_viewer_response.json()
    assert integer_viewer == stable_viewer
    assert stable_viewer["schema_version"] == "frisket.evidence_viewer.v1"
    assert stable_viewer["link"]["stable_id"] == link["stable_id"]
    assert stable_viewer["link"]["status"] == "active"
    artifact = stable_viewer["artifacts"][0]
    assert artifact["artifact_ref"]["blob"]["url"] == (
        f"/api/projects/{project_id}/blobs/{hashes['pdf_hash']}"
    )
    assert artifact["pages"][0]["image"]["url"] == (
        f"/api/projects/{project_id}/blobs/{hashes['page_image_hash']}"
    )
    assert artifact["pages"][0]["regions"][0]["bbox"][0]["space"] == ("page_normalized")
    assert before_counts == after_counts
    assert [
        row["status"]
        for row in project.db.execute("SELECT status FROM evidence_links ORDER BY id")
    ] == ["active", "active"]

    edit_result = run_action_spec(
        project,
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "idempotency_key": "http-evidence-stale@v1",
            "params": {
                "edits": [
                    {
                        "row_id": row_id,
                        "column_id": columns["output"],
                        "value": "manually corrected value",
                    }
                ]
            },
        },
        project_id=project_id,
    )
    assert edit_result.status == "completed"

    active_after_edit = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{columns['output']}/evidence"
    )
    stale_after_edit = client.get(
        f"/api/projects/{project_id}/cells/{row_id}/{columns['output']}/evidence",
        params={"include_stale": "1"},
    )
    stale_viewer_response = client.get(
        f"/api/projects/{project_id}/evidence/links/{link['stable_id']}/viewer"
    )

    assert active_after_edit.status_code == 200, active_after_edit.text
    assert active_after_edit.json()["links"] == []
    assert active_after_edit.json()["stale_count"] == 1

    assert stale_after_edit.status_code == 200, stale_after_edit.text
    stale_payload = stale_after_edit.json()
    assert [item["stable_id"] for item in stale_payload["links"]] == [link["stable_id"]]
    assert stale_payload["links"][0]["status"] == "stale"

    assert stale_viewer_response.status_code == 200, stale_viewer_response.text
    stale_viewer = stale_viewer_response.json()
    assert stale_viewer["link"]["status"] == "stale"
    assert stale_viewer["link"]["stale_reason"] == "manual_cell_edit"


def test_http_evidence_viewer_missing_refs_are_typed_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Evidence 404"}).json()[
        "id"
    ]

    missing_link = client.get(
        f"/api/projects/{project_id}/evidence/links/evidence_link:missing/viewer"
    )
    missing_project = client.get(
        "/api/projects/missing-project/evidence/links/evidence_link:missing/viewer"
    )

    assert missing_link.status_code == 404
    error = ActionError.model_validate(missing_link.json())
    assert error.schema_version == "frisket.action_error.v1"
    assert error.code == "evidence_link_not_found"
    assert error.field == "evidence_link_id"
    assert error.details["evidence_link_id"] == "evidence_link:missing"

    assert missing_project.status_code == 404
    project_error = ActionError.model_validate(missing_project.json()["detail"])
    assert project_error.schema_version == "frisket.action_error.v1"
    assert project_error.code == "project_not_found"
    assert project_error.field == "project_id"
    assert project_error.details["project_id"] == "missing-project"

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_action_job_detail_non_integer_path_emits_422(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "workspace")) as client:
        response = client.get("/api/projects/project-1/actions/jobs/not-an-integer")

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "int_parsing",
            "loc": ["path", "job_id"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
            "input": "not-an-integer",
        }
    ]


def test_receipt_and_job_detail_ignore_unknown_query_parameters(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "workspace")) as client:
        receipt = client.get(
            "/api/projects/project-missing/actions/v1/receipts/receipt-1",
            params={"unexpected": "value"},
        )
        job = client.get(
            "/api/projects/project-missing/actions/jobs/1",
            params={"unexpected": "value"},
        )

    # These two handlers intentionally lack reject_unknown_query_parameters.
    # The request reaches Workspace.get and reports the missing project; the
    # unknown query key produces neither 400 nor 422.
    assert receipt.status_code == 404
    assert job.status_code == 404

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionError, ActionResult, Receipt
from frisket.contracts.http.models import HttpError
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
        )
    )


def _import_rows_action() -> dict[str, Any]:
    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Receipt Rows",
        "params": {
            "columns": [
                {"name": "headline", "type": "text"},
                {"name": "source_url", "type": "url"},
            ],
            "rows": [
                {
                    "headline": "City hall awarded a no-bid contract.",
                    "source_url": "https://example.com/story/1",
                },
                {
                    "headline": "Routine road work finished early.",
                    "source_url": "https://example.com/story/2",
                },
            ],
            "source": {
                "kind": "inline",
                "label": "receipt lookup fixture",
                "fingerprint": "sha256:receipt-lookup",
            },
        },
        "idempotency_key": "http-receipt-lookup@sha256:stable",
    }


def test_http_v1_receipt_lookup_uses_canonical_receipt_contract(tmp_path) -> None:
    client = _client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Receipt lookup"}).json()[
        "id"
    ]

    run_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_import_rows_action(),
    )
    assert run_response.status_code == 200, run_response.text
    result = ActionResult.model_validate(run_response.json())
    assert result.receipt_id is not None

    lookup_response = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{result.receipt_id}"
    )
    assert lookup_response.status_code == 200, lookup_response.text
    receipt = Receipt.model_validate(lookup_response.json())
    assert receipt.schema_version == "frisket.receipt.v1"
    assert receipt.receipt_id == result.receipt_id
    assert receipt.project_id == project_id
    assert receipt.action_kind == "import.rows"
    assert receipt.status == "completed"
    assert {item.ref["kind"] for item in receipt.outputs} >= {
        "materialized_sheet",
        "source_rows",
    }
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "source_rows",
        "source_cell",
    }

    missing_response = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/receipt_missing"
    )
    assert missing_response.status_code == 404
    envelope = HttpError.model_validate(missing_response.json())
    missing = ActionError.model_validate(envelope.detail)
    assert missing.schema_version == "frisket.action_error.v1"
    assert missing.code == "receipt_not_found"
    assert missing.details["receipt_id"] == "receipt_missing"

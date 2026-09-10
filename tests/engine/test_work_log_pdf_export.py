import io

import pytest
from fastapi.testclient import TestClient
from action_test_helpers import typed_map_request
from pypdf import PdfReader

from frisket.contracts.action import ActionResult
from http_test_helpers import drain_queue


@pytest.fixture
def client(replay_client):
    return replay_client


def import_csv(
    client: TestClient,
    content: str,
    *,
    name: str = "Export Test",
    sheet_name: str | None = None,
) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    params = {"sheet_name": sheet_name} if sheet_name is not None else None
    resp = client.post(
        f"/api/projects/{pid}/import/csv",
        params=params,
        files={"file": ("rows.csv", content, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    return pid, resp.json()["sheet_id"]


def _seed_work_log(client: TestClient) -> str:
    pid, sheet_id = import_csv(
        client,
        "text\nCall 212-555-0123\nNo phone\n",
        name="Export Test",
        sheet_name="Calls Sheet",
    )
    action = typed_map_request(
        "map.regex_extract",
        sheet_id,
        params={"input_columns": ["text"], "pattern": r"\d{3}-\d{3}-\d{4}"},
        output_names={"extracted": "phone"},
        idempotency_key="work_log_pdf/map_regex_extract@sha256:v1",
    )
    run = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        params={},
        json=action,
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "queued"
    assert result.job_id is not None
    assert result.action.kind == "map.regex_extract"
    drain_queue(client)
    return pid


def test_project_work_log_pdf_export(client: TestClient):
    pid = _seed_work_log(client)

    out = client.get(f"/api/projects/{pid}/export/work-log.pdf")

    assert out.status_code == 200, out.text
    assert out.headers["content-type"].startswith("application/pdf")
    assert 'filename="Export-Test-work-log.pdf"' in out.headers["content-disposition"]
    assert out.content.startswith(b"%PDF-")

    reader = PdfReader(io.BytesIO(out.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Work log: Export Test" in text
    assert "Run 1 map.regex_extract via local/non-model" in text
    assert "Example row" in text
    assert "phone: 212-555-0123" in text
    assert "regex_extract:" not in text

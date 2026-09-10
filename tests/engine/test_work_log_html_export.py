import re

import pytest
from fastapi.testclient import TestClient
from action_test_helpers import typed_map_request

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
        sheet_name="Calls <Sheet>",
    )
    action = typed_map_request(
        "map.regex_extract",
        sheet_id,
        params={"input_columns": ["text"], "pattern": r"\d{3}-\d{3}-\d{4}"},
        output_names={"extracted": "phone"},
        idempotency_key="work_log_html/map_regex_extract@sha256:v1",
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


def test_project_work_log_html_export(client: TestClient):
    pid = _seed_work_log(client)

    out = client.get(f"/api/projects/{pid}/export/work-log.html")

    assert out.status_code == 200, out.text
    assert out.headers["content-type"].startswith("text/html")
    assert 'filename="Export-Test-work-log.html"' in out.headers["content-disposition"]
    html = out.text
    assert "<!doctype html>" in html
    assert "<h1>Work log: Export Test</h1>" in html
    assert "Calls &lt;Sheet&gt;" in html
    assert re.search(r"<th>Run</th>.*<th>Action</th>.*<th>Cost</th>", html, re.S)
    assert "map.regex_extract" in html
    assert "<th>Recipe</th>" not in html
    assert "Example changes for run 1" in html
    assert "212-555-0123" in html

"""Dataset and work-log exports."""

import csv
import io

import pytest
from fastapi.testclient import TestClient
from action_test_helpers import typed_map_request

from frisket.contracts.action import ActionResult
from frisket.engine.store.media_blobs import owned_media_metadata_document
from http_test_helpers import drain_queue, post_cell_edit_as_v1_action


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


def test_sheet_csv_export_streams_current_values(client: TestClient):
    pid, sheet_id = import_csv(
        client,
        'name,note,count\nAda,"hello, world",2\nGrace,plain,3\n',
        name="Dataset Export",
    )
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    cols = {c["name"]: c for c in data["columns"]}
    first_row = data["rows"][0]
    edit = {
        "row_id": first_row["id"],
        "column_id": cols["note"]["id"],
        "value": "edited | quoted",
    }
    r = post_cell_edit_as_v1_action(client, pid, [edit])
    assert r.status_code == 200, r.text

    out = client.get(
        f"/api/projects/{pid}/exports/sheets?sheet_id={sheet_id}&format=csv"
    )
    assert out.status_code == 200, out.text
    assert out.headers["content-type"].startswith("text/csv")
    assert 'filename="rows.csv"' in out.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(out.text)))
    assert rows == [
        {"name": "Ada", "note": "edited | quoted", "count": "2"},
        {"name": "Grace", "note": "plain", "count": "3"},
    ]


def _export_csv_action(sheet_id: int, path, *, key: str, formula_policy=None) -> dict:
    params = {
        "sheet_id": sheet_id,
        "destination": {"kind": "local_file", "path": str(path)},
    }
    if formula_policy is not None:
        params["formula_policy"] = formula_policy
    return {
        "action_id": "export.sheet_csv",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def test_csv_export_escapes_formula_cells_by_default(client: TestClient):
    pid, sheet_id = import_csv(
        client,
        "name,formula\nAda,=SUM(A1:A2)\nGrace,-1+2\nHal,plain\n",
        name="Formula",
    )
    out = client.get(
        f"/api/projects/{pid}/exports/sheets?sheet_id={sheet_id}&format=csv"
    )
    rows = list(csv.DictReader(io.StringIO(out.text)))
    assert rows[0]["formula"] == "'=SUM(A1:A2)"  # leading = escaped
    assert rows[1]["formula"] == "'-1+2"  # leading - escaped
    assert rows[2]["formula"] == "plain"  # untouched

    raw = client.get(
        f"/api/projects/{pid}/exports/sheets",
        params={"sheet_id": sheet_id, "format": "csv", "formula_policy": "raw"},
    )
    raw_rows = list(csv.DictReader(io.StringIO(raw.text)))
    assert raw_rows[0]["formula"] == "=SUM(A1:A2)"  # raw preserves exact text


def test_csv_escape_does_not_corrupt_native_negative_numbers(client: TestClient):
    pid = client.post("/api/projects", json={"name": "Numbers"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("nums")
    cols = {
        "label": project.add_column(sheet_id, "label"),
        "delta": project.add_column(sheet_id, "delta", type="number"),
    }
    project.add_rows(
        sheet_id,
        [{"label": "drop", "delta": -1.5}, {"label": "-not a formula", "delta": 2}],
        cols,
    )
    out = client.get(
        f"/api/projects/{pid}/exports/sheets?sheet_id={sheet_id}&format=csv"
    )
    rows = list(csv.DictReader(io.StringIO(out.text)))
    assert rows[0]["delta"] == "-1.5"  # native negative number NOT escaped
    assert rows[1]["delta"] == "2"
    assert rows[1]["label"] == "'-not a formula"  # a string starting with - is escaped


def test_csv_http_and_action_apply_identical_formula_policy(client, tmp_path):
    pid, sheet_id = import_csv(client, "name,formula\nAda,=1+1\n", name="Parity")
    out_path = tmp_path / "parity.csv"
    run = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_export_csv_action(sheet_id, out_path, key="parity@sha256:v1"),
    )
    assert run.status_code == 200, run.text
    assert ActionResult.model_validate(run.json()).status == "completed"
    # read bytes to preserve CRLF (read_text would normalize newlines)
    action_text = out_path.read_bytes().decode("utf-8")

    http_text = client.get(
        f"/api/projects/{pid}/exports/sheets?sheet_id={sheet_id}&format=csv"
    ).text
    assert action_text == http_text  # the two CSV paths cannot diverge
    assert list(csv.DictReader(io.StringIO(action_text)))[0]["formula"] == "'=1+1"


def test_csv_export_flattens_media_columns(client: TestClient):
    pid = client.post("/api/projects", json={"name": "MediaCsv"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("clips")
    cols = {
        "title": project.add_column(sheet_id, "title"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    digest = project.add_blob(
        b"fake-mp3-bytes" * 4,
        filename="a.mp3",
        mime="audio/mpeg",
        metadata=owned_media_metadata_document(probe={"duration_seconds": 3.0}),
    )
    project.add_rows(sheet_id, [{"title": "A", "media": {"blob": digest}}], cols)

    out = client.get(
        f"/api/projects/{pid}/exports/sheets?sheet_id={sheet_id}&format=csv"
    )
    header = out.text.splitlines()[0]
    assert "media.blob" in header
    assert "media.mime_type" in header
    rows = list(csv.DictReader(io.StringIO(out.text)))
    assert rows[0]["media.blob"] == digest  # hash, never bytes/path
    assert rows[0]["media.mime_type"] == "audio/mpeg"
    assert rows[0]["media"] == "a.mp3"  # display = filename


def test_project_work_log_markdown_export(client: TestClient):
    pid, sheet_id = import_csv(
        client,
        "text\nCall 212-555-0123\nNo phone\n",
        sheet_name="Calls | Sheet\nOne",
    )
    action = typed_map_request(
        "map.regex_extract",
        sheet_id,
        params={"input_columns": ["text"], "pattern": r"\d{3}-\d{3}-\d{4}"},
        output_names={"extracted": "phone | value\nline"},
        idempotency_key="work_log_export/map_regex_extract@sha256:v1",
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

    out = client.get(f"/api/projects/{pid}/export/work-log.md")
    assert out.status_code == 200, out.text
    assert out.headers["content-type"].startswith("text/markdown")
    assert 'filename="Export-Test-work-log.md"' in out.headers["content-disposition"]
    text = out.text
    assert "# Work log: Export Test" in text
    assert "| Calls \\| Sheet<br>One | 2 | 2 |" in text
    assert "## Operation History" in text
    assert "| 1 | import.csv | import.csv | applied |" in text
    assert "| 2 | map | map.regex_extract | applied |" in text
    assert "## Runs" in text
    assert "| Run | Action | Model | Rows | Failed | Cost | Outputs |" in text
    assert (
        "| 1 | map.regex_extract | local/non-model | 2/2 | 0 | $0.0000 | "
        "phone \\| value<br>line |"
    ) in text
    assert "## Action Receipts" in text
    assert "| import.csv | completed | 1 |" in text
    assert "| map.regex_extract | completed | 2 |" in text
    assert "Recipe" not in text
    assert "regex_extract:" not in text

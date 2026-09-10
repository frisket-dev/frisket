"""HTTP contract for the read-only replace-rules preview route.

POST /api/projects/{pid}/replace-rules/v1/preview evaluates the ordered rules
server-side with the SAME engine the resolve.replace commit uses (Python
``re``). It writes nothing (no receipt, no op, no column) and returns per-rule
match counts, the no-match unmatched, an optional test-value trace, and the
full-column ``value_hash`` the commit validates as ``expected_value_hash``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from helpers import make_client as _client


def _seed(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Replace Preview"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("orgs")
    cols = {"org": project.add_column(sheet_id, "org")}
    project.add_rows(
        sheet_id,
        [
            {"org": "Acme Inc"},
            {"org": "ACME Corp"},
            {"org": "globex ltd"},
            {"org": "n/a"},
            {"org": None},
            {"org": "Banana"},
        ],
        cols,
    )
    return pid, sheet_id


RULES = [
    {"match": "contains", "pattern": "acme", "target": "Acme"},
    {"match": "exact", "pattern": "n/a", "target": None},
    {"match": "regex", "pattern": r"^glo\w+", "target": "Globex"},
]


def test_replace_rules_preview_counts_without_side_effects(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    project = client.app.state.workspace.get(pid)
    ops_before = len(project.history())
    receipts_before = int(
        project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    )

    resp = client.post(
        f"/api/projects/{pid}/replace-rules/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "org", "rules": RULES},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == "frisket.replace_rules_preview.v1"
    assert body["sheet_id"] == sheet_id
    assert body["total_rows"] == 6
    assert body["rule_counts"] == [
        {"index": 0, "matched_rows": 2, "matched_values": 2},
        {"index": 1, "matched_rows": 1, "matched_values": 1},
        {"index": 2, "matched_rows": 1, "matched_values": 1},
    ]
    assert body["unmatched_rows"] == 1  # the null cell is not a unmatched row
    assert body["test_result"] is None  # only present when test_value is sent
    assert body["value_hash"].startswith("sha256:")

    # read-only: no receipt, no op, no new column
    assert len(project.history()) == ops_before
    assert (
        int(project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
        == receipts_before
    )
    assert [c["name"] for c in project.columns(sheet_id)] == ["org"]


def test_replace_rules_preview_test_value_trace(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    resp = client.post(
        f"/api/projects/{pid}/replace-rules/v1/preview",
        json={
            "sheet_id": sheet_id,
            "input_column": "org",
            "rules": RULES,
            "unmatched": "null",
            "test_value": "no rule hits this",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["test_result"] == {
        "matched_rule_index": None,
        "output": None,
    }


def test_replace_rules_preview_invalid_regex_is_bare_v1_error(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    resp = client.post(
        f"/api/projects/{pid}/replace-rules/v1/preview",
        json={
            "sheet_id": sheet_id,
            "input_column": "org",
            "rules": [{"match": "regex", "pattern": "(unclosed", "target": "x"}],
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "detail" not in body
    assert body["code"] == "invalid_regex"
    assert body["field"] == "rules[0].pattern"


def test_replace_rules_preview_missing_column_is_invalid_input_ref(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    resp = client.post(
        f"/api/projects/{pid}/replace-rules/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "missing", "rules": RULES},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "invalid_input_ref"
    assert body["field"] == "input_column"

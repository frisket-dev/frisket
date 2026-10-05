from __future__ import annotations

import json

from frisket.engine.store.result_generations import ResultGenerationStore
from helpers import make_client


def _project_with_rows(tmp_path):
    client = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Grid rows"}).json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("rows.csv", "name,score\nOne,1\nTwo,2\nThree,3\n", "text/csv")},
    )
    assert response.status_code == 200, response.text
    return client, project_id, int(response.json()["sheet_id"])


def test_rows_only_page_omits_scope_count_and_header_readers(
    tmp_path, monkeypatch
) -> None:
    client, project_id, sheet_id = _project_with_rows(tmp_path)
    url = f"/api/projects/{project_id}/sheets/{sheet_id}/data"
    full = client.get(url, params={"limit": 2})
    assert full.status_code == 200, full.text
    full_payload = full.json()
    assert full_payload["total"] == 3

    project = client.app.state.workspace.get(project_id)
    project.add_column(sheet_id, "generated", ai_generated=True)
    monkeypatch.setattr(
        "frisket.server.services.sheet_grid.compute_transcript_statuses",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rows-only page read transcript header metadata")
        ),
    )
    monkeypatch.setattr(ResultGenerationStore, "is_generation_managed", lambda *_: True)
    monkeypatch.setattr(
        project,
        "pending_replay_count",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rows-only page counted replay header metadata")
        ),
    )

    statements: list[str] = []
    project.db.set_trace_callback(statements.append)
    try:
        response = client.get(
            url,
            params={"limit": 2, "include_scope_metadata": "false"},
        )
    finally:
        project.db.set_trace_callback(None)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "total" not in payload
    assert [row["id"] for row in payload["rows"]] == [
        row["id"] for row in full_payload["rows"]
    ]
    normalized = [" ".join(statement.upper().split()) for statement in statements]
    assert not any("COUNT(*) OVER()" in statement for statement in normalized)
    assert not any(
        statement.startswith("SELECT COUNT(*) FROM ROWS R") for statement in normalized
    )


def test_rows_only_numeric_sorted_page_skips_window_count(tmp_path) -> None:
    client, project_id, sheet_id = _project_with_rows(tmp_path)
    project = client.app.state.workspace.get(project_id)
    statements: list[str] = []
    project.db.set_trace_callback(statements.append)
    try:
        response = client.get(
            f"/api/projects/{project_id}/sheets/{sheet_id}/data",
            params={
                "filter": json.dumps({"score": {"gte": "2"}}),
                "sort": json.dumps([{"column": "score", "dir": "desc"}]),
                "include_scope_metadata": "false",
            },
        )
    finally:
        project.db.set_trace_callback(None)

    assert response.status_code == 200, response.text
    assert "total" not in response.json()
    normalized = [" ".join(statement.upper().split()) for statement in statements]
    assert not any("COUNT(*) OVER()" in statement for statement in normalized)
    assert not any(
        statement.startswith("SELECT COUNT(*) FROM ROWS R") for statement in normalized
    )

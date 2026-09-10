"""The completed cluster result is sufficient authority for the entity-table CTA."""

from __future__ import annotations


import pytest

from http_test_helpers import drain_queue
from helpers import replace_test_source_cell
from tests.engine.test_entities import _client


@pytest.fixture
def reviewed_source(tmp_path):
    with _client(tmp_path) as client:
        pid = client.post("/api/projects", json={"name": "Entity workflow"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(pid)
        sheet_id = project.add_sheet("People")
        column_id = project.add_column(sheet_id, "name", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [
                {"name": name}
                for name in (
                    "Jon Smith",
                    "Smith, Jon",
                    "Jon Smith",
                    "jon  smith",
                    "Jane Doe",
                    "Doe, Jane",
                )
            ],
            {"name": column_id},
        )
        response = client.post(
            f"/api/projects/{pid}/clusters/v1/preview",
            json={
                "sheet_id": sheet_id,
                "input_column": "name",
                "method": "fingerprint",
            },
        )
        assert response.status_code == 200, response.text
        yield client, project, pid, sheet_id, column_id, row_ids, response.json()


def _commit_reviewed(source):
    client, _project, pid, sheet_id, _column_id, _row_ids, preview = source
    jon_key = next(
        group["key"]
        for group in preview["clusters"]
        if any(value["value"] == "Jon Smith" for value in group["values"])
    )
    request = {
        "action_id": "cluster.values",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": "name",
            "review": {
                "source_hash": preview["value_hash"],
                "canonical_overrides": {jon_key: "Jonathan Smith"},
                "excluded_members": {jon_key: ["Smith, Jon"]},
            },
        },
        "output_names": {"canonical": "Reviewed name"},
        "idempotency_key": "commit-reviewed-clusters",
    }
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "queued", result
    drain_queue(client)
    job = client.app.state.workspace.queue.get(result["job_id"])
    assert job.status == "done", job.error
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed", result
    assert result["receipt_id"] and isinstance(result["run_id"], int)
    return result


def _create_entities(client, pid, receipt_id):
    return client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "resolve.entities",
            "scope": {"kind": "project"},
            "params": {"source": {"kind": "cluster_values", "receipt_id": receipt_id}},
            "sheet_name": "Reviewed people",
            "output_names": {},
            "idempotency_key": "create-reviewed-entities",
        },
    )


def _counts(project):
    return tuple(
        project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("sheets", "columns", "rows", "ops", "receipts")
    )


def test_entity_table_uses_committed_choices_without_clustering_again(
    reviewed_source, monkeypatch
):
    client, project, pid, _sheet_id, _column_id, row_ids, _preview = reviewed_source
    committed = _commit_reviewed(reviewed_source)
    receipt_response = client.get(
        f"/api/projects/{pid}/actions/v1/receipts/{committed['receipt_id']}"
    )
    assert receipt_response.status_code == 200
    groups = next(
        item["ref"]["clusters"]
        for item in receipt_response.json()["evidence"]
        if item["ref"]["kind"] == "value_clusters"
    )
    assert len(groups) == 2
    jon = next(group for group in groups if group["canonical"] == "Jonathan Smith")
    assert jon["size"] == 3
    assert row_ids[1] not in jon["row_ids"]

    def unexpected_clustering(*_args, **_kwargs):
        pytest.fail("entity creation must consume the committed groups, not recluster")

    monkeypatch.setattr(
        "frisket.preview.cluster.compute_method_clusters", unexpected_clustering
    )
    monkeypatch.setattr(
        "frisket.engine.executor.value_cluster.compute_method_clusters",
        unexpected_clustering,
    )
    response = _create_entities(client, pid, committed["receipt_id"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed", result
    sheet = next(item for item in result["outputs"] if item["kind"] == "sheet")
    assert sheet["name"] == "Reviewed people"
    values = {
        column["name"]: project.get_values(sheet["sheet_id"], int(column["id"]))
        for column in project.columns(sheet["sheet_id"])
    }
    actual = {key: row_id for row_id, key in values["key"].items()}
    assert set(actual) == {group["key"] for group in groups}
    for group in groups:
        row_id = actual[group["key"]]
        assert values["entity"][row_id] == group["canonical"]
        assert values["mentions"][row_id] == group["size"]
        variants = values["source_variants"][row_id]
        assert [
            {"value": item["value"], "count": item["count"]} for item in variants
        ] == group["values"]
        assert sorted(
            member for item in variants for member in item["row_ids"]
        ) == sorted(group["row_ids"])


def test_preview_hash_is_not_a_committed_source_receipt(reviewed_source):
    client, project, pid, _sheet_id, _column_id, _row_ids, preview = reviewed_source
    assert "receipt_id" not in preview
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    before = _counts(project)
    response = _create_entities(client, pid, preview["value_hash"])
    assert response.status_code == 400, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["errors"][0]["code"] == "invalid_input_ref"
    assert _counts(project) == before


@pytest.mark.parametrize("change", ["values", "undo"])
def test_entity_table_refuses_a_stale_completed_cluster_result(reviewed_source, change):
    client, project, pid, _sheet_id, column_id, row_ids, _preview = reviewed_source
    committed = _commit_reviewed(reviewed_source)
    if change == "undo":
        project.undo()
    else:
        replace_test_source_cell(
            project,
            row_id=row_ids[0],
            column_id=column_id,
            value="Changed person",
        )
    before = _counts(project)
    response = _create_entities(client, pid, committed["receipt_id"])
    assert response.status_code == 409, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["errors"][0]["code"] == "stale_replay"
    assert _counts(project) == before

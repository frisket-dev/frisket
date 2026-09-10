from fastapi.testclient import TestClient

from frisket.engine.runner.preview import PREVIEW_MAX_ROWS
from frisket.server.app import create_app
from tests.preview.test_preview_inmemory import _poll


def test_public_semantic_preview_samples_source_but_searches_whole_target(
    tmp_path, monkeypatch
):
    embedded = []

    def embed(texts):
        embedded.extend(texts)
        return [
            [1.0, 0.0]
            if text.startswith("source-") or text == "best-target"
            else [0.0, 1.0]
            for text in texts
        ]

    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder", lambda *a, **kw: (embed, "fastembed/test")
    )

    def forbid_durable_match_evidence(*args, **kwargs):
        raise AssertionError("preview must not require a durable receipt writer")

    monkeypatch.setattr(
        "frisket.engine.store.receipts.ReceiptStore.record_semantic_match",
        forbid_durable_match_evidence,
    )
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Semantic preview"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    left, right = project.add_sheet("Source"), project.add_sheet("Lookup")
    source = project.add_column(left, "source", type="text")
    target = project.add_column(right, "target", type="text")
    count = PREVIEW_MAX_ROWS + 5
    source_rows = project.add_rows(
        left,
        [{"source": f"source-{index}"} for index in range(count)],
        {"source": source},
    )
    target_rows = project.add_rows(
        right,
        [{"target": f"other-{index}"} for index in range(count)]
        + [{"target": "best-target"}],
        {"target": target},
    )
    project.db.commit()

    def snapshot():
        return {
            table: tuple(
                tuple(row) for row in project.db.execute(f"SELECT * FROM {table}")
            )
            for table in (
                "sheets",
                "columns",
                "rows",
                "cells",
                "ops",
                "runs",
                "receipts",
                "results",
                "cell_result_heads",
                "model_calls",
                "output_column_claims",
                "effect_checkpoints",
            )
        }

    before = snapshot()
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/preview",
        json={
            "action_id": "join.semantic",
            "scope": {"kind": "sheet_rows", "sheet_id": left},
            "params": {
                "source": "source",
                "target": {"sheet_id": right, "column": "target"},
            },
            "sheet_name": "Not published",
            "idempotency_key": "preview-semantic",
        },
    )
    assert response.status_code == 202, response.text
    done = _poll(client, project_id, response.json()["preview_id"])
    assert done["status"] == "done", done
    result = done["result"]
    assert result["sampled"] == PREVIEW_MAX_ROWS
    assert set(result["rows"]) == {str(row) for row in source_rows[:PREVIEW_MAX_ROWS]}
    assert all(
        row["match_value"]["value"] == "best-target" for row in result["rows"].values()
    )
    assert all(
        row["matched_row_id"]["value"] == target_rows[-1]
        for row in result["rows"].values()
    )
    assert "best-target" in embedded
    assert {value for value in embedded if value.startswith("source-")} == {
        f"source-{index}" for index in range(PREVIEW_MAX_ROWS)
    }
    assert snapshot() == before

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_project_debug_does_not_create_response_cache_sidecar(tmp_path: Path) -> None:
    app = create_app(tmp_path / "ws")
    client = TestClient(app)
    project_id = client.post("/api/projects", json={"name": "Debug sidecar"}).json()[
        "id"
    ]
    project = app.state.workspace.get(project_id)
    cache_file = project.path / "project.cache.db"
    assert not cache_file.exists()

    response = client.get(f"/api/projects/{project_id}/debug")

    assert response.status_code == 200, response.text
    assert response.json()["cache"] == {
        "mode": "replay",
        "enabled": False,
        "entries": 0,
    }
    assert not cache_file.exists()


@pytest.mark.parametrize(
    "observed_counts, cell_count, expected",
    [([10, None], 10, None), ([0], 0, 0), (None, 10, 10), (None, None, None)],
)
def test_debug_and_row_totals_use_call_facts_without_poisoning_secondary_cells(
    tmp_path, observed_counts, cell_count, expected
):
    from frisket.ai.models.metadata import ModelCallMeta
    from frisket.engine.store.runs import RunResultStore
    from frisket.server.run_payloads import action_run_rows_payload
    from helpers import write_claimed_test_results

    with TestClient(create_app(tmp_path / "ws")) as client:
        project_id = client.post("/api/projects", json={"name": "Usage"}).json()["id"]
        project = client.app.state.workspace.get(project_id)
        sheet = project.add_sheet("data")
        source = project.add_column(sheet, "source")
        project.add_rows(sheet, [{"source": "hello"}], {"source": source})
        row_id = project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=?", (sheet,)
        ).fetchone()[0]
        answer = project.add_column(sheet, "answer", ai_generated=True)
        detail = project.add_column(sheet, "detail", ai_generated=True)
        run_id = RunResultStore(project).start_run(
            project.append_change("map"), sheet, "map.ask", total_rows=1
        )
        calls = [
            ModelCallMeta.provider_call(
                capability="llm.complete",
                engine="mock/model",
                provider="mock",
                provider_kind="platform_api",
                credential_source="local",
                provider_cost_usd=None,
                provider_reported_cost_usd=None,
                cost_source="unknown",
                duration_ms=None,
                units={"tokens_in": count, "tokens_out": count},
            ).as_dict()
            for count in observed_counts or []
        ]
        write_claimed_test_results(
            project,
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": answer,
                    "value": "answer",
                    "tokens_in": cell_count,
                    "tokens_out": cell_count,
                    "model_calls": calls,
                },
                {"row_id": row_id, "column_id": detail, "value": "detail"},
            ],
        )
        row = action_run_rows_payload(project, run_id)["rows"][0]
        assert row["tokens_in"] == expected and row["tokens_out"] == expected
        assert row["cells"][1]["tokens_in"] is None
        response = client.get(f"/api/projects/{project_id}/debug")
        assert response.status_code == 200, response.text
        summary = next(
            item for item in response.json()["results"] if item["run_id"] == run_id
        )
        assert summary["tokens_in"] == expected and summary["tokens_out"] == expected

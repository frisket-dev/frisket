from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.engine.store.runs import RunResultStore
from frisket.operability.trace import trace_path
from frisket.server.app import create_app
from helpers import write_claimed_test_results


@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        ([True, True], "failed"),
        ([False, True], "partial"),
        ([False, False], "completed"),
    ],
)
def test_run_outcomes_agree_across_status_trace_and_provenance(
    tmp_path: Path, errors: list[bool], expected: str
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "outcomes"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("stories")
    source = project.add_column(sheet, "story")
    output = project.add_column(sheet, "topic", ai_generated=True)
    rows = project.add_rows(
        sheet, [{"story": "one"}, {"story": "two"}], {"story": source}
    )
    op = project.append_op("map", {"action_kind": "map.classify"}, label="classify")
    store = RunResultStore(project)
    run = store.start_run(
        op,
        sheet,
        "map.classify",
        model="test/model",
        params={},
        total_rows=2,
        row_ids=rows,
    )
    write_claimed_test_results(
        project,
        run,
        [
            {
                "row_id": row,
                "column_id": output,
                "value": None if error else "ok",
                **({"error": "failed"} if error else {}),
            }
            for row, error in zip(rows, errors, strict=True)
        ],
    )
    store.finish_run(run)
    sidecar = trace_path(project.path, run)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(sidecar, "wt", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"kind": "meta", "run_id": run, "action_kind": "map.classify"})
            + "\n"
        )
        stream.write(json.dumps({"kind": "row", "row_id": rows[0]}) + "\n")

    base = f"/api/projects/{project_id}"
    status = client.get(f"{base}/actions/runs/{run}/status")
    recorded = client.get(f"{base}/actions/runs/{run}/trace/rows/{rows[0]}")
    absent = client.get(f"{base}/actions/runs/{run}/trace/rows/{rows[1]}")
    provenance = client.get(f"{base}/provenance")
    for response in (status, recorded, absent, provenance):
        assert response.status_code == 200, response.text
    assert recorded.json()["recorded"] is True
    assert absent.json()["recorded"] is False
    assert status.json()["run"]["status"] == expected
    assert recorded.json()["run"]["status"] == expected
    assert absent.json()["run"]["status"] == expected
    assert (
        next(item for item in provenance.json()["runs"] if item["run_id"] == run)[
            "status"
        ]
        == expected
    )

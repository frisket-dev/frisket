from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.runner.preview import PreviewColumn, PreviewResult
from frisket.server.route_errors import RouteError
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.workspace import Workspace


def test_evaluation_preview_reads_full_artifacts_and_checks_ownership(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("One", project_id="one")
    workspace.create("Two", project_id="two")
    service = ActionPreviewRunService(workspace)
    registry = service._registry
    large = "z" * 100_000
    result = PreviewResult(
        sheet_id=1,
        columns=[PreviewColumn(name="subject", column_type="text")],
        values={
            1: {"subject": {"value": large}},
            2: {"subject": {"value": None, "error": "failed"}},
        },
        row_ids=[1, 2],
        sampled=2,
        total=2,
    )
    result.finalize_artifacts()
    context = {
        "sheet_id": 1,
        "action_id": "map.ask",
        "output_columns": [
            {"logical_key": "answer", "column_id": None, "current_name": "subject"}
        ],
        "row_ids": [1, 2, 3],
        "source_fingerprints": {"1": "a", "2": "b", "3": "c"},
        "settings": {"params": {}},
    }
    job = registry.start(
        "one", 3, lambda progress, cancel: result, comparison_context=context
    )
    registry._threads[job.id].join(timeout=10)
    assert job.finished_at is not None
    data = service.evaluation_preview("one", job.id)
    assert data["rows"]["1"]["answer"] == {"state": "value", "value": large}
    assert data["rows"]["2"]["answer"]["state"] == "error"
    assert data["rows"]["3"]["answer"]["state"] == "missing"
    with pytest.raises(RouteError):
        service.evaluation_preview("two", job.id)
    registry.shutdown()


def test_evaluation_preview_reports_failure_and_cancelled_partial_rows(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("One", project_id="one")
    service = ActionPreviewRunService(workspace)
    registry = service._registry
    context = {
        "sheet_id": 1,
        "action_id": "map.ask",
        "output_columns": [
            {"logical_key": "answer", "column_id": None, "current_name": "subject"}
        ],
        "row_ids": [1, 2],
        "source_fingerprints": {"1": "a", "2": "b"},
        "settings": {},
    }

    def fail(progress, cancel):
        raise RuntimeError("failed")

    failed = registry.start("one", 2, fail, comparison_context=context)
    registry._threads[failed.id].join(timeout=10)
    assert failed.finished_at is not None
    assert all(
        row["answer"]["state"] == "error"
        for row in service.evaluation_preview("one", failed.id)["rows"].values()
    )

    def cancel_after_one(progress, cancel):
        cancel.set()
        return PreviewResult(
            sheet_id=1,
            columns=[PreviewColumn(name="subject", column_type="text")],
            values={1: {"subject": {"value": "yes"}}},
            row_ids=[1, 2],
            sampled=1,
            total=2,
        )

    cancelled = registry.start("one", 2, cancel_after_one, comparison_context=context)
    registry._threads[cancelled.id].join(timeout=10)
    assert cancelled.finished_at is not None
    rows = service.evaluation_preview("one", cancelled.id)["rows"]
    assert rows["1"]["answer"] == {"state": "value", "value": "yes"}
    assert rows["2"]["answer"] == {"state": "cancelled"}
    registry.shutdown()

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from frisket.actions.types import ActionRequest
from frisket.contracts.action import ActionError
from frisket.actions.system import typed_action_for_request
from frisket.engine.store import Project


def test_static_action_cutovers_preserve_non_source_prechecks() -> None:
    # Capability authority now comes from the registered action, never a
    # request-authored override (including one that tries to remove it).
    with pytest.raises(ValidationError, match="capabilities"):
        ActionRequest.model_validate(
            {
                "action_id": "cluster.values",
                "capabilities": [],
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": {"source": "name"},
                "idempotency_key": "cluster@sha256:test",
            }
        )

    with pytest.raises(ValueError, match="distinct"):
        typed_action_for_request(
            {
                "action_id": "map.find",
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "sheet_name": "Findings",
                "params": {
                    "source": "body",
                    "instruction": "Find examples.",
                    "fields": [
                        {"name": "detail", "type": "text"},
                        {"name": "detail", "type": "text"},
                    ],
                    "model": "anthropic/claude-haiku-4-5",
                },
                "idempotency_key": "find@sha256:test",
            }
        )


def test_cluster_values_project_type_gate_uses_its_descriptor(tmp_path: Path) -> None:
    from frisket.engine.executor import run_action_spec

    project = Project.create(tmp_path / "cluster.frisket", name="Cluster")
    try:
        sheet_id = project.add_sheet("Inputs")
        project.add_column(sheet_id, "score", type="number")
        result = run_action_spec(
            project,
            {
                "action_id": "cluster.values",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"source": "score", "method": "fingerprint", "min_size": 2},
                "idempotency_key": "wrong-column-type",
            },
            project_id="test",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_params"
        assert result.errors[0].details["errors"] == [
            {
                "message": "Clustering requires a visible text, category, or link source column"
            }
        ]
        assert [column["name"] for column in project.columns(sheet_id)] == ["score"]
        assert project.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


def test_map_find_project_type_gate_uses_its_descriptor(tmp_path: Path) -> None:
    from frisket.engine.executor.find_action import prepare_typed_find_admission

    project = Project.create(tmp_path / "find.frisket", name="Find")
    try:
        sheet_id = project.add_sheet("Inputs")
        column_id = project.add_column(sheet_id, "score", type="number")
        row_id = project.add_rows(
            sheet_id,
            [{"score": 3}],
            {"score": column_id},
        )[0]
        bound = typed_action_for_request(
            {
                "action_id": "map.find",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [row_id],
                },
                "sheet_name": "Findings",
                "params": {
                    "source": "score",
                    "instruction": "Find examples.",
                    "model": "anthropic/claude-haiku-4-5",
                },
                "idempotency_key": "find-type-gate",
            }
        )

        error = prepare_typed_find_admission(project, bound)

        assert isinstance(error, ActionError)
        assert error.code == "invalid_input_ref"
        assert error.field == "params.source"
        assert error.details["accepted_column_types"] == [
            "text",
            "timestamped_transcript",
            "audio",
            "video",
            "image",
            "file",
        ]
    finally:
        project.close()

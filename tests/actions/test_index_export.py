from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry
from frisket.actions.embeddings import IndexExportParams
from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.actions.types import ActionRequest
from frisket.engine.executor.action_families.embeddings import (
    run_typed_embedding_action,
)
from frisket.engine.store import Project
from frisket.sdk import ActionParams, IndexExport, IndexExporter, action
from tests.engine.test_embedding_index_export_executor import _seed


class CustomParams(ActionParams):
    selected: str
    folder: str
    keep_vectors: Any = False
    formats: list[str] = ["arrow", "jsonl", "arrow"]


def custom_export(params: CustomParams, files: IndexExporter) -> IndexExport:
    result = files.export(
        params.selected.removeprefix("chosen:"),
        path=str(Path(params.folder) / "out"),
        formats=params.formats,
        include_vectors=params.keep_vectors,
    )
    # Returned data is not authority over the committed artifact or provenance.
    return result.model_copy(update={"index_id": "invented", "artifacts": ()})


def _bound(params: dict[str, Any], *, key: str = "custom-export"):
    definition = action(
        name="archive",
        title="Archive",
        description="Export the selected index.",
        category=ActionCategory.SOURCES,
        run=custom_export,
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(definition,)),)
    ).get("custom.archive")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.archive",
            scope={"kind": "project"},
            params=params,
            idempotency_key=key,
        ),
    )


def test_reused_exporter_records_actual_arguments_not_custom_return(tmp_path):
    project = Project.create(tmp_path / "project")
    try:
        seeded = _seed(project, tmp_path)
        bound = _bound(
            {"selected": f"chosen:{seeded['index_id']}", "folder": str(tmp_path)}
        )
        result = run_typed_embedding_action(project, "p", bound)
        assert result.status == "completed", result.errors
        assert [output.ref["format"] for output in result.outputs] == ["arrow", "jsonl"]
        jsonl = result.outputs[1].ref
        assert Path(jsonl["path"]).parent == seeded["out"]
        rows = [
            json.loads(line) for line in Path(jsonl["path"]).read_text().splitlines()
        ]
        assert all(row["vector"] is None for row in rows)
        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?",
                (result.receipt_id,),
            ).fetchone()[0]
        )
        assert receipt["inputs"][0]["ref"]["index_id"] == seeded["index_id"]
        assert receipt["action_kind"] == "custom.archive"
        assert receipt["provider_use"][0]["cost_actual"] == 0.0
        assert receipt["provider_use"][0]["external_api"] is False
        assert len(receipt["exports"]) == 2
        replay = run_typed_embedding_action(project, "p", bound)
        assert replay.receipt_id == result.receipt_id
    finally:
        project.close()


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"selected": "chosen:missing"}, "embedding_index_not_found"),
        ({"keep_vectors": 1}, "invalid_params"),
        ({"formats": []}, "invalid_params"),
        ({"formats": ["csv"]}, "invalid_params"),
        ({"selected": "chosen:   "}, "invalid_params"),
    ],
)
def test_reused_exporter_refuses_actual_arguments_without_invented_fields(
    tmp_path, patch, code
):
    project = Project.create(tmp_path / "project")
    try:
        seeded = _seed(project, tmp_path)
        bound = _bound(
            {
                "selected": f"chosen:{seeded['index_id']}",
                "folder": str(tmp_path),
                **patch,
            }
        )
        result = run_typed_embedding_action(project, "p", bound)
        assert result.status == "failed"
        assert result.errors[0].code == code
        assert result.errors[0].action_kind == "custom.archive"
        assert result.errors[0].field == "params"
        assert result.receipt_id is None
        assert list(seeded["out"].iterdir()) == []
    finally:
        project.close()


@pytest.mark.parametrize("patch", [{"destination": []}, {"include_vectors": 1}])
def test_typed_export_params_reject_malformed_values(patch):
    request = {
        "action_id": "embedding.index_export",
        "scope": {"kind": "project"},
        "params": {
            "index_id": "index",
            "destination": {"kind": "local_dir", "path": "/tmp"},
            **patch,
        },
        "idempotency_key": "shape",
    }
    result = validate_root_action(request)
    assert result.ok is False
    assert result.error.code == "invalid_action_request"


def test_index_export_formats_deduplicate_in_order():
    params = IndexExportParams(
        index_id="index",
        destination={"kind": "local_dir", "path": "/tmp"},
        formats=["arrow", "jsonl", "arrow", "parquet"],
    )
    assert params.formats == ["arrow", "jsonl", "parquet"]

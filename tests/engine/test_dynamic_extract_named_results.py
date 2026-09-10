from __future__ import annotations

import json

import pytest

from executor_harness import run_action_with_confirmation
from frisket.actions.core import RegisteredAction
from frisket.actions.extract import EXTRACT
from frisket.actions.registry import ACTION_REGISTRY
from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from test_model_rows_actions import _DataAdapter


@pytest.fixture
def extracted(tmp_path, monkeypatch):
    registered = RegisteredAction("custom.extract", EXTRACT)
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, registered.action_id: registered},
    )
    project = Project.create(tmp_path / "dynamic-list.frisket")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "body", type="text")
    project.add_rows(sheet, [{"body": "Ada launched a rocket."}], {"body": column})
    requests = []
    router = ModelRouter(keys={"anthropic": "stub"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = _DataAdapter(
        requests,
        {
            "entities": {
                "value": [{"name": "Ada"}, {"name": "rocket"}],
                "evidence": [[{"quote": "Ada"}], [{"quote": "rocket"}]],
            }
        },
    )
    result = run_action_with_confirmation(
        project,
        {
            "action_id": registered.action_id,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": ["body"],
                "model": "anthropic/claude-haiku-4-5",
                "fields": [
                    {
                        "name": "entities",
                        "type": "list",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    }
                ],
                "grounding": {"enabled": True, "citation_required": True},
            },
            "output_names": {"entities": "published_entities"},
            "idempotency_key": "extract",
        },
        project_id="dynamic-list",
        router=router,
    )
    assert result.status == "completed", result.errors
    assert len(requests) == 1
    named = next(
        output.ref for output in result.outputs if output.kind == "named_result"
    )
    assert named["source_action_kind"] == "custom.extract"
    assert named["output_key"] == "entities"
    assert named["route"] == "published_entities"
    try:
        yield project, named, requests
    finally:
        project.close()


def derive(project, named):
    return run_action_with_confirmation(
        project,
        {
            "action_id": "derive.table_from_list",
            "scope": {"kind": "project"},
            "sheet_name": "Entities",
            "params": {
                "source": {
                    "kind": "named_result",
                    **{
                        key: named[key]
                        for key in (
                            "sheet_id",
                            "column_id",
                            "run_id",
                            "route",
                            "schema",
                        )
                    },
                },
                "item_schema": named["item_schema"],
                "columns": [{"name": "name", "path": "$.name", "type": "text"}],
            },
            "idempotency_key": "derive",
        },
        project_id="dynamic-list",
    )


def test_reused_extract_named_list_materializes_from_admitted_dynamic_outputs(
    extracted,
):
    project, named, requests = extracted
    result = derive(project, named)
    assert result.status == "completed", result.errors
    child = next(output.sheet_id for output in result.outputs if output.kind == "sheet")
    output_column = next(
        column for column in project.columns(child) if column["name"] == "name"
    )
    assert list(project.get_values(child, output_column["id"]).values()) == [
        "Ada",
        "rocket",
    ]
    assert len(requests) == 1


@pytest.mark.parametrize(
    "tamper", ["params", "output_names", "action_kind", "missing_spec"]
)
def test_dynamic_output_handoff_rejects_unbound_producer_spec(extracted, tamper):
    project, named, requests, *_ = extracted
    spec = json.loads(
        project.db.execute(
            "SELECT params FROM runs WHERE id=?", (named["run_id"],)
        ).fetchone()[0]
    )
    if tamper == "missing_spec":
        spec = {}
    elif tamper == "params":
        spec["params"]["instruction"] = "Not the admitted extraction"
    elif tamper == "output_names":
        spec["output_names"]["entities"] = "other_output"
    else:
        spec["action_kind"] = "map.extract"
    project.db.execute(
        "UPDATE runs SET params=? WHERE id=?", (json.dumps(spec), named["run_id"])
    )
    project.db.commit()
    result = derive(project, named)
    assert result.status == "failed"
    assert any(error.code == "invalid_input_ref" for error in result.errors), (
        result.errors
    )
    assert len(requests) == 1

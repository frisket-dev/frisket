from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import SheetRows
from frisket.authoring.recipe_registry import (
    ActionRegistryError,
    build_action_artifact,
    validate_action_artifact,
)
from frisket.server.app import create_app


def _spec():
    return {
        "action_kind": "map.classify",
        "sheet_id": 12,
        "params": {
            "source": {"text": "{{story}} — {{desk}}"},
            "engine": "llm",
            "model": "openai/gpt-5-mini",
            "fields": [
                {"name": "beat", "type": "category", "labels": ["city", "courts"]}
            ],
        },
        "output_names": {"beat": "editorial_beat"},
    }


def _checks(artifact):
    return {
        check["name"]: check["evidence"] for check in artifact["receipts"][0]["checks"]
    }


def test_typed_artifact_publishes_and_imports_without_recipe_lookup(
    tmp_path, monkeypatch
):
    def forbidden_lookup(_kind):
        raise AssertionError("typed actions must not consult the recipe registry")

    monkeypatch.setattr(
        "frisket.authoring.recipe_registry.get_recipe", forbidden_lookup
    )
    with TestClient(create_app(tmp_path)) as client:
        response = client.post(
            "/api/actions/v1/registry/artifacts", json={"name": "Beat", "spec": _spec()}
        )
        assert response.status_code == 200, response.text
        artifact = response.json()["artifact"]
        assert "sheet_id" not in artifact["spec"]
        assert artifact["spec"]["params"] == _spec()["params"]
        assert artifact["action"] == {"kind": "map.classify", "version": "1"}
        checks = _checks(artifact)
        assert checks["action.registered"]["llm"] is True
        assert checks["spec.sources"]["source_columns"] == ["story", "desk"]
        assert checks["spec.outputs"]["output_fields"] == [
            {"name": "editorial_beat", "column_type": "category"}
        ]
        imported = client.post(
            "/api/actions/v1/registry/imports",
            json={"artifact_id": artifact["artifact_id"]},
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["saved_action"]["spec"] == artifact["spec"]
        assert validate_action_artifact(artifact) == artifact


@pytest.mark.parametrize(
    "change",
    [
        {"input_columns": ["story"]},
        {"output_names": {"unknown": "result"}},
        {"output_names": {"beat": 7}},
        {"params": {"source": ["story"], "invented": True}},
    ],
)
def test_typed_artifact_rejects_legacy_or_invalid_authored_values(change):
    with pytest.raises(ActionRegistryError):
        build_action_artifact(name="Invalid", spec={**_spec(), **change})


@pytest.mark.parametrize("engine, llm", [("llm", True), ("deepl", False)])
def test_artifact_model_fact_follows_selected_typed_engine(engine, llm):
    params = {"source": ["story"], "target_language": "es", "engine": engine}
    if llm:
        params["model"] = "openai/gpt-5-mini"
    artifact = build_action_artifact(
        name="Translate", spec={"action_kind": "map.translate", "params": params}
    )
    assert _checks(artifact)["action.registered"]["llm"] is llm
    assert _checks(artifact)["spec.outputs"]["output_fields"] == [
        {"name": "translation", "column_type": "text"}
    ]


def test_artifact_preserves_registered_plugin_path():
    from frisket.authoring.plugin_registry import register_recipe, unregister_recipe
    from frisket.ops.base import Recipe

    class PluginRecipe(Recipe):
        name = "artifact_test"
        version = "plugin-v3"
        llm = False
        consumes_resolution = False
        cost_class = "free"

        def output_fields(self, spec):
            return [{"name": "result", "column_type": "text"}]

        def source_columns(self, spec):
            return spec["input_columns"]

    kind = "artifact_test.plugin"
    register_recipe(
        PluginRecipe(name="artifact_test", version="plugin-v3", llm=False),
        action_kind=kind,
        plugin="artifact_test",
    )
    try:
        artifact = build_action_artifact(
            name="Plugin", spec={"action_kind": kind, "input_columns": ["story"]}
        )
        assert artifact["action"]["version"] == "plugin-v3"
        assert _checks(artifact)["spec.sources"]["source_columns"] == ["story"]
        assert validate_action_artifact(artifact) == artifact
    finally:
        unregister_recipe(kind)


def test_deterministic_typed_artifact_uses_declared_logical_output():
    artifact = build_action_artifact(
        name="Template",
        spec={
            "action_kind": "map.template",
            "params": {"template": {"text": "Hello {{name}}"}},
            "output_names": {"rendered": "greeting"},
        },
    )
    checks = _checks(artifact)
    assert checks["action.registered"]["llm"] is False
    assert checks["spec.sources"]["source_columns"] == ["name"]
    assert checks["spec.outputs"]["output_fields"] == [
        {"name": "greeting", "column_type": "text"}
    ]


@pytest.mark.parametrize("name", ["body", "answer"])
def test_judge_artifact_and_invocation_share_input_overlap_guard(name):
    params = {
        "source": ["body"],
        "judged_column": "answer",
        "model": "openai/gpt-5-mini",
        "guidelines": "Supported",
    }
    action = ACTION_REGISTRY.get("map.judge")
    output_names = {"verdict": name}
    with pytest.raises(ValueError, match="overlap input columns"):
        action.bind_values(
            scope=SheetRows(sheet_id=1), params=params, output_names=output_names
        )
    with pytest.raises(ActionRegistryError, match="overlap input columns"):
        build_action_artifact(
            name="Invalid judge",
            spec={
                "action_kind": action.action_id,
                "params": params,
                "output_names": output_names,
            },
        )
    artifact = build_action_artifact(
        name="Valid judge",
        spec={
            "action_kind": action.action_id,
            "params": params,
            "output_names": {"verdict": "review"},
        },
    )
    artifact["spec"]["output_names"] = output_names
    with pytest.raises(ActionRegistryError, match="overlap input columns"):
        validate_action_artifact(artifact)


def test_portable_binding_preserves_semantic_join_child_name_policy():
    action = ACTION_REGISTRY.get("join.semantic")
    params = {
        "source": "description",
        "target": {"sheet_id": 2, "column": "category"},
        "carry": ["id"],
    }
    bound, fields = action.bind_params(
        params=params, output_names={"carry.id": "record_id"}
    )
    assert [field.key for field in fields] == [
        "match_value",
        "match_score",
        "matched_row_id",
    ]
    for names in ({"carry.missing": "No"}, {"source": "match_value"}):
        with pytest.raises(ValueError):
            action.bind_params(params=bound.model_dump(mode="json"), output_names=names)
    with pytest.raises(ValueError, match="cover every logical output"):
        action.bind_params(
            params=params,
            output_names={"carry.id": "record_id"},
            require_all_output_names=True,
        )


def test_portable_binding_keeps_project_output_name_restriction():
    with pytest.raises(ValueError, match="project actions do not accept output_names"):
        ACTION_REGISTRY.get("column.add").bind_params(
            params={}, output_names={"column": "new"}
        )

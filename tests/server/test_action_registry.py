from __future__ import annotations

import re
from typing import Any

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.server.services import saved_actions


RECIPE_RE = re.compile(r"recipe", re.IGNORECASE)


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(tmp_path, router=ModelRouter(cache=None, cache_mode="off"))
    )


def _recipe_hits(value: Any, path: str = "$", depth: int = 0) -> list[str]:
    if depth > 50:
        return [f"{path} exceeded scan depth"]
    hits: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}[{key!r}]"
            if RECIPE_RE.search(str(key)):
                hits.append(f"{key_path} key={key!r}")
            hits.extend(_recipe_hits(item, key_path, depth + 1))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            hits.extend(_recipe_hits(item, f"{path}[{idx}]", depth + 1))
    elif isinstance(value, str) and RECIPE_RE.search(value):
        hits.append(f"{path} value={value!r}")
    return hits


def _publishable_saved_action_spec() -> dict[str, Any]:
    return {
        "action_kind": "map.classify",
        "action_name": "Beat classifier",
        "sheet_id": "1",
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": "Classify short local-government story blurbs.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["city", "courts"],
                    "description": "Local news beat.",
                }
            ],
            "include_confidence": False,
            "include_justification": False,
        },
        "output_names": {"beat": "beat"},
    }


def test_v1_action_registry_publish_list_get_import_without_public_recipe_language(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    workspace = client.app.state.workspace
    saved_entry = workspace.save_recipe(
        "Beat classifier",
        _publishable_saved_action_spec(),
    )
    saved_template = saved_actions.saved_action_template(saved_entry)
    assert _recipe_hits(saved_template) == []

    published = client.post(
        "/api/actions/v1/registry/artifacts",
        json={
            "saved_action_id": saved_template["id"],
            "description": "Classifies short local-government story blurbs.",
            "publisher": {"name": "Frisket tests"},
            "dataset": {
                "name": "beat-fixture",
                "version": "2026-06-17",
                "row_count": 2,
            },
            "checks": [
                {
                    "name": "eval.fixture.expected_labels",
                    "status": "passed",
                    "evidence": {"correct": 2, "total": 2},
                }
            ],
        },
    )
    assert published.status_code == 200, published.text
    artifact = published.json()["artifact"]
    artifact_id = artifact["artifact_id"]
    assert _recipe_hits(published.json()) == []
    assert artifact["schema_version"] == "frisket.action_artifact.v1"
    assert artifact["action"]["kind"] == "map.classify"
    assert artifact["spec"]["action_kind"] == "map.classify"
    assert "sheet_id" not in artifact["spec"]
    assert artifact["spec"]["params"]["source"] == ["story"]
    assert artifact["spec"]["params"]["fields"][0]["name"] == "beat"
    assert artifact["spec"]["output_names"] == {"beat": "beat"}
    assert artifact["receipts"][0]["schema_version"] == "frisket.action_eval_receipt.v1"
    assert artifact["receipts"][0]["action"]["kind"] == "map.classify"
    assert "action.registered" in {
        check["name"] for check in artifact["receipts"][0]["checks"]
    }

    stored = client.app.state.workspace.action_registry.get(artifact_id)
    assert stored == artifact

    registry = client.get("/api/actions/v1/registry/artifacts")
    assert registry.status_code == 200, registry.text
    registry_payload = registry.json()
    assert _recipe_hits(registry_payload) == []
    assert registry_payload["schema_version"] == "frisket.action_registry.v1"
    assert [entry["artifact_id"] for entry in registry_payload["artifacts"]] == [
        artifact_id
    ]
    assert registry_payload["artifacts"][0]["action"]["kind"] == "map.classify"

    fetched = client.get(f"/api/actions/v1/registry/artifacts/{artifact_id}")
    assert fetched.status_code == 200, fetched.text
    fetched_payload = fetched.json()
    assert _recipe_hits(fetched_payload) == []
    assert fetched_payload["artifact"]["artifact_id"] == artifact_id
    assert fetched_payload["artifact"]["action"]["kind"] == "map.classify"

    imported = client.post(
        "/api/actions/v1/registry/imports",
        json={"artifact_id": artifact_id, "name": "Pulled beat classifier"},
    )
    assert imported.status_code == 200, imported.text
    import_payload = imported.json()
    assert _recipe_hits(import_payload) == []
    assert import_payload["schema_version"] == "frisket.action_registry_import.v1"
    assert import_payload["saved_action"]["schema_version"] == (
        "frisket.saved_action_template.v1"
    )
    assert import_payload["saved_action"]["action_kind"] == "map.classify"
    assert import_payload["saved_action"]["spec"]["action_kind"] == "map.classify"
    assert import_payload["artifact"]["action"]["kind"] == "map.classify"

    templates = [
        saved_actions.saved_action_template(entry)
        for entry in workspace.saved_recipes()
    ]
    assert [template["name"] for template in templates] == [
        "Beat classifier",
        "Pulled beat classifier",
    ]
    assert _recipe_hits(templates) == []

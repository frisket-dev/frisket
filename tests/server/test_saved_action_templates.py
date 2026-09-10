"""Saved-action registry publish contract and route removal.

GET/POST /api/actions/v1/saved-actions were removed (zero callers outside
tests); the saved-recipes STORE they wrote to stays, since the action-registry
publish(saved_action_id)/import(save_recipe) flows build on it directly via
``workspace.save_recipe``/``saved_recipe_by_id``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.ai.llm import ModelRouter
from frisket.authoring.recipe_registry import REMOVED_ACTION_KEYS
from frisket.server.app import create_app
from frisket.server import schemas
from frisket.server.services import saved_actions
from frisket.server.services.action_registry import (
    ActionRegistryRouteError,
    ActionRegistryService,
)


RECIPE_RE = re.compile(r"recipe", re.IGNORECASE)


def _client(tmp_path) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return TestClient(
        create_app(workspace, router=ModelRouter(cache=None, cache_mode="off"))
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


def test_v1_saved_actions_can_publish_by_saved_id_without_legacy_save_routes(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    workspace = client.app.state.workspace
    entry = workspace.save_recipe(
        "Beat classifier",
        _publishable_saved_action_spec(),
    )

    published = client.post(
        "/api/actions/v1/registry/artifacts",
        json={
            "saved_action_id": entry["id"],
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
    assert _recipe_hits(published.json()) == []
    artifact = published.json()["artifact"]
    assert artifact["schema_version"] == "frisket.action_artifact.v1"
    assert artifact["action"]["kind"] == "map.classify"
    assert artifact["spec"]["action_kind"] == "map.classify"
    assert "sheet_id" not in artifact["spec"]
    assert artifact["spec"]["params"]["source"] == ["story"]
    assert artifact["spec"]["params"]["fields"][0]["name"] == "beat"
    assert artifact["spec"]["output_names"] == {"beat": "beat"}

    stored = workspace.action_registry.get(artifact["artifact_id"])
    assert stored == artifact


def test_saved_action_boundaries_refuse_removed_wire_spellings(tmp_path) -> None:
    client = _client(tmp_path)
    workspace = client.app.state.workspace
    service = ActionRegistryService(workspace)
    for removed_key in sorted(REMOVED_ACTION_KEYS):
        spec = {"action_kind": "map.classify", removed_key: "removed"}
        with pytest.raises(ValidationError, match=removed_key):
            schemas.PublishActionArtifactBody(name="Removed spelling", spec=spec)
        with pytest.raises(ActionRegistryRouteError, match=removed_key):
            service.publish_artifact(
                saved_action_id=None,
                spec=spec,
                name="Removed spelling",
                description="",
                publisher={},
                dataset={},
                checks=[],
            )
        with pytest.raises(saved_actions.SavedActionSpecError, match=removed_key):
            workspace.save_recipe("Removed spelling", spec)

    assert workspace.saved_recipes() == []
    assert workspace.action_registry.list()["artifacts"] == []


def test_saved_action_and_legacy_recipe_routes_absent_from_openapi(tmp_path) -> None:
    client = _client(tmp_path)
    openapi = client.get("/openapi.json").json()
    assert "/api/recipes/save" not in openapi["paths"]
    assert "/api/recipes/saved" not in openapi["paths"]
    assert "/api/actions/v1/saved-actions" not in openapi["paths"]

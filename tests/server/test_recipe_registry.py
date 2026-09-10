from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.authoring import recipe_registry
from frisket.authoring.recipe_registry import (
    ActionRegistryError,
    ActionRegistryStore,
    build_action_artifact,
)
from frisket.server.app import create_app
from frisket.server.services import saved_actions
from frisket.server.services.action_registry import (
    ActionRegistryRouteError,
    ActionRegistryService,
)


SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
RECIPE_RE = re.compile(r"recipe", re.IGNORECASE)


def _client(tmp_path):
    return TestClient(
        create_app(tmp_path, router=ModelRouter(cache=None, cache_mode="off"))
    )


def _classify_spec() -> dict:
    # Typed portable spec: action_kind + nested typed params (+ optional
    # output_names). ``sheet_id``/``overwrite`` are project-local keys the
    # publisher strips.
    return {
        "action_kind": "map.classify",
        "action_name": "Local beat classifier",
        "sheet_id": 123,
        "overwrite": True,
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": "Short local-government stories.",
            "fields": [
                {"name": "beat", "type": "category", "labels": ["city", "courts"]}
            ],
        },
        "output_names": {"beat": "beat"},
    }


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


def test_action_registry_publish_import_round_trip_is_content_addressed(
    tmp_path, monkeypatch
):
    clock = {"now": "2026-09-06T17:50:24Z"}
    monkeypatch.setattr(recipe_registry, "_utc_now", lambda: clock["now"])
    client = _client(tmp_path)
    body = {
        "name": "Local beat classifier",
        "description": "Classifies short local-government story blurbs.",
        "spec": _classify_spec(),
        "publisher": {"name": "Frisket tests"},
        "dataset": {"name": "beat-fixture", "version": "2026-06-14", "row_count": 2},
        "checks": [
            {
                "name": "eval.fixture.expected_labels",
                "status": "passed",
                "evidence": {"correct": 2, "total": 2},
            }
        ],
    }

    published = client.post("/api/actions/v1/registry/artifacts", json=body)
    assert published.status_code == 200, published.text
    first = published.json()
    assert _recipe_hits(first) == []
    artifact = first["artifact"]
    artifact_id = first["artifact_id"]
    assert first["schema_version"] == "frisket.action_registry_publish.v1"
    assert artifact["digest"] == f"sha256:{artifact_id}"
    assert SHA256_RE.match(artifact["digest"])
    assert artifact["schema_version"] == "frisket.action_artifact.v1"
    assert artifact["action"]["kind"] == "map.classify"
    assert artifact["spec"]["action_kind"] == "map.classify"
    assert "sheet_id" not in artifact["spec"]
    assert "overwrite" not in artifact["spec"]

    stored = client.app.state.workspace.action_registry.get(artifact_id)
    assert stored == artifact
    on_disk = json.loads(
        (tmp_path / "action_registry" / "artifacts" / f"{artifact_id}.json").read_text()
    )
    assert on_disk == artifact
    assert _recipe_hits(on_disk) == []

    # An identical evaluation published later has the same content identity.
    # It must not replace the stored evaluation's original timestamp.
    clock["now"] = "2026-09-06T17:50:25Z"
    republished = client.post("/api/actions/v1/registry/artifacts", json=body)
    assert republished.status_code == 200, republished.text
    assert republished.json()["artifact_id"] == artifact_id
    assert republished.json()["artifact"] == artifact
    assert _recipe_hits(republished.json()) == []

    registry = client.get("/api/actions/v1/registry/artifacts")
    assert registry.status_code == 200
    registry_payload = registry.json()
    assert _recipe_hits(registry_payload) == []
    assert registry_payload["schema_version"] == "frisket.action_registry.v1"
    entries = registry_payload["artifacts"]
    assert [entry["artifact_id"] for entry in entries] == [artifact_id]
    assert entries[0]["action"]["kind"] == "map.classify"

    fetched = client.get(f"/api/actions/v1/registry/artifacts/{artifact_id}")
    assert fetched.status_code == 200
    fetched_payload = fetched.json()
    assert _recipe_hits(fetched_payload) == []
    assert fetched_payload["artifact"]["artifact_id"] == artifact_id
    receipt = fetched_payload["artifact"]["receipts"][0]
    assert SHA256_RE.match(receipt["receipt_id"])
    assert receipt["schema_version"] == "frisket.action_eval_receipt.v1"
    assert receipt["action"]["kind"] == "map.classify"
    assert receipt["dataset"]["name"] == "beat-fixture"
    assert receipt["dataset"]["version"] == "2026-06-14"
    assert receipt["dataset"]["row_count"] == 2
    assert SHA256_RE.match(receipt["dataset"]["fingerprint"])
    check_names = {check["name"] for check in receipt["checks"]}
    assert {
        "action.registered",
        "spec.portable",
        "spec.outputs",
        "safety.policy",
        "eval.fixture.expected_labels",
    } <= check_names
    safety = next(
        check for check in receipt["checks"] if check["name"] == "safety.policy"
    )
    assert set(safety["evidence"]["blocked_actions"]) == {
        "map.mcp_extract",
        "map.python",
        "research.answer",
    }

    imported = client.post(
        "/api/actions/v1/registry/imports",
        json={"artifact_id": artifact_id, "name": "Pulled beat classifier"},
    )
    assert imported.status_code == 200, imported.text
    import_payload = imported.json()
    assert _recipe_hits(import_payload) == []
    assert import_payload["schema_version"] == "frisket.action_registry_import.v1"
    saved = import_payload["saved_action"]
    assert saved["name"] == "Pulled beat classifier"
    assert saved["spec"] == artifact["spec"]
    assert import_payload["artifact"]["artifact_id"] == artifact_id
    assert saved["action_kind"] == "map.classify"

    listed_internal = client.app.state.workspace.saved_recipes()
    assert listed_internal[0]["registry_artifact"]["artifact_id"] == artifact_id
    assert listed_internal[0]["eval_receipts"][0]["receipt_id"] == receipt["receipt_id"]

    templates = [
        saved_actions.saved_action_template(entry)
        for entry in client.app.state.workspace.saved_recipes()
    ]
    assert [(template["id"], template["name"]) for template in templates] == [
        (saved["id"], saved["name"])
    ]
    assert templates[0]["action_kind"] == "map.classify"
    assert _recipe_hits(templates) == []

    clock["now"] = "2026-09-06T17:50:26Z"
    other_client = _client(tmp_path / "other-workspace")
    copied = other_client.post(
        "/api/actions/v1/registry/imports",
        json={
            "artifact": fetched_payload["artifact"],
            "name": "Copied beat classifier",
        },
    )
    assert copied.status_code == 200, copied.text
    copied_payload = copied.json()
    assert _recipe_hits(copied_payload) == []
    assert copied_payload["saved_action"]["name"] == "Copied beat classifier"
    assert copied_payload["saved_action"]["spec"] == artifact["spec"]
    copied = other_client.app.state.workspace.action_registry.get(artifact_id)
    assert copied == artifact


def test_registry_merges_new_evaluation_without_retimestamping_existing_receipt(
    tmp_path,
):
    first = build_action_artifact(
        name="Beat classifier", spec=_classify_spec(), now="2026-09-06T17:50:24Z"
    )
    later = build_action_artifact(
        name="Beat classifier",
        spec=_classify_spec(),
        now="2026-09-06T17:50:25Z",
        checks=[{"name": "eval.extra", "status": "passed", "evidence": {"correct": 2}}],
    )
    assert first["artifact_id"] == later["artifact_id"]
    assert first["receipts"][0]["receipt_id"] != later["receipts"][0]["receipt_id"]
    store = ActionRegistryStore(tmp_path)
    store.publish(first)
    stored = store.publish(later)
    assert stored["published_at"] == first["published_at"]
    assert {receipt["receipt_id"]: receipt for receipt in stored["receipts"]} == {
        first["receipts"][0]["receipt_id"]: first["receipts"][0],
        later["receipts"][0]["receipt_id"]: later["receipts"][0],
    }


def test_action_registry_persists_canonical_kind_without_an_implementation_alias(
    tmp_path,
):
    client = _client(tmp_path)
    published = client.post(
        "/api/actions/v1/registry/artifacts",
        json={
            "name": "Beat classifier",
            "spec": _classify_spec(),
            "dataset": {"name": "classify-fixture", "version": "1", "row_count": 1},
        },
    )
    assert published.status_code == 200, published.text
    payload = published.json()
    assert _recipe_hits(payload) == []
    artifact = payload["artifact"]
    assert artifact["action"]["kind"] == "map.classify"
    assert artifact["spec"]["action_kind"] == "map.classify"
    assert artifact["receipts"][0]["action"]["kind"] == "map.classify"
    check = next(
        check
        for check in artifact["receipts"][0]["checks"]
        if check["name"] == "action.registered"
    )
    assert check["evidence"]["action_kind"] == "map.classify"

    stored = client.app.state.workspace.action_registry.get(payload["artifact_id"])
    assert stored == artifact
    assert _recipe_hits(stored) == []


def test_registry_digest_binds_receipt_metadata_without_interpreting_it(tmp_path):
    client = _client(tmp_path)
    published = client.post(
        "/api/actions/v1/registry/artifacts",
        json={
            "name": "Beat classifier",
            "spec": _classify_spec(),
            "publisher": {"team": ["local", "investigations"]},
            "dataset": {
                "row_count": "about two",
                "metadata": ["curator supplied"],
            },
            "checks": [
                {
                    "name": "eval.notes",
                    "status": "recorded",
                    "evidence": ["manually reviewed"],
                },
                {"name": ["nested"], "status": "passed"},
            ],
        },
    )

    assert published.status_code == 200, published.text
    artifact = published.json()["artifact"]
    assert artifact["publisher"] == {"team": ["local", "investigations"]}
    receipt = artifact["receipts"][0]
    assert receipt["dataset"]["row_count"] == "about two"
    assert receipt["dataset"]["metadata"] == ["curator supplied"]
    assert receipt["checks"][-1]["name"] == ["nested"]
    assert receipt["checks"][-2] == {
        "name": "eval.notes",
        "status": "recorded",
        "evidence": ["manually reviewed"],
    }


@pytest.mark.parametrize(
    ("source", "message"),
    (
        ([7], "Input should be a valid string"),
        ([" "], "value must be non-empty and trimmed"),
        # The flattened legacy spelling is not a typed artifact field.
        ("legacy", "unknown typed artifact fields: input_columns"),
    ),
)
def test_registry_publish_rejects_invalid_source_columns_without_emitting_artifact(
    tmp_path, source, message
) -> None:
    client = _client(tmp_path)
    spec = _classify_spec()
    if source == "legacy":
        spec["input_columns"] = ["story"]
    else:
        spec["params"]["source"] = source

    response = client.post(
        "/api/actions/v1/registry/artifacts",
        json={"name": "Invalid classifier", "spec": spec},
    )

    assert response.status_code == 400
    assert "invalid action spec" in response.text
    assert message in response.text
    assert "artifact" not in response.json()
    registry = client.get("/api/actions/v1/registry/artifacts")
    assert registry.status_code == 200
    assert registry.json()["artifacts"] == []


def _external_action_artifact() -> dict:
    return build_action_artifact(
        name="Beat classifier",
        spec={
            "action_kind": "map.classify",
            "action_name": "Beat classifier",
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
            },
            "output_names": {"beat": "editorial_beat"},
        },
        description="Classifies short local-government story blurbs.",
        publisher={"name": "External newsroom"},
        dataset=None,
        checks=None,
    )


def test_registry_uses_the_canonical_lookup_key_not_the_private_implementation_name(
    monkeypatch,
) -> None:
    from frisket.authoring import recipe_registry

    class PrivateNamedImplementation:
        name = "private_runner_name"
        version = "7"
        llm = False

        def output_fields(self, spec):
            return spec["output_fields"]

        def source_columns(self, _spec):
            return []

    lookups = []
    monkeypatch.setattr(
        recipe_registry,
        "get_recipe",
        lambda action_kind: lookups.append(action_kind) or PrivateNamedImplementation(),
    )
    artifact = recipe_registry.build_action_artifact(
        name="Canonical plugin action",
        spec={
            "action_kind": "plugin.alias",
            "output_fields": [{"name": "result", "type": "text"}],
        },
    )
    assert lookups == ["plugin.alias", "plugin.alias"]
    assert artifact["action"] == {"kind": "plugin.alias", "version": "7"}
    assert artifact["spec"]["action_kind"] == "plugin.alias"


def test_registry_import_save_publish_preserves_artifact_and_receipt_ids(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    client = _client(root)
    original = _external_action_artifact()

    imported = client.post(
        "/api/actions/v1/registry/imports", json={"artifact": original}
    )
    assert imported.status_code == 200, imported.text
    saved_id = imported.json()["saved_action"]["id"]

    republished = client.post(
        "/api/actions/v1/registry/artifacts",
        json={
            "saved_action_id": saved_id,
            "description": original["description"],
            "publisher": original["publisher"],
        },
    )
    assert republished.status_code == 200, republished.text
    assert republished.json()["artifact_id"] == original["artifact_id"]

    stored = client.app.state.workspace.action_registry.get(original["artifact_id"])
    assert stored is not None
    assert stored["spec"] == original["spec"]
    assert [receipt["receipt_id"] for receipt in stored["receipts"]] == [
        original["receipts"][0]["receipt_id"]
    ]


@pytest.mark.parametrize(
    ("artifact_name", "import_name"),
    (("Named artifact", "   "), ("   ", None)),
)
def test_registry_import_rejects_blank_effective_name_without_residue(
    tmp_path,
    artifact_name,
    import_name,
):
    client = _client(tmp_path)
    artifact = build_action_artifact(
        name=artifact_name,
        spec=_classify_spec(),
        now="2026-08-29T00:00:00Z",
    )
    body = {"artifact": artifact}
    if import_name is not None:
        body["name"] = import_name

    response = client.post("/api/actions/v1/registry/imports", json=body)

    assert response.status_code == 400, response.text
    assert "name cannot be blank" in response.text
    assert client.app.state.workspace.saved_recipes() == []
    assert not (tmp_path / "action_registry").exists()


def test_saved_action_v1_spec_is_the_artifact_spec_without_an_adapter() -> None:
    spec = {
        "action_kind": "map.classify",
        "action_name": "Beat classifier",
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "fields": [
                {"name": "beat", "type": "category", "labels": ["city", "courts"]}
            ],
        },
        "output_names": {"beat": "editorial_beat"},
        "publisher_extra": {"x": 1},
    }
    assert saved_actions.require_saved_action_spec(spec) == spec


def test_action_registry_import_rejects_malformed_or_tampered_artifacts(tmp_path):
    client = _client(tmp_path)
    published = client.post(
        "/api/actions/v1/registry/artifacts",
        json={
            "name": "Beat classifier",
            "spec": _classify_spec(),
            "dataset": {"name": "beat-fixture", "version": "1", "row_count": 1},
        },
    )
    assert published.status_code == 200, published.text
    artifact_id = published.json()["artifact_id"]
    good = client.app.state.workspace.action_registry.get(artifact_id)

    cases = (
        (("schema_version",), "frisket.recipe_artifact.v1", "unsupported action"),
        (("recipe",), {"name": "classify", "version": "1"}, "unknown keys"),
        (("name",), "Different name", "artifact_id"),
        (("name",), [], "name must be a string"),
        (("published_at",), [], "published_at must be a string"),
        (("spec", "sheet_id"), 999, "project-local"),
        (("spec", "recipe"), "classify", "removed keys"),
        (("spec", "action_kind"), "classify", "not canonical"),
        # Flattened legacy output declarations are not typed artifact fields;
        # the logical outputs live in params.fields / output_names.
        (("spec", "output_fields"), [{"name": "beat"}], "unknown typed artifact"),
        (("spec", "params", "fields", 0, "name"), [], "fields.0.name"),
        (("spec", "params", "fields", 0, "name"), "  ", "fields.0.name"),
        (("spec", "output_names", "beat"), [], "output names must be non-empty"),
        (("spec", "output_names", "beat"), "  ", "output names must be non-empty"),
        (("spec", "output_names", "bogus"), "x", "unknown output names: bogus"),
        (("receipts",), [], "eval receipt"),
        (("receipts", 0, "dataset"), [], "dataset must be an object"),
        (("receipts", 0, "checks"), {}, "checks must be a list"),
        (("receipts", 0, "created_at"), [], "created_at must be a string"),
        (("receipts", 0, "recipe"), {"name": "classify"}, "unknown keys"),
        (("action", "kind"), "map.extract", "does not match spec"),
    )
    for path, value, message in cases:
        artifact = copy.deepcopy(good)
        target = artifact
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        bad = client.post(
            "/api/actions/v1/registry/imports", json={"artifact": artifact}
        )
        assert bad.status_code == 400, (path, bad.text)
        assert message in bad.text


@pytest.mark.parametrize(
    "removed_key",
    (
        "sheetId",
        "targetColumnId",
        "targetColumnName",
        "inputColumns",
        "inputTemplate",
        "outputFields",
        "targetLanguage",
        "includeConfidence",
        "includeJustification",
        "includeLatLon",
        "rowIds",
        "previewRows",
        "recipe",
        "recipeKind",
        "recipeName",
        "recipe_version",
        "fields",
    ),
)
def test_action_registry_import_rejects_removed_saved_action_keys_without_residue(
    tmp_path,
    removed_key,
):
    client = TestClient(
        create_app(tmp_path, router=ModelRouter(cache=None, cache_mode="off")),
        raise_server_exceptions=False,
    )
    spec = _classify_spec()
    spec[removed_key] = "removed"
    try:
        artifact = build_action_artifact(
            name="Removed spelling",
            spec=spec,
            now="2026-08-29T00:00:00Z",
        )
    except ActionRegistryError as exc:
        assert f"removed keys: {removed_key}" in str(exc)
        artifact = build_action_artifact(
            name="Removed spelling",
            spec=_classify_spec(),
            now="2026-08-29T00:00:00Z",
        )
        artifact["spec"][removed_key] = "removed"

    response = client.post(
        "/api/actions/v1/registry/imports",
        json={"artifact": artifact},
    )

    assert response.status_code == 400, (removed_key, response.text)
    assert f"removed keys: {removed_key}" in response.text
    workspace = client.app.state.workspace
    assert workspace.action_registry.list()["artifacts"] == []
    assert workspace.saved_recipes() == []
    assert not list(tmp_path.glob("action_registry/artifacts/*.json"))


def test_action_registry_store_refuses_removed_keys_on_publish_and_lookup(
    tmp_path,
) -> None:
    store = ActionRegistryStore(tmp_path)
    artifact = build_action_artifact(
        name="Removed spelling",
        spec=_classify_spec(),
        now="2026-08-29T00:00:00Z",
    )
    artifact["spec"]["outputFields"] = [{"name": "legacy"}]

    with pytest.raises(ActionRegistryError, match="removed keys: outputFields"):
        store.publish(artifact)
    assert not store.root.exists()

    store.artifacts_dir.mkdir(parents=True)
    artifact_path = store.artifacts_dir / f"{artifact['artifact_id']}.json"
    artifact_path.write_text(json.dumps(artifact))
    with pytest.raises(ActionRegistryError, match="removed keys: outputFields"):
        store.get(artifact["artifact_id"])
    with pytest.raises(ActionRegistryError, match="removed keys: outputFields"):
        store.list()


def test_action_artifact_preserves_nested_recipe_vocabulary_as_opaque_user_data(
    tmp_path,
) -> None:
    # Typed built-in params are closed models, so free-form nested user data
    # in ``spec.params`` only exists on the plugin recipe path, which remains.
    from frisket.authoring.plugin_registry import register_recipe, unregister_recipe
    from frisket.ops.base import Recipe

    class OpaqueParamsRecipe(Recipe):
        name = "opaque_params"
        version = "plugin-v1"
        llm = False
        consumes_resolution = False
        cost_class = "free"

        def output_fields(self, spec):
            return [{"name": "result", "column_type": "text"}]

        def source_columns(self, spec):
            return spec["input_columns"]

    kind = "opaque_params.plugin"
    register_recipe(
        OpaqueParamsRecipe(name="opaque_params", version="plugin-v1", llm=False),
        action_kind=kind,
        plugin="opaque_params",
    )
    spec = {
        "action_kind": kind,
        "input_columns": ["story"],
        "params": {
            "recipe": {"name": "Grandma's soup", "steps": ["mix", "simmer"]},
            "recipe_version": "family-v2",
        },
    }
    try:
        artifact = build_action_artifact(
            name="Recipe vocabulary is data",
            spec=spec,
            publisher={"recipe": "newsroom style guide"},
            dataset={"metadata": {"recipe_version": "curation-v3"}},
            checks=[
                {
                    "name": "eval.fixture",
                    "status": "passed",
                    "evidence": {"recipe": {"score": 1}, "recipe_version": "eval-v1"},
                },
            ],
            now="2026-08-29T00:00:00Z",
        )
        stored = ActionRegistryStore(tmp_path).publish(artifact)
    finally:
        unregister_recipe(kind)
    assert stored["spec"]["params"] == spec["params"]
    assert stored["publisher"]["recipe"] == "newsroom style guide"
    assert stored["receipts"][0]["dataset"]["metadata"] == {
        "recipe_version": "curation-v3"
    }
    assert stored["receipts"][0]["checks"][-1]["evidence"] == {
        "recipe": {"score": 1},
        "recipe_version": "eval-v1",
    }


@pytest.mark.parametrize(
    "action_kind", ["map.mcp_extract", "map.python", "research.answer"]
)
def test_action_registry_rejects_unsafe_canonical_action_publish(tmp_path, action_kind):
    client = _client(tmp_path)
    unsafe = client.post(
        "/api/actions/v1/registry/artifacts",
        json={
            "name": "Do not share code",
            "spec": {
                "action_kind": action_kind,
                "action_name": "Python action",
                "input_columns": ["story"],
                "code": "result = open('/etc/passwd').read()",
                "output_fields": [{"name": "leak", "type": "text"}],
            },
            "dataset": {"name": "unsafe", "version": "1", "row_count": 1},
        },
    )
    assert unsafe.status_code == 400
    assert _recipe_hits(unsafe.json()) == []
    assert "not importable through the shared registry" in unsafe.text


@pytest.mark.parametrize(
    "action_kind", ["classify", "regex_extract", "python", "agent"]
)
def test_action_registry_rejects_noncanonical_implementation_ids(tmp_path, action_kind):
    service = ActionRegistryService(_client(tmp_path).app.state.workspace)
    with pytest.raises(ActionRegistryRouteError, match="not canonical"):
        service.publish_artifact(
            saved_action_id=None,
            spec={
                "action_kind": action_kind,
                "input_columns": ["story"],
                "output_fields": [{"name": "result", "type": "text"}],
            },
            name="Removed implementation id",
            description="",
            publisher={},
            dataset={},
            checks=[],
        )


def test_two_processes_concurrently_publish_same_artifact_distinct_receipts(
    tmp_path,
):
    """Two independent processes call ActionRegistryStore.publish for the
    SAME artifact (identical name/spec/publisher -> identical artifact_id)
    with DISTINCT receipts at once. Both must exit zero, and the final
    on-disk artifact must carry both receipts and be valid JSON -- this
    proves the publish-path filelock.FileLock covers the full read/merge/
    temp-write/replace transaction across processes,
    not merely a same-process race."""
    registry_root = tmp_path / "workspace"
    code = (
        "import json, sys\n"
        "from frisket.authoring.recipe_registry import ActionRegistryStore, build_action_artifact\n"
        "spec = {\n"
        "    'action_kind': 'map.classify',\n"
        "    'action_name': 'Local beat classifier',\n"
        "    'params': {\n"
        "        'source': ['story'],\n"
        "        'engine': 'llm',\n"
        "        'model': 'anthropic/claude-haiku-4-5',\n"
        "        'fields': [\n"
        "            {'name': 'beat', 'type': 'category', 'labels': ['city', 'courts']}\n"
        "        ],\n"
        "    },\n"
        "}\n"
        "artifact = build_action_artifact(\n"
        "    name='Concurrent classifier',\n"
        "    spec=spec,\n"
        "    publisher={'name': 'cold-open-test'},\n"
        "    checks=[{'name': f'proc.{sys.argv[2]}', 'status': 'passed', 'evidence': {}}],\n"
        ")\n"
        "store = ActionRegistryStore(__import__('pathlib').Path(sys.argv[1]))\n"
        "published = store.publish(artifact)\n"
        "print(published['artifact_id'])\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(registry_root), str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    results = [p.communicate(timeout=30) for p in processes]
    assert [p.returncode for p in processes] == [0, 0], results
    artifact_ids = {stdout.strip() for stdout, _stderr in results}
    assert len(artifact_ids) == 1, "both processes must publish the same artifact_id"
    (artifact_id,) = artifact_ids

    store_path = registry_root / "action_registry" / "artifacts" / f"{artifact_id}.json"
    on_disk = json.loads(store_path.read_text())
    assert len(on_disk["receipts"]) == 2
    receipt_check_names = {
        check["name"]
        for receipt in on_disk["receipts"]
        for check in receipt["checks"]
        if check["name"].startswith("proc.")
    }
    assert receipt_check_names == {"proc.0", "proc.1"}

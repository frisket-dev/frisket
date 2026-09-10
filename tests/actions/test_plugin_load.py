from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    PluginManifestLoader,
    PluginManifestRecord,
)
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.plugin_load_action import run_typed_plugin_load_action
from frisket.engine.store import Project
from tests.authoring.test_plugin_load_executor import (
    _plugin_load_action,
    _write_manifest,
)


class CustomParams(ActionParams):
    folder: str
    behavior: str = "copy"


def custom_load(
    params: CustomParams, manifests: PluginManifestLoader
) -> PluginManifestRecord:
    if params.behavior == "no_call":
        return PluginManifestRecord(
            plugin_id="invented",
            version="0",
            manifest_sha256="fake",
            byte_count=999,
            contributes={},
        )
    result = manifests.load(str(Path(params.folder) / "plugin.json"))
    if params.behavior == "double_call":
        try:
            manifests.load(str(Path(params.folder) / "plugin.json"))
        except ValueError:
            pass
    # The annotated return is descriptive, not authority over receipt facts.
    return result.model_copy(update={"plugin_id": "invented", "byte_count": 999})


def _bound(folder: Path, behavior: str = "copy") -> BoundTypedActionRequest:
    definition = action(
        name="record",
        title="Record local manifest",
        description="Load one derived manifest path.",
        category=ActionCategory.SOURCES,
        run=custom_load,
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(definition,)),)
    ).get("custom.record")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.record",
            scope={"kind": "project"},
            params={"folder": str(folder), "behavior": behavior},
            idempotency_key="custom-plugin",
        ),
    )


def test_reused_loader_records_actual_path_and_facts_not_returned_claims(tmp_path):
    manifest = _write_manifest(tmp_path)
    project = Project.create(tmp_path / "project")
    try:
        bound = _bound(manifest.parent)
        first = run_typed_plugin_load_action(project, "p", bound)
        assert first.status == "completed", first.errors
        ref = first.outputs[0].ref
        assert ref["path"] == str(manifest)
        assert ref["plugin_id"] == "frisket-ndjson"
        assert ref["byte_count"] == manifest.stat().st_size
        assert ref["package_sha256"] == ref["package_identity"]["package_sha256"]
        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
            ).fetchone()[0]
        )
        assert receipt["action_kind"] == "custom.record"
        assert receipt["outputs"][0]["ref"] == ref
        assert receipt["evidence"][0]["ref"] == ref
        manifest.unlink()
        replay = run_typed_plugin_load_action(project, "p", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
    finally:
        project.close()


@pytest.mark.parametrize("behavior", ["no_call", "double_call"])
def test_reused_loader_requires_exactly_one_successful_call(tmp_path, behavior):
    manifest = _write_manifest(tmp_path)
    project = Project.create(tmp_path / "project")
    try:
        result = run_typed_plugin_load_action(
            project, "p", _bound(manifest.parent, behavior)
        )
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert result.receipt_id is None
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0
    finally:
        project.close()


@pytest.mark.parametrize(
    "manifest",
    [
        {"kind": "local_file", "path": " "},
        {"kind": "local_file", "path": 123},
        {"kind": "url", "path": "https://example.test/plugin.json"},
        {"kind": "local_file", "path": "plugin.json", "sha256": "claimed"},
    ],
)
def test_manifest_params_refuse_malformed_or_claimed_source_facts(manifest):
    request = _plugin_load_action(Path("unused"))
    request["params"]["manifest"] = manifest
    result = validate_root_action(request)
    assert result.ok is False
    assert result.error.code == "invalid_action_request"


def test_missing_manifest_replay_requires_identical_typed_request(tmp_path):
    manifest = _write_manifest(tmp_path)
    project = Project.create(tmp_path / "project")
    try:
        first = run_action_spec(project, _plugin_load_action(manifest), project_id="p")
        assert first.status == "completed", first.errors
        manifest.unlink()
        changed = run_action_spec(
            project,
            _plugin_load_action(manifest.with_name("different.json")),
            project_id="p",
        )
        assert changed.status == "failed"
        assert changed.errors[0].code == "idempotency_conflict"
        legacy = run_action_spec(
            project,
            {
                "schema_version": "frisket.action.v2",
                "kind": "plugin.load",
                "capabilities": ["project:write", "plugin:load"],
                "params": {"manifest": {"kind": "local_file", "path": str(manifest)}},
                "idempotency_key": "plugin_load@sha256:v1",
            },
            project_id="p",
        )
        assert legacy.status == "failed"
        assert legacy.errors[0].code == "invalid_action_request"
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    finally:
        project.close()


@pytest.mark.parametrize(
    "changed_file", ["plugin.json", "helper.py", "workbench-descriptors.json"]
)
def test_present_package_bytes_are_revalidated_on_replay(tmp_path, changed_file):
    manifest = _write_manifest(tmp_path, plugin_id="demo.command")
    document = json.loads(manifest.read_text())
    document["contributes"]["workbench_commands"] = ["demo.command.command.hello"]
    manifest.write_text(json.dumps(document))
    helper = manifest.parent / "helper.py"
    helper.write_text("TOKEN = 'one'\n")
    descriptor = manifest.parent / "workbench-descriptors.json"
    descriptor.write_bytes(
        (
            Path(__file__).parents[1]
            / "fixtures/local_plugins/demo_command/workbench-descriptors.json"
        ).read_bytes()
    )
    project = Project.create(tmp_path / "project")
    try:
        request = _plugin_load_action(manifest)
        first = run_action_spec(project, request, project_id="p")
        assert first.status == "completed", first.errors
        ref = first.outputs[0].ref
        assert ref["workbench_descriptor_package"]["descriptor_manifests"][0]["id"] == (
            "demo.command.command.hello"
        )
        assert {item["path"] for item in ref["package_identity"]["files"]} == {
            "plugin.json",
            "helper.py",
            "workbench-descriptors.json",
        }
        target = manifest.parent / changed_file
        target.write_text(target.read_text() + "\n")
        result = run_action_spec(project, request, project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "idempotency_conflict"
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    finally:
        project.close()


@pytest.mark.parametrize("changed", [False, True])
def test_transactional_duplicate_key_recheck_uses_package_replay_policy(
    tmp_path, monkeypatch, changed
):
    from frisket.engine.executor import action_lifecycle

    manifest = _write_manifest(tmp_path)
    project = Project.create(tmp_path / "project")
    try:
        request = _plugin_load_action(manifest)
        winner = run_action_spec(project, request, project_id="p")
        assert winner.status == "completed", winner.errors
        if changed:
            manifest.write_text(manifest.read_text() + "\n")
        lookup = action_lifecycle._receipt_for_idempotency
        transactions = []

        def race(target, key):
            transactions.append(target.db.in_transaction)
            return None if len(transactions) == 1 else lookup(target, key)

        monkeypatch.setattr(action_lifecycle, "_receipt_for_idempotency", race)
        result = run_action_spec(project, request, project_id="p")
        assert transactions == [False, True]
        if changed:
            assert result.status == "failed"
            assert result.errors[0].code == "idempotency_conflict"
        else:
            assert result.status == "completed", result.errors
            assert result.receipt_id == winner.receipt_id
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        assert not project.db.in_transaction
    finally:
        project.close()


def test_receipt_insertion_failure_rolls_back_without_activation(tmp_path, monkeypatch):
    from frisket.authoring.plugin_registry import default_registry
    from frisket.engine.store.receipts import ReceiptStore

    manifest = _write_manifest(tmp_path)
    project = Project.create(tmp_path / "project")
    before = default_registry().plugin_manifests()
    try:
        insert = ReceiptStore.insert_completed

        def fail_after_insert(self, receipt, *, commit=True):
            assert commit is False
            insert(self, receipt, commit=commit)
            raise RuntimeError("private fault details")

        monkeypatch.setattr(ReceiptStore, "insert_completed", fail_after_insert)
        result = run_action_spec(project, _plugin_load_action(manifest), project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert "private fault" not in result.errors[0].message
        assert result.receipt_id is None
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert default_registry().plugin_manifests() == before
        assert not project.db.in_transaction
    finally:
        project.close()


@pytest.mark.parametrize("transport", ["cli", "mcp", "http"])
def test_generic_transport_runs_canonical_plugin_load(tmp_path, capsys, transport):
    manifest = _write_manifest(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_path = workspace / "demo.frisket"
    Project.create(project_path).close()
    request = _plugin_load_action(manifest)
    if transport == "cli":
        from frisket.cli.action import action as cli_action

        request_file = tmp_path / "request.json"
        request_file.write_text(json.dumps(request))
        assert (
            cli_action(
                [
                    "run",
                    "--project",
                    str(project_path),
                    "--project-id",
                    "demo",
                    str(request_file),
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
    elif transport == "mcp":
        from frisket.server.mcp import LocalBackend
        from tests.server.test_mcp_server import call

        backend = LocalBackend(workspace)
        try:
            result = call(
                backend, "run_action", {"project_id": "demo", "action": request}
            )
            assert result["done"] is True
        finally:
            backend.ws.get("demo").close()
    else:
        from fastapi.testclient import TestClient
        from frisket.server.app import create_app

        with TestClient(create_app(workspace)) as client:
            response = client.post("/api/projects/demo/actions/v1/run", json=request)
            assert response.status_code == 200, response.text
            result = response.json()
    assert result["status"] == "completed", result
    assert result.get("run_id") is None
    assert result["receipt_id"]
    assert result["outputs"][0]["ref"]["plugin_id"] == "frisket-ndjson"
    project = Project(project_path)
    try:
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()

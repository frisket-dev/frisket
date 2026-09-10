from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.engine.jobs import HandlerRegistry, JobHandlerContext
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.server.app import create_app


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_ID = "frisket-handler-demo"
JOB_KIND = "frisket.handler_demo.job"
HANDLER_KEY = f"{PLUGIN_ID}:handler_demo.echo"
UNDECLARED_HANDLER_KEY = "frisket-undeclared-handler-kind:handler_demo.echo"


def _trusted_handler(payload: dict[str, Any]) -> dict[str, Any]:
    return {"handled": True, "payload": payload}


def _write_manifest(
    tmp_path: Path,
    *,
    plugin_id: str = PLUGIN_ID,
    job_kind: str = JOB_KIND,
    handler_key: str = HANDLER_KEY,
) -> Path:
    package_dir = tmp_path / plugin_id
    package_dir.mkdir(parents=True, exist_ok=True)
    path = package_dir / "plugin.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": plugin_id,
                "version": "0.1.0",
                "contributes": {
                    "actions": [],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [job_kind],
                },
                "requires": {
                    "capabilities": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                    "secrets": [],
                },
                "runtime": {
                    "job_handlers": [{"kind": job_kind, "handler_key": handler_key}]
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _plugin_load_action(manifest_path: Path) -> dict[str, Any]:
    return {
        "action_id": "plugin.load",
        "scope": {"kind": "project"},
        "params": {"manifest": {"kind": "local_file", "path": str(manifest_path)}},
        "idempotency_key": f"trusted-job-handler-bindings@{manifest_path.name}",
    }


def _load_and_enable(
    client: TestClient, project_id: str, manifest_path: Path, *, plugin_id: str
) -> tuple[str, str]:
    # install-local writes the workspace catalog identity that /activate
    # reads (a raw plugin.load action no longer populates it).
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = str(installed.json()["receiptId"])

    activation_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activation_response.status_code == 200, activation_response.text
    return receipt_id, activation_response.json()["manifestSha256"]


def test_trusted_backend_activation_binds_declared_job_handler_without_package_loading(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    register_trusted_backend_handler(HANDLER_KEY, _trusted_handler)
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Trusted job handler bindings"}
        ).json()["id"]
        manifest_path = _write_manifest(tmp_path)
        receipt_id, manifest_sha = _load_and_enable(
            client, project_id, manifest_path, plugin_id=PLUGIN_ID
        )

        metadata_only = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert metadata_only.status_code == 200, metadata_only.text
        assert metadata_only.json()["executableHandlersRegistered"] is False
        assert metadata_only.json()["registeredExecutableHandlers"] == {
            "jobHandlers": []
        }
        metadata_spec = next(
            spec
            for spec in default_registry().job_handler_specs()
            if spec.kind == JOB_KIND
        )
        assert metadata_spec.handler is None

        executable = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert executable.status_code == 200, executable.text
        body = executable.json()
        assert body["schemaVersion"] == "frisket.workbench_plugin_backend_activation.v1"
        assert body["projectId"] == project_id
        assert body["pluginId"] == PLUGIN_ID
        assert body["receiptId"] == receipt_id
        assert body["manifestSha256"] == manifest_sha
        assert body["arbitraryPackageLoadAllowed"] is False
        assert body["executableHandlersRegistered"] is True
        assert body["registeredExecutableHandlers"] == {"jobHandlers": [JOB_KIND]}

        worker_handlers = HandlerRegistry()
        default_registry().install_job_handlers(worker_handlers)
        handler = worker_handlers.get(JOB_KIND)
        assert handler is not None
        assert handler({"row": 7}, JobHandlerContext.without_job_row()) == {
            "handled": True,
            "payload": {"row": 7},
        }
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_trusted_backend_activation_rejects_unknown_or_undeclared_handler_bindings(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Rejected handler bindings"}
        ).json()["id"]

        unknown_key_manifest = _write_manifest(
            tmp_path,
            plugin_id="frisket-unknown-handler-key",
            job_kind="frisket.unknown.job",
            handler_key="frisket-unknown-handler-key:no_such_key",
        )
        _load_and_enable(
            client,
            project_id,
            unknown_key_manifest,
            plugin_id="frisket-unknown-handler-key",
        )
        unknown_key = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-unknown-handler-key/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert unknown_key.status_code == 409, unknown_key.text
        assert (
            unknown_key.json()["detail"]["code"]
            == "plugin_backend_activation_handler_key_unknown"
        )

        register_trusted_backend_handler(UNDECLARED_HANDLER_KEY, _trusted_handler)
        undeclared_manifest = _write_manifest(
            tmp_path,
            plugin_id="frisket-undeclared-handler-kind",
            job_kind="frisket.declared.job",
            handler_key=UNDECLARED_HANDLER_KEY,
        )
        payload = json.loads(undeclared_manifest.read_text(encoding="utf-8"))
        payload["runtime"]["job_handlers"][0]["kind"] = "frisket.not_declared.job"
        undeclared_manifest.write_text(json.dumps(payload), encoding="utf-8")
        _load_and_enable(
            client,
            project_id,
            undeclared_manifest,
            plugin_id="frisket-undeclared-handler-kind",
        )
        undeclared = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-undeclared-handler-kind/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert undeclared.status_code == 409, undeclared.text
        assert (
            undeclared.json()["detail"]["code"]
            == "plugin_backend_activation_handler_binding_invalid"
        )
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        unregister_trusted_backend_handler(UNDECLARED_HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_runtime_handler_bindings_require_trusted_capability_and_namespaced_keys(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    with pytest.raises(ValueError, match="namespaced"):
        register_trusted_backend_handler("not-namespaced", _trusted_handler)
    for invalid_prefix in [
        "Frisket.Geo",
        "frisket geo",
        "frisket/geo",
        "frisket..geo",
        ".frisket.geo",
        "frisket.geo.",
        "-frisket",
        "frisket-",
        "frisket._geo",
    ]:
        with pytest.raises(ValueError, match="canonical plugin id"):
            register_trusted_backend_handler(
                f"{invalid_prefix}:handler_demo.echo", _trusted_handler
            )

    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post(
        "/api/projects", json={"name": "Runtime binding capability gate"}
    ).json()["id"]
    manifest_path = _write_manifest(tmp_path, plugin_id="frisket-no-runtime-capability")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["requires"]["capabilities"] = []
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    load_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_plugin_load_action(manifest_path),
    )
    assert load_response.status_code == 400, load_response.text
    load_result = ActionResult.model_validate(load_response.json())
    assert load_result.status == "failed"
    assert len(load_result.errors) == 1
    assert load_result.errors[0].code == "invalid_plugin_manifest"
    assert load_result.errors[0].field == "params.manifest.runtime.job_handlers"

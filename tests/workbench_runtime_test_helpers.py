from __future__ import annotations

import json
from pathlib import Path

from frisket.engine.store import Project
from frisket.authoring.workbench.plugin_runtime import (
    activate_workbench_plugin_manifest,
    execute_workbench_plugin_local_install_plan,
)


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


def write_runtime_plugin_manifest(
    tmp_path: Path,
    *,
    plugin_id: str,
    runtime_bindings: dict[str, dict[str, str]],
    column_types: list[str] | None = None,
) -> Path:
    # One package per directory: package identity hashes every
    # file in the manifest's parent, so sibling manifests must not share a dir.
    package_dir = tmp_path / plugin_id
    package_dir.mkdir(parents=True, exist_ok=True)
    path = package_dir / "plugin.json"
    normalized_bindings = {
        binding_type: dict(runtime_bindings.get(binding_type, {}))
        for binding_type in ("actions", "importers", "operators", "projections")
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": plugin_id,
                "version": "0.1.0",
                "contributes": {
                    "actions": sorted(normalized_bindings["actions"]),
                    "importers": sorted(normalized_bindings["importers"]),
                    "operators": sorted(normalized_bindings["operators"]),
                    "projections": sorted(normalized_bindings["projections"]),
                    "column_types": sorted(column_types or []),
                    "job_handlers": [],
                },
                "requires": {
                    "capabilities": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                    "secrets": [],
                },
                "runtime": {
                    binding_type: [
                        {"kind": kind, "handler_key": handler_key}
                        for kind, handler_key in sorted(bindings.items())
                    ]
                    for binding_type, bindings in normalized_bindings.items()
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def activate_runtime_plugin_for_project(
    project: Project,
    tmp_path: Path,
    *,
    plugin_id: str,
    runtime_bindings: dict[str, dict[str, str]],
    project_id: str,
    column_types: list[str] | None = None,
) -> tuple[str, str]:
    manifest_path = write_runtime_plugin_manifest(
        tmp_path,
        plugin_id=plugin_id,
        runtime_bindings=runtime_bindings,
        column_types=column_types,
    )
    # Install through the trusted-local plan so the workspace catalog holds the
    # package identity that activation reads.
    status, payload = execute_workbench_plugin_local_install_plan(
        project,
        project_id=project_id,
        plugin_id=plugin_id,
        source={"kind": "localPath", "value": str(manifest_path)},
        arbitrary_package_load_allowed=False,
    )
    assert status == 200, payload
    receipt_id = str(payload["receiptId"])

    activation = activate_workbench_plugin_manifest(
        project,
        project_id=project_id,
        plugin_id=plugin_id,
        receipt_id=receipt_id,
        trust_acknowledged=True,
        permissions_accepted=[TRUSTED_LOCAL_BACKEND_CAPABILITY],
        arbitrary_package_load_allowed=False,
    )
    return receipt_id, str(activation["manifestSha256"])

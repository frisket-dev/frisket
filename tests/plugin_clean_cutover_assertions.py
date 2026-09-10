from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.plugin import PluginManifest
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"

# Literal obsolete fixture data, deliberately NOT a product constant: this
# file asserts the cutover removed the generated-map materialization surface,
# so it must keep describing that surface after the constant naming it is
# gone.
_OBSOLETE_GENERATED_RESULT_SCHEMA_VERSION = "frisket.plugin_generated_result_schema.v1"

# A real builtin action id, so the shadowing assertion below exercises the
# gate rather than a name that merely looks builtin.
_SHADOWED_BUILTIN_ACTION_ID = "map.clean_dates"


def assert_internal_bootstrap_route_removed(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    routes = [
        route.path
        for route in client.app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
    ]
    assert (
        "/api/projects/{pid}/workbench/internal-plugins/{plugin_id}/bootstrap"
        not in routes
    )

    response = client.post(
        "/api/projects/project-clean-cutover/workbench/internal-plugins/"
        "frisket.text_tools/bootstrap"
    )
    assert response.status_code == 404


def assert_bundled_internal_source_rejected(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "clean cutover"}).json()[
        "id"
    ]
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/frisket.text_tools/install-local",
        json={
            "source": {"kind": "bundled_internal", "pluginId": "frisket.text_tools"},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["installFailure"]["code"] == "plugin_install_source_invalid"


def _legacy_manifest(extra_action_fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "frisket.plugin.v1",
        "id": "clean.cutover.legacy",
        "version": "0.1.0",
        "contributes": {"actions": ["clean.cutover.legacy.action"]},
        "requires": {"capabilities": [TRUSTED_LOCAL_BACKEND_CAPABILITY]},
        "runtime": {
            "actions": [
                {
                    "kind": "clean.cutover.legacy.action",
                    "handler_key": "clean.cutover.legacy:action",
                    "handler_api": "plugin_action",
                    "module_path": "plugin.py",
                    **extra_action_fields,
                }
            ]
        },
    }


def assert_internal_shadow_manifest_field_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        PluginManifest.model_validate(_legacy_manifest({"internal_shadow": True}))
    assert "internal_shadow" in str(caught.value)


def assert_op_declaration_materialization_rejected() -> None:
    base_materialization = {
        "mode": "generated_map_results",
        "schema_version": _OBSOLETE_GENERATED_RESULT_SCHEMA_VERSION,
        "output_schema": {
            "columns": [
                {
                    "key": "value",
                    "type": "text",
                    "name_param": "output_name",
                    "primary": True,
                }
            ]
        },
    }
    for declared_value in (
        "frisket.sdk.ops.clean_dates:CLEAN_DATES_OP",
        None,
    ):
        with pytest.raises(ValidationError) as caught:
            PluginManifest.model_validate(
                _legacy_manifest(
                    {
                        "materialization": {
                            **base_materialization,
                            "op_declaration": declared_value,
                        }
                    }
                )
            )
        # Before the cutover the block itself was legal and only
        # `op_declaration` was refused; after it, the whole materialization
        # key is unknown. Either way the manifest is rejected and the error
        # names the obsolete surface.
        message = str(caught.value)
        assert "op_declaration" in message or "materialization" in message


def assert_public_action_collision_rejected(tmp_path: Path) -> None:
    _reset_default_registry_for_tests()
    try:
        # A well-formed NATIVE package whose manifest claims a builtin action
        # id. Everything else about it is legal, so install and activate both
        # succeed and the collision gate is the thing that refuses it.
        plugin_root = tmp_path / "public_collision_plugin"
        plugin_root.mkdir()
        (plugin_root / "plugin.py").write_text(
            "from __future__ import annotations\n"
            "from pydantic import BaseModel\n"
            "from frisket.actions.core import ActionCategory, action, map_rows\n"
            "from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult\n"
            "from frisket.plugins.sdk import Plugin\n"
            "class Params(ActionParams):\n"
            "    source: ColumnRef[str]\n"
            "class Output(BaseModel):\n"
            "    cleaned: str | None\n"
            "def clean_dates(params: Params, row: Row) -> RowResult[Output]:\n"
            "    return RowResult(output=Output(cleaned=params.source.read(row)))\n"
            "CLEAN = action(name='clean_dates', title='Clean dates',\n"
            "    description='Clean dates.', category=ActionCategory.TEXT,\n"
            "    run=map_rows(clean_dates))\n"
            "plugin = Plugin(id='clean.cutover.public_collision', version='0.1.0',\n"
            f"    capabilities=[{TRUSTED_LOCAL_BACKEND_CAPABILITY!r}], actions=(CLEAN,))\n",
            encoding="utf-8",
        )
        from frisket.plugins.manifest_generate import (
            load_generation_source,
            manifest_dict_from_plugin,
        )

        manifest = manifest_dict_from_plugin(load_generation_source(plugin_root))
        # Retarget the one binding at a builtin id, keeping the manifest
        # internally consistent so nothing earlier can reject it first.
        manifest["contributes"]["actions"] = [_SHADOWED_BUILTIN_ACTION_ID]
        binding = manifest["runtime"]["actions"][0]
        binding["kind"] = _SHADOWED_BUILTIN_ACTION_ID
        binding["catalog_entry"]["kind"] = _SHADOWED_BUILTIN_ACTION_ID
        (plugin_root / "plugin.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "public action collision"}
        ).json()["id"]
        installed = client.post(
            f"/api/projects/{project_id}/workbench/plugins/"
            "clean.cutover.public_collision/install-local",
            json={
                "source": {"kind": "localPath", "value": str(plugin_root)},
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert installed.status_code == 200, installed.text
        receipt_id = installed.json()["receiptId"]

        activated = client.post(
            f"/api/projects/{project_id}/workbench/plugins/"
            "clean.cutover.public_collision/activate",
            json={
                "receiptId": receipt_id,
                "trustAcknowledged": True,
                "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert activated.status_code == 200, activated.text

        backend = client.post(
            f"/api/projects/{project_id}/workbench/plugins/"
            "clean.cutover.public_collision/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert backend.status_code == 409, backend.text
        body = backend.json()
        code = body.get("code")
        detail = body.get("detail")
        if code is None and isinstance(detail, dict):
            code = detail.get("code")
        assert code == "plugin_backend_activation_public_action_collision"
    finally:
        _reset_default_registry_for_tests()

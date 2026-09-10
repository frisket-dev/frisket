"""HTTP/OpenAPI coverage for the seven typed workbench plugin operations.

A REAL lifecycle chain (install → activate → backend activate → settings
get/patch → disable → uninstall) through the live app is the no-pruning
proof: every response's exact wire key order is asserted (``response.json()``
preserves it), and the consent/trust facts are asserted by value at each
hop. The raw component-module and marketplace routes must not grow schemas.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.authoring.workbench import plugin_runtime_shared
from frisket.contracts.http import models as contracts
from frisket.contracts.http.models import WorkbenchPluginSettings
from frisket.server.app import create_app


PLUGIN_ID = "demo.env_settings"
PLUGIN_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "demo_env_settings"
)
TRUST_CAPABILITY = "plugin:trusted_local_backend"

INSTALL_EXECUTION_KEYS = [
    "schemaVersion",
    "projectId",
    "pluginId",
    "source",
    "installState",
    "activation",
    "runtimeSource",
    "receiptId",
    "manifestSha256",
    "packageSha256",
    "arbitraryPackageLoadAllowed",
    "installFailure",
    "layoutMutated",
]
ACTIVATION_KEYS = [
    "schemaVersion",
    "projectId",
    "pluginId",
    "receiptId",
    "manifestSha256",
    "packageSha256",
    "runtimeSource",
    "activation",
    "installState",
    "registryActivated",
    "arbitraryPackageLoadAllowed",
    "permissionsAccepted",
    "registeredPluginManifests",
]
BACKEND_ACTIVATION_KEYS = [
    "schemaVersion",
    "projectId",
    "pluginId",
    "receiptId",
    "manifestSha256",
    "packageSha256",
    "runtimeSource",
    "arbitraryPackageLoadAllowed",
    "executableHandlersRegistered",
    "trustedRuntimeBindingsRegistered",
    "registeredBackendContributions",
    "registeredExecutableHandlers",
    "registeredRuntimeBindings",
]
INSTALL_STATE_KEYS = [
    "schemaVersion",
    "projectId",
    "pluginId",
    "receiptId",
    "manifestSha256",
    "packageSha256",
    "installState",
    "activation",
    "runtimeSource",
    "permissionsAccepted",
    "registryActivated",
    "arbitraryPackageLoadAllowed",
    "disabledReason",
    "source",
    "installFailure",
]
SETTINGS_ENVELOPE_KEYS = [
    "schemaVersion",
    "projectId",
    "pluginId",
    "canMutate",
    "settings",
]
SETTING_OPTION_KEYS = [
    "id",
    "title",
    "type",
    "description",
    "defaultValue",
    "effectiveValue",
    "source",
    "enum",
    "min",
    "max",
    "readOnly",
]


def _client(tmp_path) -> tuple[TestClient, str]:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Plugin lifecycle"}).json()["id"]
    return client, pid


def _plugin_url(pid: str, tail: str) -> str:
    return f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/{tail}"


DESCRIPTOR_TAIL_KEYS = ["workbenchDescriptorPackage", "workbenchDescriptorManifests"]


def _assert_install_execution_keys(payload: dict) -> None:
    keys = list(payload.keys())
    assert keys[: len(INSTALL_EXECUTION_KEYS)] == INSTALL_EXECUTION_KEYS
    # The producer appends both descriptor keys together, in this order, or
    # neither — the tail is ordered wire truth, not an unordered grab-bag.
    assert keys[len(INSTALL_EXECUTION_KEYS) :] in ([], DESCRIPTOR_TAIL_KEYS)


def test_contract_schema_literals_match_producers() -> None:
    assert (
        contracts.WORKBENCH_PLUGIN_ACTIVATION_SCHEMA_VERSION
        == plugin_runtime_shared.PLUGIN_ACTIVATION_SCHEMA_VERSION
    )
    assert (
        contracts.WORKBENCH_PLUGIN_BACKEND_ACTIVATION_SCHEMA_VERSION
        == plugin_runtime_shared.PLUGIN_BACKEND_ACTIVATION_SCHEMA_VERSION
    )
    assert (
        contracts.WORKBENCH_PLUGIN_INSTALL_PLAN_EXECUTION_SCHEMA_VERSION
        == plugin_runtime_shared.PLUGIN_INSTALL_PLAN_EXECUTION_SCHEMA_VERSION
    )


def test_lifecycle_chain_preserves_transport_shape_and_trust_facts(tmp_path) -> None:
    client, pid = _client(tmp_path)
    source = {"kind": "localPath", "value": str(PLUGIN_ROOT)}

    installed = client.post(
        _plugin_url(pid, "install-local"),
        json={"source": source, "arbitraryPackageLoadAllowed": False},
    )
    assert installed.status_code == 200, installed.text
    install_body = installed.json()
    _assert_install_execution_keys(install_body)
    assert install_body["schemaVersion"] == "frisket.plugin_install_plan_execution.v1"
    assert install_body["installState"] == "installed"
    assert install_body["activation"] == "manifestLoaded"
    assert install_body["source"] == source
    assert install_body["arbitraryPackageLoadAllowed"] is False
    assert install_body["installFailure"] is None
    receipt_id = install_body["receiptId"]
    assert receipt_id

    refused = client.post(
        _plugin_url(pid, "activate"),
        json={"receiptId": receipt_id, "trustAcknowledged": False},
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "plugin_activation_trust_required"

    activated = client.post(
        _plugin_url(pid, "activate"),
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUST_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text
    activation_body = activated.json()
    assert list(activation_body.keys()) == ACTIVATION_KEYS
    assert activation_body["schemaVersion"] == "frisket.workbench_plugin_activation.v1"
    assert activation_body["receiptId"] == receipt_id
    assert activation_body["installState"] == "enabled"
    assert activation_body["registryActivated"] is True
    assert activation_body["arbitraryPackageLoadAllowed"] is False
    assert activation_body["permissionsAccepted"] == [TRUST_CAPABILITY]
    assert activation_body["registeredPluginManifests"] == [PLUGIN_ID]

    backend = client.post(
        _plugin_url(pid, "backend/activate"),
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": False,
        },
    )
    assert backend.status_code == 200, backend.text
    backend_body = backend.json()
    assert list(backend_body.keys()) == BACKEND_ACTIVATION_KEYS
    assert (
        backend_body["schemaVersion"]
        == "frisket.workbench_plugin_backend_activation.v1"
    )
    assert backend_body["arbitraryPackageLoadAllowed"] is False
    assert backend_body["executableHandlersRegistered"] is False

    settings = client.get(_plugin_url(pid, "settings"))
    assert settings.status_code == 200, settings.text
    settings_body = settings.json()
    assert list(settings_body.keys()) == SETTINGS_ENVELOPE_KEYS
    assert settings_body["schemaVersion"] == "frisket.workbench_plugin_settings.v1"
    assert settings_body["canMutate"] is True
    assert settings_body["settings"], "fixture plugin declares settings"
    for option in settings_body["settings"]:
        assert list(option.keys()) == SETTING_OPTION_KEYS

    patched = client.patch(
        _plugin_url(pid, "settings"),
        json={"values": {f"{PLUGIN_ID}.mode": "detail"}},
    )
    assert patched.status_code == 200, patched.text
    patched_body = patched.json()
    assert list(patched_body.keys()) == SETTINGS_ENVELOPE_KEYS
    mode = {item["id"]: item for item in patched_body["settings"]}[f"{PLUGIN_ID}.mode"]
    assert mode["effectiveValue"] == "detail"
    assert mode["source"] == "project"

    invalid = client.patch(
        _plugin_url(pid, "settings"),
        json={"values": {f"{PLUGIN_ID}.missing": "x"}},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_plugin_settings"
    assert invalid.json()["detail"]["details"]["fields"] == [
        {"id": f"{PLUGIN_ID}.missing", "message": "unknown setting"}
    ]

    disabled = client.post(_plugin_url(pid, "disable"), json={})
    assert disabled.status_code == 200, disabled.text
    disabled_body = disabled.json()
    assert list(disabled_body.keys()) == INSTALL_STATE_KEYS
    assert disabled_body["schemaVersion"] == "frisket.workbench_plugin_install_state.v1"
    assert disabled_body["installState"] == "disabled"
    assert disabled_body["activation"] == "blocked"
    assert disabled_body["disabledReason"] == "plugin_disabled"
    assert disabled_body["permissionsAccepted"] == [TRUST_CAPABILITY]

    uninstalled = client.post(_plugin_url(pid, "uninstall"), json={})
    assert uninstalled.status_code == 200, uninstalled.text
    uninstalled_body = uninstalled.json()
    assert list(uninstalled_body.keys()) == INSTALL_STATE_KEYS
    assert uninstalled_body["installState"] == "uninstalled"
    assert uninstalled_body["activation"] == "removed"

    gone = client.post(_plugin_url(pid, "disable"), json={})
    assert gone.status_code == 404
    assert gone.json()["detail"]["code"] == "plugin_lifecycle_not_installed"


def test_descriptor_install_keeps_ordered_descriptor_tail(tmp_path) -> None:
    """A descriptor-shipping plugin must render the two descriptor keys in
    producer-appended tail position — pinning the optional-field order the
    lifecycle fixture (which ships no descriptors) cannot."""

    client, pid = _client(tmp_path)
    root = (
        Path(__file__).parent.parent
        / "fixtures"
        / "local_plugins"
        / "frisket_geo_smoke"
    )

    installed = client.post(
        f"/api/projects/{pid}/workbench/plugins/frisket.geosmoke/install-local",
        json={
            "source": {"kind": "localPath", "value": str(root)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    keys = list(installed.json().keys())
    assert keys == INSTALL_EXECUTION_KEYS + DESCRIPTOR_TAIL_KEYS


def test_blocked_install_keeps_failure_envelope_and_status(tmp_path) -> None:
    client, pid = _client(tmp_path)

    blocked = client.post(
        _plugin_url(pid, "install-local"),
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": True,
        },
    )
    assert blocked.status_code == 403
    body = blocked.json()
    _assert_install_execution_keys(body)
    assert body["schemaVersion"] == "frisket.plugin_install_plan_execution.v1"
    assert body["installState"] == "failed"
    assert body["arbitraryPackageLoadAllowed"] is False
    assert (
        body["installFailure"]["code"]
        == "plugin_install_arbitrary_package_load_blocked"
    )


def test_unknown_producer_field_errors_loudly_instead_of_pruning() -> None:
    payload = {
        "schemaVersion": "frisket.workbench_plugin_settings.v1",
        "projectId": "p",
        "pluginId": "x",
        "canMutate": True,
        "settings": [],
        "surprise": True,
    }
    with pytest.raises(ValidationError):
        WorkbenchPluginSettings.model_validate(payload)


def test_openapi_declares_typed_operations_and_keeps_raw_routes_raw(tmp_path) -> None:
    client, _pid = _client(tmp_path)
    document = client.app.openapi()
    paths = document["paths"]
    base = "/api/projects/{pid}/workbench/plugins/{plugin_id}"

    typed = {
        (f"{base}/settings", "get", "WorkbenchPluginSettings"),
        (f"{base}/settings", "patch", "WorkbenchPluginSettings"),
        (f"{base}/install-local", "post", "WorkbenchPluginInstallExecution"),
        (f"{base}/activate", "post", "WorkbenchPluginActivation"),
        (f"{base}/backend/activate", "post", "WorkbenchPluginBackendActivation"),
        (f"{base}/disable", "post", "WorkbenchPluginInstallState"),
        (f"{base}/uninstall", "post", "WorkbenchPluginInstallState"),
    }
    for path, method, ref in typed:
        operation = paths[path][method]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{ref}"}, (path, method)

    # Every browser-facing lifecycle operation can pass through the shared
    # session/RBAC gate before its handler. Those reachable 401/403 branches
    # must remain visible to generated clients; handler-specific statuses stay
    # declared alongside them.
    expected_errors = {
        (f"{base}/settings", "get"): {"401", "403", "404", "409", "422", "500"},
        (f"{base}/settings", "patch"): {"401", "403", "404", "409", "422", "500"},
        (f"{base}/install-local", "post"): {"401", "403", "409", "422", "500"},
        (f"{base}/activate", "post"): {"400", "401", "403", "404", "409", "422", "500"},
        (f"{base}/backend/activate", "post"): {"401", "403", "409", "422", "500"},
        (f"{base}/disable", "post"): {"401", "403", "404", "409", "422", "500"},
        (f"{base}/uninstall", "post"): {"401", "403", "404", "409", "422", "500"},
    }
    for (path, method), statuses in expected_errors.items():
        assert statuses <= set(paths[path][method]["responses"]), (path, method)
    install_403 = paths[f"{base}/install-local"]["post"]["responses"]["403"]
    assert install_403["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WorkbenchPluginInstallExecution"
    }

    module_route = paths[f"{base}/frontend-components/{{contribution_id}}/module.js"][
        "get"
    ]
    marketplace = paths["/api/projects/{pid}/workbench/marketplace"]["get"]
    for raw_operation in (module_route, marketplace):
        raw_schema = (
            raw_operation["responses"]["200"]
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        assert "$ref" not in raw_schema

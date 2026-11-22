"""Workbench plugin manifest identity, fixtures, policy, admission, settings, and runtime index tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.authoring.workbench import (
    plugin_runtime,
    plugin_runtime_settings,
    plugin_runtime_status,
)
from frisket.authoring.workbench.contracts import (
    CANONICAL_REGIONS,
    FORBIDDEN_TAB_CONTRACTS,
    LEGAL_HOSTS,
    project_manifest,
)
from frisket.authoring.workbench.plugin_runtime import (
    activate_workbench_plugin_backend_contributions,
    activate_workbench_plugin_manifest,
)
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    enabled_workbench_plugin_ids,
    project_runtime_binding,
)
from frisket.authoring.workbench.plugin_runtime_settings import (
    _loaded_manifest_for_plugin_env,
)
from frisket.authoring.workbench.plugin_runtime_shared import (
    WorkbenchPluginActivationError,
    WorkbenchPluginLifecycleError,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    _effective_required_capabilities,
    _loaded_manifest_from_ref,
    workbench_plugin_frontend_component_module,
    workbench_plugin_runtime_index,
)
from frisket.contracts.plugin import (
    PluginManifestLoadError,
    load_plugin_manifest_file,
)
from frisket.security.secrets import decrypt_secret
from frisket.server.app import create_app

# --- manifest identity ---


def _manifest_identity_write_manifest(
    tmp_path: Path,
    *,
    plugin_id: str,
    version: str = "1.2.3",
    contributes: dict | None = None,
    runtime: dict | None = None,
) -> Path:
    path = tmp_path / "identity.plugin.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": plugin_id,
                "version": version,
                "contributes": contributes
                or {
                    "actions": [],
                    "importers": ["ndjson"],
                    "column_types": [],
                    "job_handlers": [],
                },
                "requires": {
                    "capabilities": [],
                    "secrets": [],
                },
                "runtime": runtime or {},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("plugin_id", "version"),
    [
        ("frisket-ndjson", "0.1.0"),
        ("frisket.geo", "1.2.3"),
        ("acme_geo_tools", "2.0.0-alpha.1"),
        ("acme_geo_tools", "2.0.0-alpha.1+build.7"),
        ("a1.b2-c3_d4", "10.20.30+build.7"),
    ],
)
def test_plugin_manifest_accepts_canonical_identity(
    tmp_path: Path, plugin_id: str, version: str
) -> None:
    loaded = load_plugin_manifest_file(
        _manifest_identity_write_manifest(
            tmp_path, plugin_id=plugin_id, version=version
        )
    )
    assert loaded.manifest.id == plugin_id
    assert loaded.manifest.version == version


@pytest.mark.parametrize(
    "plugin_id",
    [
        "Frisket.Geo",
        "frisket geo",
        "frisket/geo",
        "frisket..geo",
        ".frisket.geo",
        "frisket.geo.",
        "frisket:geo",
        "frisket\ngeo",
        "-frisket",
        "frisket-",
        "frisket._geo",
        "frisket.geo_",
        "frisket.-geo",
        "frisket.geo-",
    ],
)
def test_plugin_manifest_rejects_ambiguous_plugin_ids(
    tmp_path: Path, plugin_id: str
) -> None:
    with pytest.raises(PluginManifestLoadError) as exc_info:
        load_plugin_manifest_file(
            _manifest_identity_write_manifest(tmp_path, plugin_id=plugin_id)
        )
    assert exc_info.value.code == "invalid_plugin_manifest"
    assert "plugin id" in exc_info.value.message


@pytest.mark.parametrize(
    "version",
    [
        "",
        "v1.2.3",
        "1",
        "1.2",
        "1.2.3 beta",
        "1.2.3/beta",
        "1.2.3-01",
        "1.2.3-alpha..1",
        "01.2.3",
        "latest",
        "2026.06.23",
    ],
)
def test_plugin_manifest_rejects_non_semver_versions(
    tmp_path: Path, version: str
) -> None:
    with pytest.raises(PluginManifestLoadError) as exc_info:
        load_plugin_manifest_file(
            _manifest_identity_write_manifest(
                tmp_path, plugin_id="frisket.geo", version=version
            )
        )
    assert exc_info.value.code == "invalid_plugin_manifest"
    assert "plugin version" in exc_info.value.message


def test_plugin_manifest_normalizes_and_rejects_ambiguous_contribution_names(
    tmp_path: Path,
) -> None:
    with pytest.raises(PluginManifestLoadError) as exc_info:
        load_plugin_manifest_file(
            _manifest_identity_write_manifest(
                tmp_path,
                plugin_id="frisket.geo",
                contributes={
                    "actions": ["frisket.demo.action", " frisket.demo.action "],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
            )
        )
    assert exc_info.value.code == "invalid_plugin_manifest"
    assert "duplicate contribution name" in exc_info.value.message

    with pytest.raises(PluginManifestLoadError) as control_exc:
        load_plugin_manifest_file(
            _manifest_identity_write_manifest(
                tmp_path,
                plugin_id="frisket.geo",
                contributes={
                    "actions": ["frisket.demo.\naction"],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
            )
        )
    assert control_exc.value.code == "invalid_plugin_manifest"
    assert "control characters" in control_exc.value.message


def test_plugin_manifest_rejects_ambiguous_runtime_bindings(tmp_path: Path) -> None:
    with pytest.raises(PluginManifestLoadError) as duplicate_key:
        load_plugin_manifest_file(
            _manifest_identity_write_manifest(
                tmp_path,
                plugin_id="frisket.geo",
                contributes={
                    "actions": ["frisket.demo.one", "frisket.demo.two"],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
                runtime={
                    "actions": [
                        {
                            "kind": "frisket.demo.one",
                            "handler_key": "frisket.geo:shared",
                        },
                        {
                            "kind": "frisket.demo.two",
                            "handler_key": "frisket.geo:shared",
                        },
                    ]
                },
            )
        )
    assert duplicate_key.value.code == "invalid_plugin_manifest"
    assert "duplicate runtime binding handler_key" in duplicate_key.value.message

    with pytest.raises(PluginManifestLoadError) as whitespace:
        load_plugin_manifest_file(
            _manifest_identity_write_manifest(
                tmp_path,
                plugin_id="frisket.geo",
                contributes={
                    "actions": ["frisket.demo.action"],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
                runtime={
                    "actions": [
                        {
                            "kind": " frisket.demo.action ",
                            "handler_key": "frisket.geo:action",
                        }
                    ]
                },
            )
        )
    assert whitespace.value.code == "invalid_plugin_manifest"
    assert "surrounding whitespace" in whitespace.value.message


def test_plugin_manifest_derives_runtime_handler_key_from_binding_kind(
    tmp_path: Path,
) -> None:
    loaded = load_plugin_manifest_file(
        _manifest_identity_write_manifest(
            tmp_path,
            plugin_id="demo.weather_forecast",
            contributes={
                "actions": ["demo.weather_forecast.op.forecast_highs"],
                "importers": ["demo.weather_forecast.importer.cases"],
                "column_types": [],
                "job_handlers": [],
            },
            runtime={
                "actions": [{"kind": "demo.weather_forecast.op.forecast_highs"}],
                "importers": [
                    {
                        "kind": "demo.weather_forecast.importer.cases",
                        "handler_api": "plugin_importer",
                    }
                ],
            },
        )
    )

    assert (
        loaded.manifest.runtime.actions[0].handler_key
        == "demo.weather_forecast:forecast_highs"
    )
    assert (
        loaded.manifest.runtime.importers[0].handler_key
        == "demo.weather_forecast:cases"
    )


def test_plugin_manifest_rejects_handler_key_collision_after_derivation(
    tmp_path: Path,
) -> None:
    with pytest.raises(PluginManifestLoadError) as exc_info:
        load_plugin_manifest_file(
            _manifest_identity_write_manifest(
                tmp_path,
                plugin_id="demo.weather_forecast",
                contributes={
                    "actions": [
                        "demo.weather_forecast.op.forecast_highs",
                        "demo.weather_forecast.alt.forecast_highs",
                    ],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
                runtime={
                    "actions": [
                        {"kind": "demo.weather_forecast.op.forecast_highs"},
                        {"kind": "demo.weather_forecast.alt.forecast_highs"},
                    ]
                },
            )
        )

    assert exc_info.value.code == "invalid_plugin_manifest"
    assert "duplicate runtime binding handler_key" in exc_info.value.message


# --- contract fixtures ---

CONTRACT_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "workbench_plugin"
BEHAVIORAL_EXPECTATION_KEYS = {
    "expected",
    "expectedResolved",
    "expectedManifest",
    "expectedMerge",
    "expectedMigration",
    "expectedRepair",
    "expectedErrors",
    "expectedState",
    "expectedActions",
    "expectedLayoutRefs",
}
RUNTIME_ONLY_STATE_KEYS = {"activeRowId", "activeCell", "activeEvidenceLinkId"}


def test_workbench_plugin_fixture_matrix_is_complete() -> None:
    matrix = _fixture_doc("expected_matrix.json")
    documents = [
        _fixture_doc(path.name)
        for path in sorted(CONTRACT_FIXTURES_ROOT.glob("*.json"))
        if path.name != "expected_matrix.json"
    ]
    assert isinstance(matrix, dict)
    assert documents and all(isinstance(document, dict) for document in documents)
    cases: list[dict[str, Any]] = []
    for document in documents:
        assert document.get("schemaVersion") == (
            "frisket.workbench_contract_fixtures.v1"
        )
        assert isinstance(document.get("cases"), list)
        assert all(isinstance(case, dict) for case in document["cases"])
        cases.extend(document["cases"])

    case_ids = [case.get("id") for case in cases]
    assert matrix["schemaVersion"] == "frisket.workbench_contract_matrix.v1"
    assert case_ids and all(
        isinstance(case_id, str) and case_id for case_id in case_ids
    )
    assert len(case_ids) == len(set(case_ids))
    assert set(case_ids) >= set(matrix["requiredCases"])
    assert set(case_ids) >= set(matrix["negativeCases"])
    assert all(BEHAVIORAL_EXPECTATION_KEYS.intersection(case) for case in cases)

    declared_json = json.dumps(cases, sort_keys=True)
    for inventory in ("statusCodes", "canonicalRegions", "forbiddenContributionKinds"):
        assert all(json.dumps(item) in declared_json for item in matrix[inventory])
    assert all(
        json.dumps(reason) in declared_json
        for reason in set(matrix["disabledReasonCodes"]) - {"missing_contribution"}
    )
    assert "missing_contribution" in matrix["disabledReasonCodes"]
    assert set(matrix["canonicalRegions"]) == CANONICAL_REGIONS
    assert set(matrix["forbiddenContributionKinds"]) == FORBIDDEN_TAB_CONTRACTS

    descriptors = load_fixture_descriptors(CONTRACT_FIXTURES_ROOT)
    placements = [
        placement
        for descriptor in descriptors
        for placement in descriptor.get("placements", [])
        if isinstance(placement, dict)
    ]
    assert any(
        isinstance(requirement, dict) and requirement.get("kind") == "contribution"
        for descriptor in descriptors
        for requirement in descriptor.get("requires", [])
    )
    assert all(placement.get("host") in LEGAL_HOSTS for placement in placements)
    persisted_region_hosts = {
        host
        for case in cases
        if case["id"] != "malformed_persisted_layout_migration"
        for key in ("layout", "persisted")
        if isinstance(case.get(key), dict)
        for host in case[key].get("regions", {})
    }
    assert persisted_region_hosts <= CANONICAL_REGIONS
    assert not any(
        descriptor.get("kind") in FORBIDDEN_TAB_CONTRACTS
        or any(
            kind in str(descriptor.get("id", "")) for kind in FORBIDDEN_TAB_CONTRACTS
        )
        for descriptor in descriptors
    )

    for case in cases:
        runtime_state = case.get("runtimeState")
        if not isinstance(runtime_state, dict):
            continue
        persisted_json = json.dumps(
            [case.get("projectProfile", {}), case.get("layout", {})], sort_keys=True
        )
        assert all(
            f'"{key}":' not in persisted_json
            for key in RUNTIME_ONLY_STATE_KEYS.intersection(runtime_state)
        )


def test_workbench_plugin_manifest_projection() -> None:
    descriptors = load_fixture_descriptors(CONTRACT_FIXTURES_ROOT)
    by_id = {descriptor["id"]: descriptor for descriptor in descriptors}

    sources_manifest = project_manifest(by_id["frisket.core.panel.feeds"])
    assert sources_manifest["id"] == "frisket.core.panel.feeds"
    assert "componentKey" not in sources_manifest
    assert sources_manifest["placements"][1]["host"] == "bottomDock"
    assert sources_manifest["placements"][1]["mode"] == "tab"

    command_manifest = project_manifest(by_id["frisket.core.command.open_feeds"])
    assert "handlerKey" not in command_manifest
    assert command_manifest["commandId"] == "frisket.core.command.open_feeds"


def test_workbench_plugin_layout_profile_runtime_host_foundation() -> None:
    layout_doc = _fixture_doc("layout_profile_runtime.json")
    plugin_doc = _fixture_doc("plugin_install_placeholders.json")

    roundtrip_case = _case(layout_doc, "three_pane_roundtrip")
    missing_case = _case(plugin_doc, "missing_geo_plugin_layout")
    install_case = _case(plugin_doc, "geo_plugin_install_trust_execution")

    runtime_state = roundtrip_case["runtimeState"]
    assert runtime_state["activeCell"] == {"rowId": "row_7", "columnId": "col_geo"}
    assert runtime_state["activeEvidenceLinkId"] == 42
    assert runtime_state["activePaneId"] == "pane-map"
    assert roundtrip_case["layout"]["main"]["type"] == "split"
    assert roundtrip_case["layout"]["main"]["axis"] in {"horizontal", "vertical"}
    assert len(roundtrip_case["layout"]["main"]["children"]) == len(
        roundtrip_case["layout"]["main"]["weights"]
    )
    assert [
        leaf["contributionId"] for leaf in roundtrip_case["layout"]["main"]["children"]
    ] == [
        "frisket.core.view.grid",
        "frisket.geo.view.map",
        "frisket.core.view.evidence",
    ]

    assert install_case["startingState"]["resolvedStatus"] == "missing"
    assert (
        install_case["indexEntry"]["schemaVersion"] == "frisket.plugin_index_entry.v1"
    )
    assert install_case["indexEntry"]["pluginId"] == "frisket.geo"
    assert install_case["indexEntry"]["trustTier"] == "trustedLocal"
    assert install_case["indexEntry"]["source"] == {
        "kind": "localPath",
        "value": "/opt/frisket/plugins/frisket-geo",
    }
    assert (
        install_case["installPlan"]["schemaVersion"] == "frisket.plugin_install_plan.v1"
    )
    assert install_case["installPlan"]["actions"] == ["install", "trust", "enable"]
    assert [
        permission["id"] for permission in install_case["installPlan"]["permissions"]
    ] == ["project.read", "local.external.activate"]
    assert (
        install_case["installPlan"]["permissionPrompt"]["title"]
        == "Trust local geo plugin"
    )
    assert install_case["expectedState"]["installState"] == "enabled"
    assert install_case["expectedState"]["activation"] == "loadAllowed"
    assert install_case["expectedState"]["layoutMutated"] is False
    assert install_case["layoutRef"] == missing_case["layoutRef"]


def _fixture_doc(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_FIXTURES_ROOT / name).read_text(encoding="utf-8"))


def _case(document: dict, case_id: str) -> dict:
    return next(case for case in document["cases"] if case["id"] == case_id)


def load_fixture_descriptors(root: Path) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        if path.name == "expected_matrix.json":
            continue
        for case in json.loads(path.read_text(encoding="utf-8")).get("cases", []):
            if not isinstance(case, dict):
                continue
            raw = case.get("rawDescriptor")
            if isinstance(raw, dict):
                descriptors.append(raw)
            for key in ("sourceKindForms", "actionForms", "commands", "descriptors"):
                value = case.get(key)
                if isinstance(value, list):
                    descriptors.extend(item for item in value if isinstance(item, dict))
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for descriptor in descriptors:
        descriptor_id = descriptor.get("id")
        if isinstance(descriptor_id, str) and descriptor_id not in seen:
            seen.add(descriptor_id)
            unique.append(descriptor)
    return unique


# --- panel descriptors ---

PANEL_DESCRIPTORS_ROOT = Path(__file__).resolve().parents[2]
PANEL_DESCRIPTORS_PLUGIN_ID = "demo.selection_summary"
PANEL_ID = "demo.selection_summary.panel.selection_summary"
COMPONENT_KEY = "trustedLocal.demoSelectionSummary.SelectionSummaryPanel"
PANEL_DESCRIPTORS_PLUGIN_ROOT = (
    PANEL_DESCRIPTORS_ROOT / "tests/fixtures/local_plugins/demo_selection_summary"
)
PLUGIN_MANIFEST = PANEL_DESCRIPTORS_PLUGIN_ROOT / "plugin.json"


def _activate_selection_summary_plugin(
    client: TestClient,
    project_id: str,
) -> dict[str, Any]:
    # install-local writes the workspace catalog identity that /activate
    # reads (a raw plugin.load action no longer populates it).
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PANEL_DESCRIPTORS_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_MANIFEST)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]
    assert receipt_id

    activation_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PANEL_DESCRIPTORS_PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activation_response.status_code == 200, activation_response.text
    activation = activation_response.json()
    assert activation["installState"] == "enabled"
    assert activation["registryActivated"] is True
    return activation


def test_selection_summary_panel_descriptor_projects_from_runtime_index(
    client,
) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Selection summary panel"}
    ).json()["id"]

    activation = _activate_selection_summary_plugin(client, project_id)
    assert activation["pluginId"] == PANEL_DESCRIPTORS_PLUGIN_ID

    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    runtime_index = index_response.json()
    plugin = next(
        item
        for item in runtime_index["plugins"]
        if item["pluginId"] == PANEL_DESCRIPTORS_PLUGIN_ID
    )

    assert plugin["schemaVersion"] == "frisket.workbench_plugin_runtime_plugin.v1"
    assert plugin["installState"] == "enabled"
    assert plugin["activation"] == "registryManifestRegistered"
    assert plugin["contributionSummary"] == [
        {"kind": "workbench_panel", "count": 1, "ids": [PANEL_ID]},
    ]
    assert plugin["frontendComponentBindings"] == [
        {
            "schemaVersion": ("frisket.workbench_plugin_frontend_component_binding.v1"),
            "contributionId": PANEL_ID,
            "moduleKey": "trustedLocal.demoSelectionSummary",
            "componentKey": COMPONENT_KEY,
        }
    ]
    assert plugin["workbenchDescriptorPackage"] == {
        "schemaVersion": "frisket.workbench_descriptor_package.v1",
        "sourcePath": str(PANEL_DESCRIPTORS_PLUGIN_ROOT / "workbench-descriptors.json"),
        "descriptorCount": 1,
        "runtimeOnlyFieldsStripped": ["componentKey"],
    }
    assert len(plugin["workbenchDescriptorManifests"]) == 1

    descriptor = plugin["workbenchDescriptorManifests"][0]
    assert descriptor["schemaVersion"] == "frisket.workbench.panel.v1"
    assert descriptor["id"] == PANEL_ID
    assert descriptor["kind"] == "panel"
    assert descriptor["ownerPluginId"] == PANEL_DESCRIPTORS_PLUGIN_ID
    assert descriptor["title"] == "Selection summary"
    assert descriptor["placements"] == [
        {
            "host": "rightInspector",
            "mode": "panel",
            "slot": "inspection",
            "placementId": "demo-selection-summary-right-inspector",
            "order": 55,
        }
    ]
    assert descriptor["requires"] == [
        {"kind": "hostCapability", "id": "sheet.active"},
        {"kind": "hostCapability", "id": "selection.rows"},
        {"kind": "hostCapability", "id": "host.navigation.openRow"},
    ]
    assert descriptor["dataRequirements"] == [{"kind": "activeSheet"}]
    assert "componentKey" not in descriptor
    assert "appearsWhen" not in descriptor


# --- marketplace policy ---


def _client(tmp_path) -> TestClient:
    app = create_app(tmp_path)
    return TestClient(app)


def _project_id(client: TestClient) -> str:
    response = client.post("/api/projects", json={"name": "marketplace-policy"})
    assert response.status_code == 200
    return response.json()["id"]


def test_workbench_marketplace_discovery_policy_is_backend_project_scoped(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    pid = _project_id(client)

    response = client.get(
        f"/api/projects/{pid}/workbench/marketplace",
        params={"contribution_id": "frisket.geo.view.map", "query": "geo"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schemaVersion"] == "frisket.plugin_marketplace_query.v1"
    assert payload["projectId"] == pid
    assert payload["contributionId"] == "frisket.geo.view.map"
    assert payload["text"] == "geo"
    assert payload["policySource"] == "backend"
    assert payload["arbitraryPackageLoadAllowed"] is False
    assert payload["allowedActions"] == ["view_index_entry", "request_review"]
    assert payload["disabledActions"] == ["install", "enable", "trust", "load_package"]
    assert payload["resultCount"] == 2

    entries = {entry["pluginId"]: entry for entry in payload["entries"]}
    assert set(entries) == {"community.geo-map", "community.bottom-dock-network"}
    assert (
        entries["community.geo-map"]["schemaVersion"] == "frisket.plugin_index_entry.v1"
    )
    assert entries["community.geo-map"]["source"] == {
        "kind": "marketplace",
        "value": "community.geo-map",
    }
    assert entries["community.geo-map"]["compatibility"] == "compatible"
    assert (
        entries["community.geo-map"]["disabledReason"] == "marketplace_review_required"
    )
    assert entries["community.geo-map"]["installActionsDisabled"] is True
    assert entries["community.bottom-dock-network"]["compatibility"] == "incompatible"
    assert (
        entries["community.bottom-dock-network"]["disabledReason"]
        == "version_incompatible"
    )

    network = client.get(
        f"/api/projects/{pid}/workbench/marketplace",
        params={"contribution_id": "frisket.geo.view.map", "query": "network"},
    )
    assert network.status_code == 200
    assert network.json()["resultCount"] == 1
    assert network.json()["entries"][0]["pluginId"] == "community.bottom-dock-network"


def test_workbench_marketplace_install_attempt_fails_closed_without_layout_mutation(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    pid = _project_id(client)

    response = client.post(
        f"/api/projects/{pid}/workbench/marketplace/install-attempt",
        json={
            "pluginId": "community.graph",
            "version": "0.9.0",
            "source": {"kind": "marketplace", "value": "community.graph"},
            "arbitraryPackageLoadAllowed": False,
        },
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["schemaVersion"] == "frisket.plugin_install_attempt.v1"
    assert payload["projectId"] == pid
    assert payload["pluginId"] == "community.graph"
    assert payload["source"] == {"kind": "marketplace", "value": "community.graph"}
    assert payload["installState"] == "failed"
    assert payload["failure"] == {
        "code": "checksum_mismatch",
        "message": "Downloaded plugin checksum did not match index entry.",
        "retryable": False,
    }
    assert payload["layoutMutated"] is False
    assert payload["failureVisible"] is True
    assert payload["arbitraryPackageLoadAllowed"] is False


# --- admission ---

ADMISSION_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_ROOT = ADMISSION_ROOT / "src" / "frisket" / "authoring" / "bundled_plugins"
GEO = "frisket.geo"
MAP_VIEW = "frisket.geo.view.map"
MAP_POINTS = "frisket.geo.projection.map_points"
OTHER_SHA = "sha256:" + "0" * 64


@pytest.fixture()
def geo_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for module in (plugin_runtime, plugin_runtime_status):
        monkeypatch.setattr(module, "_bundled_plugins_root", lambda: BUNDLED_ROOT)
    _reset_default_registry_for_tests()
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "admission"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    yield client, project, project_id
    _reset_default_registry_for_tests()


def _tamper(project: Any, mismatch: str) -> None:
    catalog = plugin_runtime_status.plugin_package_catalog_for_project(project)
    ref = catalog.manifest_ref(GEO)
    assert ref is not None
    if mismatch == "plugin_id":
        ref["plugin_id"] = "frisket.geo2"
    elif mismatch == "package_sha":
        ref["package_sha256"] = OTHER_SHA
    else:
        ref["manifest_sha256"] = OTHER_SHA
    conn = catalog._connect()
    try:
        conn.execute(
            "UPDATE plugin_packages SET manifest_ref=? WHERE plugin_id=?",
            (json.dumps(ref, separators=(",", ":"), sort_keys=True), GEO),
        )
        conn.commit()
    finally:
        conn.close()


def _frontend(project: Any, project_id: str) -> Any:
    entry = plugin_runtime_status.plugin_package_catalog_for_project(project).get(GEO)
    return workbench_plugin_frontend_component_module(
        project,
        plugin_id=GEO,
        contribution_id=MAP_VIEW,
        expected_package_sha256=entry["package_sha256"],
    )


def _runtime_binding(project: Any, project_id: str) -> Any:
    binding = next(
        spec
        for binding_type in ("projections",)
        for spec in plugin_runtime.default_registry().runtime_binding_specs(
            binding_type
        )
        if spec.kind == MAP_POINTS
    )
    admitted = project_runtime_binding(
        project, binding_type="projections", kind=binding.kind
    )
    if admitted is None:
        raise LookupError("binding not admitted")
    return admitted


def _enabled_ids(project: Any, project_id: str) -> Any:
    if GEO not in enabled_workbench_plugin_ids(project):
        raise LookupError("plugin not enabled")


def _activation(project: Any, project_id: str) -> Any:
    catalog = plugin_runtime_status.plugin_package_catalog_for_project(project)
    entry = catalog.get(GEO)
    required = _effective_required_capabilities(
        _loaded_manifest_from_ref(catalog.manifest_ref(GEO)).manifest
    )
    return activate_workbench_plugin_manifest(
        project,
        project_id=project_id,
        plugin_id=GEO,
        receipt_id=entry["receipt_id"],
        trust_acknowledged=True,
        permissions_accepted=sorted(required),
        arbitrary_package_load_allowed=False,
    )


def _backend_activation(project: Any, project_id: str) -> Any:
    return activate_workbench_plugin_backend_contributions(
        project,
        project_id=project_id,
        plugin_id=GEO,
        trust_acknowledged=True,
        arbitrary_package_load_allowed=False,
    )


def _plugin_env(project: Any, project_id: str) -> Any:
    return _loaded_manifest_for_plugin_env(project, plugin_id=GEO)


def _runtime_index(project: Any, project_id: str) -> Any:
    plugins = {
        item["pluginId"]: item
        for item in workbench_plugin_runtime_index(project, project_id=project_id)[
            "plugins"
        ]
    }
    if not plugins[GEO]["registryActivated"]:
        raise LookupError("plugin not activated")


ENTRY_POINTS: dict[str, Callable[[Any, str], Any]] = {
    "frontend_module": _frontend,
    "project_runtime_binding": _runtime_binding,
    "enabled_workbench_plugin_ids": _enabled_ids,
    "manifest_activation": _activation,
    "backend_activation": _backend_activation,
    "plugin_env": _plugin_env,
    "runtime_index": _runtime_index,
}
REFUSED = (
    LookupError,
    WorkbenchPluginActivationError,
    WorkbenchPluginLifecycleError,
)


@pytest.mark.parametrize("entry_point", sorted(ENTRY_POINTS))
@pytest.mark.parametrize("mismatch", ["plugin_id", "package_sha", "manifest_sha"])
def test_entry_point_refuses_catalog_evidence_mismatch(
    geo_project, entry_point: str, mismatch: str
) -> None:
    _client, project, project_id = geo_project
    admit = ENTRY_POINTS[entry_point]
    admit(project, project_id)

    _tamper(project, mismatch)

    with pytest.raises(REFUSED):
        admit(project, project_id)


# --- project env vars ---

PROJECT_ENV_VARS_PLUGIN_ID = "demo.env_settings"
PLUGIN_CAPABILITY = "plugin:trusted_local_backend"
PROJECT_ENV_VARS_PLUGIN_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "demo_env_settings"
)


def _install_and_activate_env_plugin(client: TestClient, project_id: str) -> None:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PROJECT_ENV_VARS_PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    install_body = installed.json()
    assert install_body["installState"] == "installed"
    assert install_body["receiptId"]

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/activate",
        json={
            "receiptId": install_body["receiptId"],
            "trustAcknowledged": True,
            "permissionsAccepted": [PLUGIN_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["installState"] == "enabled"


def _env_list(client: TestClient, project_id: str) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/env"
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_plugin_project_env_vars_are_project_scoped_encrypted_and_redacted(
    client,
) -> None:
    project_a = client.post("/api/projects", json={"name": "Plugin env A"}).json()["id"]
    project_b = client.post("/api/projects", json={"name": "Plugin env B"}).json()["id"]
    _install_and_activate_env_plugin(client, project_a)

    missing = client.get(
        f"/api/projects/{project_b}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/env"
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "plugin_env_plugin_not_installed"

    initial = _env_list(client, project_a)
    assert initial == {
        "schemaVersion": "frisket.workbench_plugin_env_vars.v1",
        "projectId": project_a,
        "pluginId": PROJECT_ENV_VARS_PLUGIN_ID,
        "env": [
            {
                "schemaVersion": "frisket.workbench_plugin_env_var.v1",
                "projectId": project_a,
                "pluginId": PROJECT_ENV_VARS_PLUGIN_ID,
                "name": "DEMO_API_KEY",
                "hint": None,
                "required": True,
                "configured": False,
                "updatedAt": None,
            }
        ],
    }

    secret_value = "stage3-secret-value"
    malformed_requests = [
        client.post(
            f"/api/projects/{project_a}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/env",
            json={"value": secret_value},
        ),
        client.post(
            f"/api/projects/{project_a}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/env",
            headers={"content-type": "application/json"},
            content=secret_value,
        ),
        client.post(
            f"/api/projects/{project_a}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/env",
            json={"name": "DEMO_API_KEY", "value": {"token": secret_value}},
        ),
    ]
    for response in malformed_requests:
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "plugin_env_body_invalid"
        assert secret_value not in response.text

    invalid = client.post(
        f"/api/projects/{project_a}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/env",
        json={"name": "DEMO-API-KEY", "value": "not-used"},
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["detail"]["code"] == "plugin_env_name_invalid"

    saved = client.post(
        f"/api/projects/{project_a}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/env",
        json={"name": "demo_api_key", "value": secret_value},
    )
    assert saved.status_code == 200, saved.text
    saved_body = saved.json()
    assert saved_body == {
        "schemaVersion": "frisket.workbench_plugin_env_var.v1",
        "projectId": project_a,
        "pluginId": PROJECT_ENV_VARS_PLUGIN_ID,
        "name": "DEMO_API_KEY",
        "hint": "...alue",
        "required": True,
        "configured": True,
        "updatedAt": saved_body["updatedAt"],
    }
    assert secret_value not in saved.text

    project = client.app.state.workspace.get(project_a)
    row = project.db.execute(
        "SELECT encrypted, hint FROM workbench_plugin_env_vars "
        "WHERE plugin_id=? AND name=?",
        (PROJECT_ENV_VARS_PLUGIN_ID, "DEMO_API_KEY"),
    ).fetchone()
    assert row is not None
    encrypted = str(row["encrypted"])
    assert encrypted != secret_value
    assert secret_value not in encrypted
    assert row["hint"] == "...alue"
    assert decrypt_secret(encrypted) == secret_value

    listed = _env_list(client, project_a)
    rendered = json.dumps(listed, sort_keys=True)
    assert secret_value not in rendered
    assert listed["env"][0]["configured"] is True
    assert listed["env"][0]["hint"] == "...alue"

    _install_and_activate_env_plugin(client, project_b)
    project_b_list = _env_list(client, project_b)
    assert project_b_list["env"][0]["configured"] is False
    assert project_b_list["env"][0]["hint"] is None

    deleted = client.delete(
        f"/api/projects/{project_a}/workbench/plugins/{PROJECT_ENV_VARS_PLUGIN_ID}/env/DEMO_API_KEY"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "ok": True,
        "deleted": True,
        "projectId": project_a,
        "pluginId": PROJECT_ENV_VARS_PLUGIN_ID,
        "name": "DEMO_API_KEY",
    }
    after_delete = _env_list(client, project_a)
    assert after_delete["env"][0]["configured"] is False
    assert after_delete["env"][0]["hint"] is None


# --- settings ---

SETTINGS_PLUGIN_ID = "demo.env_settings"
SETTINGS_PLUGIN_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "demo_env_settings"
)


def _install_and_activate(client: TestClient, pid: str) -> None:
    installed = client.post(
        f"/api/projects/{pid}/workbench/plugins/{SETTINGS_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(SETTINGS_PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    activated = client.post(
        f"/api/projects/{pid}/workbench/plugins/{SETTINGS_PLUGIN_ID}/activate",
        json={
            "receiptId": installed.json()["receiptId"],
            "trustAcknowledged": True,
            "permissionsAccepted": ["plugin:trusted_local_backend"],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text


def test_workbench_plugin_settings_read_defaults_and_patch_project_values(
    client,
) -> None:
    pid = client.post("/api/projects", json={"name": "Plugin settings"}).json()["id"]
    _install_and_activate(client, pid)

    initial = client.get(
        f"/api/projects/{pid}/workbench/plugins/{SETTINGS_PLUGIN_ID}/settings"
    )
    assert initial.status_code == 200, initial.text
    assert initial.json()["schemaVersion"] == "frisket.workbench_plugin_settings.v1"
    by_id = {item["id"]: item for item in initial.json()["settings"]}
    assert by_id["demo.env_settings.enabled"]["effectiveValue"] is True
    assert by_id["demo.env_settings.mode"]["effectiveValue"] == "summary"
    assert by_id["demo.env_settings.mode"]["source"] == "default"

    patched = client.patch(
        f"/api/projects/{pid}/workbench/plugins/{SETTINGS_PLUGIN_ID}/settings",
        json={"values": {"demo.env_settings.mode": "detail"}},
    )
    assert patched.status_code == 200, patched.text
    mode = {item["id"]: item for item in patched.json()["settings"]}[
        "demo.env_settings.mode"
    ]
    assert mode["effectiveValue"] == "detail"
    assert mode["source"] == "project"


def test_workbench_plugin_settings_reject_unknown_and_secret_like_values(
    client,
) -> None:
    pid = client.post("/api/projects", json={"name": "Plugin settings"}).json()["id"]
    _install_and_activate(client, pid)

    unknown = client.patch(
        f"/api/projects/{pid}/workbench/plugins/{SETTINGS_PLUGIN_ID}/settings",
        json={"values": {"demo.env_settings.missing": "detail"}},
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "invalid_plugin_settings"

    secret_like = client.patch(
        f"/api/projects/{pid}/workbench/plugins/{SETTINGS_PLUGIN_ID}/settings",
        json={"values": {"demo.env_settings.mode": "api_key=secret"}},
    )
    assert secret_like.status_code == 422
    assert secret_like.json()["detail"]["code"] == "invalid_plugin_settings"
    assert "api_key=secret" not in secret_like.text


# --- runtime settings ---


def _stub_loaded_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = SimpleNamespace(
        manifest=SimpleNamespace(requires=SimpleNamespace(secrets=[]))
    )
    monkeypatch.setattr(
        plugin_runtime_settings,
        "_loaded_manifest_for_plugin_env",
        lambda project, *, plugin_id: loaded,
    )


def _assert_raw_value_absent(exc: BaseException, raw_value: str) -> None:
    assert raw_value not in str(exc)
    assert raw_value not in json.dumps(
        getattr(exc, "details", {}), sort_keys=True, default=str
    )


def test_reserved_plugin_env_name_error_never_echoes_raw_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loaded_manifest(monkeypatch)
    raw_value = "reserved-name-raw-secret-04b"

    with pytest.raises(WorkbenchPluginLifecycleError) as raised:
        plugin_runtime_settings.set_workbench_plugin_env_var(
            object(),
            project_id="project-04b",
            plugin_id="plugin.04b",
            name="OPENAI_API_KEY",
            value=raw_value,
        )

    assert raised.value.code == "plugin_env_name_reserved"
    _assert_raw_value_absent(raised.value, raw_value)


def test_empty_plugin_env_value_error_has_only_safe_message_and_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loaded_manifest(monkeypatch)
    raw_value = ""

    with pytest.raises(WorkbenchPluginLifecycleError) as raised:
        plugin_runtime_settings.set_workbench_plugin_env_var(
            object(),
            project_id="project-04b",
            plugin_id="plugin.04b",
            name="PLUGIN_04B_TOKEN",
            value=raw_value,
        )

    assert raised.value.code == "plugin_env_value_required"
    assert raised.value.message == "plugin env var value is required"
    assert raised.value.details == {}
    assert repr(raw_value) not in raised.value.message
    assert repr(raw_value) not in json.dumps(raised.value.details, sort_keys=True)


def test_plugin_env_write_reraises_without_echoing_raw_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loaded_manifest(monkeypatch)
    raw_value = "write-failure-raw-secret-04b"
    write_error = RuntimeError("fixture plugin env write failed")

    class FailingPluginEnvDB:
        def __init__(self) -> None:
            self.rollback_calls = 0

        def execute(self, statement: str, parameters: tuple[str, ...]) -> None:
            assert "INSERT INTO workbench_plugin_env_vars" in statement
            assert raw_value not in parameters
            raise write_error

        def rollback(self) -> None:
            self.rollback_calls += 1

    database = FailingPluginEnvDB()
    encrypted_inputs: list[str] = []
    hinted_inputs: list[str] = []
    monkeypatch.setattr(
        plugin_runtime_settings,
        "encrypt_secret",
        lambda value: encrypted_inputs.append(value) or "encrypted-fixture",
    )
    monkeypatch.setattr(
        plugin_runtime_settings,
        "key_hint",
        lambda value: hinted_inputs.append(value) or "...04b",
    )

    with pytest.raises(RuntimeError) as raised:
        plugin_runtime_settings.set_workbench_plugin_env_var(
            SimpleNamespace(db=database),
            project_id="project-04b",
            plugin_id="plugin.04b",
            name="PLUGIN_04B_TOKEN",
            value=raw_value,
        )

    assert raised.value is write_error
    _assert_raw_value_absent(raised.value, raw_value)
    assert encrypted_inputs == [raw_value]
    assert hinted_inputs == [raw_value]
    assert database.rollback_calls == 1


# --- runtime index ---


def _runtime_index_write_manifest(
    tmp_path: Path, *, plugin_id: str = "frisket-ndjson"
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
                    "actions": ["import.ndjson"],
                    "importers": ["ndjson"],
                    "column_types": ["geo_point"],
                    "job_handlers": [],
                },
                "requires": {"capabilities": [], "secrets": []},
            }
        ),
        encoding="utf-8",
    )
    return path


def _install_local(
    client: TestClient, project_id: str, manifest_path: Path, *, plugin_id: str
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_workbench_plugin_runtime_index_byte_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The index derives package identity from the workspace catalog,
    # not a per-project receipt scan. Package IDENTITY (manifest evidence + the
    # digests) is the catalog's; the project's row supplies enablement (here
    # absent, so the plugin projects as a bare "installed" catalog entry).
    manifest_ref = {
        "kind": "plugin_manifest",
        "schema_version": "frisket.plugin.v1",
        "plugin_id": "fixture.plugin",
        "version": "1.2.3",
        "path": "/fixtures/fixture.plugin.json",
        "manifest_sha256": "sha256:fixture-manifest",
        "package_sha256": "sha256:fixture-package",
        "byte_count": 321,
        "contributes": {
            "workbench_views": ["fixture.plugin.view.main"],
            "actions": ["fixture.action"],
        },
        "requires": {
            "capabilities": ["project:read"],
            "secrets": ["FIXTURE_TOKEN"],
        },
        "runtime": {
            "workbench_components": [
                {
                    "contribution_id": "fixture.plugin.view.main",
                    "module_key": "trustedLocal.fixturePlugin.module",
                    "component_key": "trustedLocal.fixturePlugin.MainView",
                    "module_path": "frontend/main.js",
                }
            ]
        },
    }
    catalog_entry = {
        "plugin_id": "fixture.plugin",
        "install_source": "{}",
        "manifest_sha256": "sha256:fixture-manifest",
        "package_sha256": "sha256:fixture-package",
        "runtime_source": "plugin.load_receipt",
        "receipt_id": "receipt-fixture",
        "manifest_ref": json.dumps(manifest_ref),
        "has_executable_backend": 0,
        "install_failure": None,
        "updated_at": "",
    }

    class FixtureCatalog:
        def all(self) -> dict[str, dict[str, Any]]:
            return {"fixture.plugin": catalog_entry}

        def manifest_ref(self, plugin_id: str) -> dict[str, Any] | None:
            return manifest_ref if plugin_id == "fixture.plugin" else None

    fixture_project = object()
    monkeypatch.setattr(
        plugin_runtime_status,
        "plugin_package_catalog_for_project",
        lambda project: FixtureCatalog(),
    )
    monkeypatch.setattr(
        plugin_runtime_status,
        "default_registry",
        lambda: SimpleNamespace(plugin_manifests=lambda: []),
    )
    monkeypatch.setattr(
        plugin_runtime_status,
        "_workbench_plugin_install_states",
        lambda project: {},
    )
    monkeypatch.setattr(
        plugin_runtime_status,
        "load_first_party_workbench_descriptor_package",
        lambda: SimpleNamespace(
            schema_version="frisket.workbench_descriptor_package.v1",
            descriptor_manifests=[
                {
                    "schemaVersion": "frisket.workbench.view.v1",
                    "id": "frisket.core.view.fixture",
                }
            ],
        ),
    )

    rendered = json.dumps(
        plugin_runtime_status.workbench_plugin_runtime_index(
            fixture_project,
            project_id="project/fixture",
        ),
        separators=(",", ":"),
    ).encode()
    expected = json.dumps(
        {
            "schemaVersion": "frisket.workbench_plugin_runtime_index.v1",
            "projectId": "project/fixture",
            "arbitraryPackageLoadAllowed": False,
            "receiptScanLimit": 5000,
            "skippedInvalidReceipts": 0,
            "skippedInvalidManifestRefs": 0,
            "loadedPluginCount": 1,
            "plugins": [
                {
                    "schemaVersion": "frisket.workbench_plugin_runtime_plugin.v1",
                    "pluginId": "fixture.plugin",
                    "version": "1.2.3",
                    "installState": "installed",
                    "activation": "manifestLoaded",
                    "runtimeSource": "plugin.load_receipt",
                    "receiptId": "receipt-fixture",
                    "manifestSha256": "sha256:fixture-manifest",
                    "packageSha256": "sha256:fixture-package",
                    "byteCount": 321,
                    "source": {
                        "kind": "local_file",
                        "path": "/fixtures/fixture.plugin.json",
                    },
                    "contributionSummary": [
                        {
                            "kind": "workbench_view",
                            "count": 1,
                            "ids": ["fixture.plugin.view.main"],
                        },
                        {
                            "kind": "action",
                            "count": 1,
                            "ids": ["fixture.action"],
                        },
                    ],
                    "frontendComponentBindings": [
                        {
                            "schemaVersion": (
                                "frisket.workbench_plugin_frontend_component_binding.v1"
                            ),
                            "contributionId": "fixture.plugin.view.main",
                            "moduleKey": "trustedLocal.fixturePlugin.module",
                            "componentKey": "trustedLocal.fixturePlugin.MainView",
                            "modulePath": "frontend/main.js",
                            "moduleUrl": (
                                "/api/projects/project%2Ffixture/workbench/plugins/"
                                "fixture.plugin/frontend-components/"
                                "fixture.plugin.view.main/module.js?"
                                "package=sha256%3Afixture-package"
                            ),
                        }
                    ],
                    "requires": {
                        "capabilities": ["project:read"],
                        "secrets": ["FIXTURE_TOKEN"],
                    },
                    "arbitraryPackageLoadAllowed": False,
                    "registryActivated": False,
                    "installStateSchemaVersion": None,
                    "disabledReason": None,
                }
            ],
            "firstParty": {
                "schemaVersion": "frisket.workbench_descriptor_package.v1",
                "descriptors": [
                    {
                        "schemaVersion": "frisket.workbench.view.v1",
                        "id": "frisket.core.view.fixture",
                    }
                ],
            },
        },
        separators=(",", ":"),
    ).encode()

    assert rendered == expected


def test_workbench_plugin_runtime_index_projects_loaded_manifest_receipts(
    tmp_path: Path,
    client,
) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Workbench plugin index"}
    ).json()["id"]
    manifest_path = _runtime_index_write_manifest(tmp_path)

    install = _install_local(
        client, project_id, manifest_path, plugin_id="frisket-ndjson"
    )
    assert install["receiptId"]

    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    body = index_response.json()
    assert body["schemaVersion"] == "frisket.workbench_plugin_runtime_index.v1"
    assert body["projectId"] == project_id
    assert body["arbitraryPackageLoadAllowed"] is False
    assert body["skippedInvalidReceipts"] == 0
    assert body["skippedInvalidManifestRefs"] == 0
    assert body["loadedPluginCount"] == 1

    plugin = body["plugins"][0]
    assert plugin["schemaVersion"] == "frisket.workbench_plugin_runtime_plugin.v1"
    assert plugin["pluginId"] == "frisket-ndjson"
    assert plugin["version"] == "0.1.0"
    assert plugin["installState"] == "installed"
    assert plugin["activation"] == "manifestLoaded"
    assert plugin["runtimeSource"] == "plugin.load_receipt"
    assert plugin["receiptId"] == install["receiptId"]
    assert plugin["manifestSha256"].startswith("sha256:")
    # Source is now the validated install source recorded in the workspace
    # catalog, keyed by plugin_id.
    assert plugin["source"]["kind"] == "localPath"
    assert plugin["source"]["value"] == str(manifest_path)
    assert plugin["contributionSummary"] == [
        {"kind": "action", "count": 1, "ids": ["import.ndjson"]},
        {"kind": "importer", "count": 1, "ids": ["ndjson"]},
        {"kind": "column_type", "count": 1, "ids": ["geo_point"]},
    ]
    assert plugin["requires"] == {
        "capabilities": [],
        "secrets": [],
    }
    assert plugin["arbitraryPackageLoadAllowed"] is False


def test_workbench_plugin_runtime_index_projects_frontend_component_bindings(
    tmp_path: Path,
    client,
) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Workbench plugin frontend bindings"}
    ).json()["id"]
    package_dir = tmp_path / "frisket.geo"
    package_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = package_dir / "plugin.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": "frisket.geo",
                "version": "0.1.0",
                "contributes": {
                    "workbench_views": ["frisket.geo.view.map"],
                    "workbench_panels": [
                        # Cross-surface DECLARATION (legal): the plugin lights
                        # up the core projection panel...
                        "frisket.core.panel.projection_status",
                        # ...but component BINDINGS are always the plugin's
                        # own code (plugin-binding-namespace-hardening-v1).
                        "frisket.geo.panel.receipt_status",
                    ],
                },
                "requires": {"capabilities": [], "secrets": []},
                "runtime": {
                    "workbench_components": [
                        {
                            "contribution_id": "frisket.geo.view.map",
                            "module_key": "trustedLocal.frisketGeo.receiptModule",
                            "component_key": "trustedLocal.frisketGeo.receiptMapView",
                        },
                        {
                            "contribution_id": "frisket.geo.panel.receipt_status",
                            "module_key": "trustedLocal.frisketGeo.receiptModule",
                            "component_key": "trustedLocal.frisketGeo.receiptProjectionStatus",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    _install_local(client, project_id, manifest_path, plugin_id="frisket.geo")

    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    plugin = index_response.json()["plugins"][0]
    assert plugin["pluginId"] == "frisket.geo"
    assert plugin["contributionSummary"] == [
        {"kind": "workbench_view", "count": 1, "ids": ["frisket.geo.view.map"]},
        {
            "kind": "workbench_panel",
            "count": 2,
            "ids": [
                "frisket.core.panel.projection_status",
                "frisket.geo.panel.receipt_status",
            ],
        },
    ]
    assert plugin["frontendComponentBindings"] == [
        {
            "schemaVersion": "frisket.workbench_plugin_frontend_component_binding.v1",
            "contributionId": "frisket.geo.view.map",
            "moduleKey": "trustedLocal.frisketGeo.receiptModule",
            "componentKey": "trustedLocal.frisketGeo.receiptMapView",
        },
        {
            "schemaVersion": "frisket.workbench_plugin_frontend_component_binding.v1",
            "contributionId": "frisket.geo.panel.receipt_status",
            "moduleKey": "trustedLocal.frisketGeo.receiptModule",
            "componentKey": "trustedLocal.frisketGeo.receiptProjectionStatus",
        },
    ]


def test_workbench_plugin_runtime_index_filters_invalid_frontend_component_bindings(
    tmp_path: Path,
    client,
) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Workbench plugin invalid frontend bindings"}
    ).json()["id"]
    package_dir = tmp_path / "frisket.geo"
    package_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = package_dir / "plugin.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": "frisket.geo",
                "version": "0.1.0",
                "contributes": {"workbench_views": ["frisket.geo.view.map"]},
                "requires": {"capabilities": [], "secrets": []},
                "runtime": {
                    "workbench_components": [
                        {
                            "contribution_id": "frisket.geo.view.map",
                            "module_key": "trustedLocal.frisketGeo",
                            "component_key": "trustedLocal.frisketGeo.views.MapView",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    _install_local(client, project_id, manifest_path, plugin_id="frisket.geo")

    # Corrupt the WORKSPACE CATALOG's manifest evidence (identity is catalog-
    # owned now): a whitespace-padded component_key must be filtered out.
    catalog = client.app.state.workspace.plugin_package_catalog
    entry = catalog.get("frisket.geo")
    manifest_ref = catalog.manifest_ref("frisket.geo")
    manifest_ref["runtime"]["workbench_components"][0]["component_key"] = (
        " trustedLocal.frisketGeo.views.MapView "
    )
    catalog.upsert(
        plugin_id="frisket.geo",
        install_source=json.loads(entry["install_source"]),
        manifest_sha256=entry["manifest_sha256"],
        package_sha256=entry["package_sha256"],
        receipt_id=entry["receipt_id"],
        manifest_ref=manifest_ref,
        has_executable_backend=bool(entry["has_executable_backend"]),
    )

    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    plugin = index_response.json()["plugins"][0]
    assert plugin["frontendComponentBindings"] == []

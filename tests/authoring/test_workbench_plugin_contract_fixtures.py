from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.authoring.workbench.contracts import (
    CANONICAL_REGIONS,
    FORBIDDEN_TAB_CONTRACTS,
    LEGAL_HOSTS,
    build_activity_recovery_entries,
    load_fixture_descriptors,
    project_manifest,
    resolve_contributions,
)


ROOT = Path(__file__).parent.parent / "fixtures" / "workbench_plugin"
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
        for path in sorted(ROOT.glob("*.json"))
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

    descriptors = load_fixture_descriptors(ROOT)
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


def test_workbench_plugin_manifest_projection_and_resolver_diagnostics() -> None:
    descriptors = load_fixture_descriptors(ROOT)
    by_id = {descriptor["id"]: descriptor for descriptor in descriptors}

    sources_manifest = project_manifest(by_id["frisket.core.panel.sources"])
    assert sources_manifest["id"] == "frisket.core.panel.sources"
    assert "componentKey" not in sources_manifest
    assert sources_manifest["placements"][1]["host"] == "bottomDock"
    assert sources_manifest["placements"][1]["mode"] == "tab"

    command_manifest = project_manifest(by_id["frisket.core.command.open_sources"])
    assert "handlerKey" not in command_manifest
    assert command_manifest["commandId"] == "frisket.core.command.open_sources"

    enabled = resolve_contributions(
        descriptors,
        installed_plugins={"frisket.core", "frisket.geo"},
        trusted_plugins={"frisket.core", "frisket.geo"},
        host_capabilities={
            "source.list",
            "source.poll",
            "host.navigation.openSheet",
            "grid.filter.applyBbox",
            "projection.status",
            "evidence.resolve",
            "evidence.listForRow",
            "evidence.open",
            "history.list",
        },
        data_context={
            "activeProject": True,
            "activeSheet": True,
            "activeEvidence": True,
            "activeRow": True,
            "columnTypes": {"geo": "frisket.core.column_type.geo_point"},
        },
        hidden_contributions=set(),
    )
    assert enabled["frisket.geo.view.map"].status == "enabled"
    assert enabled["frisket.geo.view.map"].activation == "loadAllowed"

    hidden = resolve_contributions(
        descriptors,
        installed_plugins={"frisket.core", "frisket.geo"},
        trusted_plugins={"frisket.core", "frisket.geo"},
        host_capabilities=set(),
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions={"frisket.geo.view.map"},
    )
    assert hidden["frisket.geo.view.map"].status == "hidden"
    assert hidden["frisket.geo.view.map"].reasons[0]["code"] == "hidden_by_profile"

    disabled = resolve_contributions(
        [by_id["frisket.geo.view.map"]],
        installed_plugins={"frisket.geo"},
        trusted_plugins={"frisket.geo"},
        host_capabilities={"grid.filter.applyBbox", "projection.status"},
        data_context={
            "activeProject": True,
            "activeSheet": True,
            "columnTypes": {"name": "text"},
        },
        hidden_contributions=set(),
    )
    assert disabled["frisket.geo.view.map"].status == "disabled"
    assert (
        disabled["frisket.geo.view.map"].reasons[0]["code"] == "data_requirement_unmet"
    )

    projection_disabled = resolve_contributions(
        [by_id["frisket.core.panel.projection_status"]],
        installed_plugins={"frisket.core"},
        trusted_plugins={"frisket.core"},
        host_capabilities={"projection.status"},
        data_context={},
        hidden_contributions=set(),
    )
    assert (
        projection_disabled["frisket.core.panel.projection_status"].status == "disabled"
    )
    assert (
        projection_disabled["frisket.core.panel.projection_status"].reasons[0]["code"]
        == "missing_context"
    )

    missing_dependency = resolve_contributions(
        [by_id["frisket.core.command.open_sources"]],
        installed_plugins={"frisket.core"},
        trusted_plugins={"frisket.core"},
        host_capabilities=set(),
        data_context={},
        hidden_contributions=set(),
    )
    assert missing_dependency["frisket.core.command.open_sources"].status == "disabled"
    assert (
        missing_dependency["frisket.core.command.open_sources"].reasons[0]["code"]
        == "missing_contribution"
    )

    missing = resolve_contributions(
        [by_id["frisket.geo.view.map"]],
        installed_plugins={"frisket.core"},
        trusted_plugins={"frisket.core"},
        host_capabilities={"grid.filter.applyBbox", "projection.status"},
        data_context={
            "activeProject": True,
            "activeSheet": True,
            "columnTypes": {"geo": "frisket.core.column_type.geo_point"},
        },
        hidden_contributions=set(),
    )
    assert missing["frisket.geo.view.map"].status == "missing"
    assert missing["frisket.geo.view.map"].reasons[0]["code"] == "missing_plugin"

    runtime_failed = resolve_contributions(
        [by_id["frisket.core.panel.sources"]],
        installed_plugins={"frisket.core"},
        trusted_plugins={"frisket.core"},
        host_capabilities={"source.list", "source.poll", "host.navigation.openSheet"},
        data_context={},
        hidden_contributions=set(),
        activation_errors={
            "frisket.core.panel.sources": {
                "message": "Unable to import core.panels.SourcesPanel"
            }
        },
    )
    assert runtime_failed["frisket.core.panel.sources"].status == "enabled"
    assert runtime_failed["frisket.core.panel.sources"].activation == "failed"
    assert (
        runtime_failed["frisket.core.panel.sources"].reasons[0]["code"]
        == "runtime_activation_failed"
    )

    duplicate = resolve_contributions(
        [by_id["frisket.core.panel.sources"], by_id["frisket.core.panel.sources"]],
        installed_plugins={"frisket.core"},
        trusted_plugins={"frisket.core"},
        host_capabilities={"source.list", "source.poll", "host.navigation.openSheet"},
        data_context={},
        hidden_contributions=set(),
    )
    assert duplicate["frisket.core.panel.sources"].status == "disabled"
    assert duplicate["frisket.core.panel.sources"].reasons[0]["code"] == "duplicate_id"

    invalid_descriptor = resolve_contributions(
        [
            {
                "schemaVersion": "frisket.workbench.view.v1",
                "kind": "view",
                "ownerPluginId": "frisket.core",
                "title": "Broken descriptor",
                "placements": [{"host": "mainView", "mode": "pane"}],
                "requires": [],
            }
        ],
        installed_plugins={"frisket.core"},
        trusted_plugins={"frisket.core"},
        host_capabilities=set(),
        data_context={},
        hidden_contributions=set(),
    )
    assert invalid_descriptor["<invalid:0>"].status == "disabled"
    assert (
        invalid_descriptor["<invalid:0>"].reasons[0]["code"] == "invalid_descriptor_id"
    )

    missing_owner = resolve_contributions(
        [
            {
                "schemaVersion": "frisket.workbench.view.v1",
                "id": "frisket.example.view.no_owner",
                "kind": "view",
                "title": "No owner",
                "placements": [{"host": "mainView", "mode": "pane"}],
                "requires": [],
            }
        ],
        installed_plugins={"frisket.example"},
        trusted_plugins={"frisket.example"},
        host_capabilities=set(),
        data_context={},
        hidden_contributions=set(),
    )
    assert missing_owner["frisket.example.view.no_owner"].status == "disabled"
    assert (
        missing_owner["frisket.example.view.no_owner"].reasons[0]["code"]
        == "missing_owner_plugin"
    )

    missing_active_entity = resolve_contributions(
        [
            {
                "schemaVersion": "frisket.workbench.view.v1",
                "id": "frisket.ftm.view.entity_connections",
                "kind": "view",
                "ownerPluginId": "frisket.ftm",
                "title": "Entity connections",
                "placements": [{"host": "entityDetail", "mode": "tab"}],
                "requires": [],
                "dataRequirements": [{"kind": "activeEntity"}],
            }
        ],
        installed_plugins={"frisket.ftm"},
        trusted_plugins={"frisket.ftm"},
        host_capabilities=set(),
        data_context={"activeSheet": True},
        hidden_contributions=set(),
    )
    assert (
        missing_active_entity["frisket.ftm.view.entity_connections"].status
        == "disabled"
    )
    assert (
        missing_active_entity["frisket.ftm.view.entity_connections"].reasons[0]["code"]
        == "missing_context"
    )


def test_workbench_plugin_layout_profile_runtime_host_foundation() -> None:
    layout_doc = _fixture_doc("layout_profile_runtime.json")
    plugin_doc = _fixture_doc("plugin_install_placeholders.json")

    hidden_case = _case(layout_doc, "hidden_not_uninstalled_profile")
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

    descriptors = load_fixture_descriptors(ROOT)
    invalid_alias_shape = resolve_contributions(
        [
            {
                **next(
                    descriptor
                    for descriptor in descriptors
                    if descriptor["id"] == "frisket.core.panel.sources"
                ),
                "aliases": {"bad": "shape"},
            }
        ],
        installed_plugins={"frisket.core"},
        trusted_plugins={"frisket.core"},
        host_capabilities={"source.list", "source.poll", "host.navigation.openSheet"},
        data_context={},
        hidden_contributions=set(),
    )
    assert invalid_alias_shape["frisket.core.panel.sources"].status == "enabled"

    resolved = resolve_contributions(
        descriptors,
        installed_plugins={"frisket.core", "frisket.media"},
        trusted_plugins={"frisket.core", "frisket.media"},
        host_capabilities=set(),
        data_context={"activeProject": True, "activeSheet": True},
        hidden_contributions=set(hidden_case["projectProfile"]["hiddenContributions"]),
    )
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

    actions_resolved = resolve_contributions(
        [
            next(
                descriptor
                for descriptor in descriptors
                if descriptor["id"] == "frisket.core.panel.actions"
            )
        ],
        installed_plugins={"frisket.core"},
        trusted_plugins={"frisket.core"},
        host_capabilities={"action.run", "action.preview"},
        data_context={"activeSheet": True},
        hidden_contributions=set(),
        granted_permissions={"project.write"},
    )
    assert actions_resolved["frisket.core.panel.actions"].status == "enabled"

    recovery = build_activity_recovery_entries(resolved)
    recovery_by_id = {entry["contributionId"]: entry for entry in recovery}
    assert recovery_by_id["frisket.geo.view.map"]["status"] == "missing"
    assert recovery_by_id["frisket.geo.view.map"]["actions"] == [
        "install_plugin",
        "remove_from_layout",
        "replace_with_grid",
    ]
    assert recovery_by_id["frisket.core.panel.watches"]["status"] == "hidden"
    assert recovery_by_id["frisket.core.panel.watches"]["reason"] == "hidden_by_profile"
    assert "reveal" in recovery_by_id["frisket.core.panel.watches"]["actions"]
    assert "reset_profile" in recovery_by_id["frisket.core.panel.watches"]["actions"]


def _fixture_doc(name: str) -> dict[str, Any]:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def _case(document: dict, case_id: str) -> dict:
    return next(case for case in document["cases"] if case["id"] == case_id)

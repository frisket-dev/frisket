from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.authoring.workbench.contracts import (
    CANONICAL_REGIONS,
    FORBIDDEN_TAB_CONTRACTS,
    LEGAL_HOSTS,
    project_manifest,
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


def test_workbench_plugin_manifest_projection() -> None:
    descriptors = load_fixture_descriptors(ROOT)
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
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


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

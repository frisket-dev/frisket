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


ROOT = Path(__file__).parent.parent / "fixtures" / "workbench_portability"

LEGAL_SLOTS = {
    "scope",
    "work.primary",
    "work.companion",
    "inspection",
    "configuration",
    "companion.output",
    "interruption",
    "launcher",
    "detail",
}
BEHAVIORAL_EXPECTATION_KEYS = {
    "expected",
    "expectedResolved",
    "expectedManifest",
    "expectedMigration",
    "expectedRepair",
    "expectedErrors",
    "expectedState",
    "expectedActions",
}
RUNTIME_ONLY_STATE_KEYS = {"activeRowId", "activeCell", "activeEvidenceLinkId"}


def test_workbench_portability_contract_sketch_and_fixture_matrix() -> None:
    matrix = _json(ROOT / "expected_matrix.json")
    cases = _cases()
    case_ids = [case.get("id") for case in cases]

    assert matrix["schemaVersion"] == "frisket.workbench_portability_contract_matrix.v1"
    assert case_ids and all(
        isinstance(case_id, str) and case_id for case_id in case_ids
    )
    assert len(case_ids) == len(set(case_ids))
    assert set(case_ids) >= set(matrix["requiredCases"])
    assert all(BEHAVIORAL_EXPECTATION_KEYS.intersection(case) for case in cases)

    descriptors = [descriptor for case in cases for descriptor in _descriptors(case)]
    descriptor_kinds = {
        descriptor["kind"]
        for descriptor in descriptors
        if isinstance(descriptor.get("kind"), str)
    }
    placements = [
        placement
        for descriptor in descriptors
        for placement in descriptor.get("placements", [])
        if isinstance(placement, dict)
    ]
    declared_json = json.dumps(cases, sort_keys=True)
    assert descriptor_kinds >= set(matrix["requiredDescriptorKinds"])
    assert {key for placement in placements for key in placement} >= set(
        matrix["requiredPlacementKeys"]
    )
    for inventory in ("requiredHostCapabilities", "disabledReasonCodes"):
        assert all(json.dumps(item) in declared_json for item in matrix[inventory])
    assert all(placement.get("host") in LEGAL_HOSTS for placement in placements)
    persisted_region_hosts = {
        host
        for case in cases
        if case["id"] != "malformed_layout_recovery"
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
        persisted = (case.get("projectProfile", {}), case.get("layout", {}))
        assert all(
            not any(_contains_key(value, key) for value in persisted)
            for key in RUNTIME_ONLY_STATE_KEYS.intersection(runtime_state)
        )


def test_workbench_portability_manifest_projection_runtime_boundaries() -> None:
    matrix = _json(ROOT / "expected_matrix.json")
    runtime_keys = set(matrix["runtimeOnlyFields"])
    saw_runtime_key = set()

    for case in _cases():
        for descriptor in _descriptors(case):
            raw_runtime_keys = {
                key for key in runtime_keys if _contains_key(descriptor, key)
            }
            saw_runtime_key |= raw_runtime_keys
            manifest = project_manifest(descriptor)
            for key in raw_runtime_keys:
                assert not _contains_key(manifest, key), (
                    f"{case['id']} projected manifest leaked runtime key {key}"
                )
            if raw_runtime_keys:
                assert {"id", "kind", "placements"}.issubset(manifest)

    assert saw_runtime_key >= {"componentKey", "handlerKey", "lazyImport", "callback"}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(ROOT.glob("*.json")):
        if path.name == "expected_matrix.json":
            continue
        payload = _json(path)
        assert isinstance(payload, dict)
        assert payload.get("schemaVersion") == (
            "frisket.workbench_portability_contract_fixtures.v1"
        )
        assert isinstance(payload.get("cases"), list)
        assert all(isinstance(case, dict) for case in payload["cases"])
        cases.extend(payload["cases"])
    return cases


def _case(case_id: str) -> dict[str, Any]:
    for case in _cases():
        if case["id"] == case_id:
            return case
    raise AssertionError(f"fixture case not found: {case_id}")


def _descriptors(case: dict[str, Any]) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    raw = case.get("rawDescriptor")
    if isinstance(raw, dict):
        descriptors.append(raw)
    for key in ("sourceKindForms", "actionForms", "commands", "descriptors"):
        value = case.get(key)
        if isinstance(value, list):
            descriptors.extend(item for item in value if isinstance(item, dict))
    return descriptors


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(
            _contains_key(nested, key) for nested in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False

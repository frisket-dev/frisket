from __future__ import annotations

import json
from pathlib import Path

from frisket.authoring.workbench.contracts import project_manifest


ROOT = Path(__file__).parent.parent / "fixtures" / "workbench_portability"


def test_descriptor_slot_parity_and_strict_placement_validation() -> None:
    descriptors = _descriptors()
    by_id = {descriptor["id"]: descriptor for descriptor in descriptors}

    for descriptor in descriptors:
        for placement in descriptor.get("placements", []):
            assert "slot" in placement, descriptor["id"]
            assert "placementId" in placement, descriptor["id"]

    map_manifest = project_manifest(by_id["frisket.geo.view.map"])
    assert map_manifest["placements"][0]["slot"] == "work.companion"
    assert map_manifest["dataRequirements"][1]["kind"] == "sheetHasColumnType"

    facets_manifest = project_manifest(
        by_id["frisket.investigative.panel.friendly_filters"]
    )
    optional = [
        req
        for req in facets_manifest["requires"]
        if req["id"] == "host.navigation.openEvidence"
    ]
    assert optional == [
        {
            "kind": "hostCapability",
            "id": "host.navigation.openEvidence",
            "optional": True,
        }
    ]


def _descriptors() -> list[dict]:
    descriptors: list[dict] = []
    for path in sorted(ROOT.glob("*.json")):
        if path.name == "expected_matrix.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for case in payload.get("cases", []):
            raw = case.get("rawDescriptor")
            if isinstance(raw, dict):
                descriptors.append(raw)
            for key in ("sourceKindForms", "actionForms", "commands", "descriptors"):
                value = case.get(key)
                if isinstance(value, list):
                    descriptors.extend(item for item in value if isinstance(item, dict))
    return descriptors

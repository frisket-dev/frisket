from __future__ import annotations

import json
from pathlib import Path

from frisket.authoring.workbench.contracts import (
    project_manifest,
    resolve_contributions,
)


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

    bad_slot = resolve_contributions(
        [by_id["frisket.example.panel.bad_slot"]],
        installed_plugins={"frisket.example"},
        trusted_plugins={"frisket.example"},
        host_capabilities=set(),
        data_context={},
        hidden_contributions=set(),
    )
    assert bad_slot["frisket.example.panel.bad_slot"].status == "disabled"
    assert bad_slot["frisket.example.panel.bad_slot"].reasons[0]["code"] == (
        "unknown_slot"
    )
    assert bad_slot["frisket.example.panel.bad_slot"].legal_placements == []
    assert bad_slot["frisket.example.panel.bad_slot"].active_placement is None

    map_without_geo = resolve_contributions(
        [by_id["frisket.geo.view.map"]],
        installed_plugins={"frisket.geo"},
        trusted_plugins={"frisket.geo"},
        host_capabilities={"grid.filter.applyBbox", "projection.status"},
        data_context={
            "activeSheet": True,
            "columnTypes": {"name": "frisket.core.column_type.text"},
        },
        hidden_contributions=set(),
    )
    assert map_without_geo["frisket.geo.view.map"].status == "disabled"
    assert map_without_geo["frisket.geo.view.map"].reasons[0]["code"] == (
        "data_requirement_unmet"
    )


def test_unsupported_placement_host_and_mode_disable_contribution() -> None:
    bad_host = {
        "schemaVersion": "frisket.workbench.panel.v1",
        "id": "frisket.example.panel.bad_host",
        "kind": "panel",
        "ownerPluginId": "frisket.example",
        "title": "Bad host",
        "placements": [
            {
                "host": "right",
                "mode": "panel",
                "slot": "inspection",
                "placementId": "bad-host",
                "default": True,
            }
        ],
    }
    bad_mode = {
        **bad_host,
        "id": "frisket.example.panel.bad_mode",
        "placements": [
            {
                "host": "rightInspector",
                "mode": "teleport",
                "slot": "inspection",
                "placementId": "bad-mode",
                "default": True,
            }
        ],
    }
    resolved = resolve_contributions(
        [bad_host, bad_mode],
        installed_plugins={"frisket.example"},
        trusted_plugins={"frisket.example"},
        host_capabilities=set(),
        data_context={},
        hidden_contributions=set(),
    )

    assert resolved["frisket.example.panel.bad_host"].status == "disabled"
    assert resolved["frisket.example.panel.bad_host"].reasons[0]["code"] == (
        "unsupported_host"
    )
    assert resolved["frisket.example.panel.bad_host"].legal_placements == []
    assert resolved["frisket.example.panel.bad_host"].active_placement is None
    assert resolved["frisket.example.panel.bad_mode"].status == "disabled"
    assert resolved["frisket.example.panel.bad_mode"].reasons[0]["code"] == (
        "unsupported_mode"
    )
    assert resolved["frisket.example.panel.bad_mode"].legal_placements == []
    assert resolved["frisket.example.panel.bad_mode"].active_placement is None


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

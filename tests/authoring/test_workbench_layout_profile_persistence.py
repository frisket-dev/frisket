from __future__ import annotations

import json
from pathlib import Path

from frisket.authoring.workbench.contracts import (
    resolve_contributions,
)


ROOT = Path(__file__).parent.parent / "fixtures" / "workbench_portability"


def test_hidden_and_missing_contributions_remain_distinct() -> None:
    descriptors = _descriptors()
    by_id = {descriptor["id"]: descriptor for descriptor in descriptors}

    hidden = resolve_contributions(
        [by_id["frisket.geo.view.map"]],
        installed_plugins={"frisket.geo"},
        trusted_plugins={"frisket.geo"},
        host_capabilities={"grid.filter.applyBbox", "projection.status"},
        data_context={
            "activeSheet": True,
            "columnTypes": {"geo": "frisket.core.column_type.geo_point"},
        },
        hidden_contributions={"frisket.geo.view.map"},
    )
    assert hidden["frisket.geo.view.map"].status == "hidden"
    assert hidden["frisket.geo.view.map"].reasons[0]["code"] == "hidden_by_profile"

    missing = resolve_contributions(
        [by_id["frisket.geo.view.map"]],
        installed_plugins={"frisket.core"},
        trusted_plugins={"frisket.core"},
        host_capabilities={"grid.filter.applyBbox", "projection.status"},
        data_context={
            "activeSheet": True,
            "columnTypes": {"geo": "frisket.core.column_type.geo_point"},
        },
        hidden_contributions=set(),
    )
    assert missing["frisket.geo.view.map"].status == "missing"
    assert missing["frisket.geo.view.map"].reasons[0]["code"] == "missing_plugin"


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

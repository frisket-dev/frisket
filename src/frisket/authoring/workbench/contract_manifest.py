from __future__ import annotations

import json

from frisket.contracts.plugin import RESERVED_PLUGIN_IDS
from frisket.authoring.workbench.contracts import (
    CAPABILITY_DECLARED_ONLY,
    CAPABILITY_DEFAULTS,
    CONTEXT_SCHEMA_VERSIONS,
    DATA_REQUIREMENT_KINDS,
    DESCRIPTOR_SCHEMA_VERSIONS,
    PLUGIN_LEGAL_PLACEMENTS,
    WORKBENCH_SLOT_BY_HOST,
)

GENERATED_BY = "scripts/ci/gen_contract_manifest.py — do not hand-edit"


def build_contract_manifest() -> dict:
    """Pure function: the JSON-serializable contract manifest.

    Every value here is read from src/frisket/authoring/workbench/contracts.py and
    src/frisket/contracts/plugin.py — nothing is duplicated inline. Key
    order matches the historical sdk/contract-manifest.json layout (list/
    dict source order is preserved; render_contract_manifest sorts keys for
    a stable, generator-agnostic diff).
    """
    return {
        "generatedBy": GENERATED_BY,
        "placements": {
            kind: [dict(entry) for entry in entries]
            for kind, entries in PLUGIN_LEGAL_PLACEMENTS.items()
        },
        "slotByHost": dict(WORKBENCH_SLOT_BY_HOST),
        "dataRequirementKinds": list(DATA_REQUIREMENT_KINDS),
        "capabilityDefaults": {
            kind: list(values) for kind, values in CAPABILITY_DEFAULTS.items()
        },
        # Available-but-never-auto-defaulted capabilities (declared-only —
        # see contracts.py CAPABILITY_DECLARED_ONLY). The frontend's full
        # AVAILABLE capability set per host family is capabilityDefaults[kind]
        # UNION capabilityDeclaredOnly.get(kind, []).
        "capabilityDeclaredOnly": {
            kind: list(values) for kind, values in CAPABILITY_DECLARED_ONLY.items()
        },
        "contextSchemaVersions": list(CONTEXT_SCHEMA_VERSIONS),
        "descriptorSchemaVersions": dict(DESCRIPTOR_SCHEMA_VERSIONS),
        "reservedPluginIds": sorted(RESERVED_PLUGIN_IDS),
    }


def render_contract_manifest(manifest: dict) -> str:
    """The single formatting rule shared by the writer and the freshness
    test: 2-space indent, alphabetically sorted keys (recursively — key
    order is not a contract value; array order is preserved), trailing
    newline."""
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

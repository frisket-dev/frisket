from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "sdk" / "contract-manifest.json"
WEB_WORKBENCH = REPO_ROOT / "web" / "src" / "workbench"

_spec = importlib.util.spec_from_file_location(
    "test_workbench_placement_policy",
    REPO_ROOT / "tests" / "authoring" / "test_workbench_placement_policy.py",
)
_policy = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_policy)
_plugin_legal_placement_pairs = _policy._plugin_legal_placement_pairs


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_contract_manifest_matches_the_python_contract_source() -> None:
    from frisket.authoring.workbench.contract_manifest import (
        build_contract_manifest,
        render_contract_manifest,
    )

    expected = render_contract_manifest(build_contract_manifest())
    actual = MANIFEST_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{MANIFEST_PATH} is stale relative to the Python contract source "
        "(src/frisket/authoring/workbench/contracts.py, src/frisket/contracts/plugin.py). "
        "Run `uv run python scripts/ci/gen_contract_manifest.py`."
    )


def test_sdk_runtime_bundle_is_the_vendored_cli_copy() -> None:
    sdk_dist = REPO_ROOT / "sdk" / "dist" / "index.mjs"
    if not sdk_dist.exists():
        # sdk/dist is a build output (gitignored), absent in a python-only env
        # like CI's `test` job. The build↔vendor sync this asserts is enforced
        # by the oracle's `npm --prefix sdk run build` checks
        # (plugin-sdk-define-api-v1 / contract-manifest-generated-from-python-v1);
        # run that build to exercise this locally. Skip (don't fail) when the
        # artifact was never built — the assertion still runs post-build.
        pytest.skip("sdk/dist/index.mjs not built (run `npm --prefix sdk run build`)")
    sdk_bundle = sdk_dist.read_bytes()
    # rule19: two-sources: built sdk/dist bytes diffed against the vendored plugin_sdk.mjs
    vendored = (REPO_ROOT / "src" / "frisket" / "data" / "plugin_sdk.mjs").read_bytes()
    assert sdk_bundle == vendored


def test_frontend_tables_match_the_manifest() -> None:
    manifest = _manifest()

    # placements: web/src/workbench/pluginRuntimeDescriptors.ts's
    # PLUGIN_LEGAL_PLACEMENTS, parsed by the focused helper in
    # test_workbench_placement_policy.py.
    frontend_pairs = _plugin_legal_placement_pairs()
    manifest_pairs = {
        kind: {(entry["host"], entry["mode"]) for entry in entries}
        for kind, entries in manifest["placements"].items()
    }
    assert frontend_pairs == manifest_pairs

    # rule19: two-sources: web descriptors.ts values diffed against the Python-generated manifest
    descriptors_ts = (WEB_WORKBENCH / "descriptors.ts").read_text(encoding="utf-8")

    # slotByHost: descriptors.ts's WORKBENCH_SLOT_BY_HOST (values, not just
    # keys — a drifted-but-canonical slot would silently mis-slot SDK-built
    # contributions).
    start = descriptors_ts.index("export const WORKBENCH_SLOT_BY_HOST")
    block = descriptors_ts[start : descriptors_ts.index("};", start)]
    frontend_slot_by_host = {}
    for line in block.splitlines():
        line = line.strip().rstrip(",")
        if ":" in line and line.endswith("'"):
            host, slot = line.split(":", 1)
            frontend_slot_by_host[host.strip()] = slot.strip().strip("'")
    assert manifest["slotByHost"] == frontend_slot_by_host

    # dataRequirementKinds: descriptors.ts's WorkbenchDataRequirement union.
    start = descriptors_ts.index("export interface WorkbenchDataRequirement")
    block = descriptors_ts[start : descriptors_ts.index("}", start)]
    frontend_kinds = {
        line.strip().strip("|").strip().rstrip(";").strip("'")
        for line in block.splitlines()
        if line.strip().startswith("|")
    }
    frontend_kinds.discard("")
    assert set(manifest["dataRequirementKinds"]) == frontend_kinds

    # capabilityDefaults: pluginPanelContext.ts / pluginViewContext.ts /
    # pluginProjectionViewContext.ts DEFAULT_PLUGIN_*_CAPABILITIES. Each
    # frontend DEFAULT_PLUGIN_*_CAPABILITIES constant is the host's full
    # AVAILABLE set for that family, not just the SDK auto-default set: since
    # plugin-host-library-injection-v1 it is the UNION of
    # capabilityDefaults[kind] and capabilityDeclaredOnly[kind]
    # (host.library.deckgl is available at the host but deliberately absent
    # from capabilityDefaults — see contracts.py CAPABILITY_DECLARED_ONLY for
    # the rationale). Panel has no declared-only capabilities yet, so its
    # union is a no-op today.
    def extract(path: Path, const_name: str) -> set[str]:
        text = path.read_text(encoding="utf-8")
        # Match with or without `export`: DEFAULT_PLUGIN_VIEW_CAPABILITIES lost
        # its export in the dead-export sweep (61421d9d, single-file use only,
        # same genre as ACTION_PLACEMENTS in web/src/actions/model.ts, whose
        # behavior is owned by action-placement-single-source.spec.ts) — the
        # single-source DECLARATION is the intent this pin protects, not the
        # export keyword.
        start = text.index(f"const {const_name}")
        block = text[start : text.index("];", start)]
        return {
            line.strip().rstrip(",").strip("'")
            for line in block.splitlines()
            if line.strip().startswith("'")
        }

    declared_only = manifest.get("capabilityDeclaredOnly", {})

    def available(kind: str) -> set[str]:
        return set(manifest["capabilityDefaults"][kind]) | set(
            declared_only.get(kind, [])
        )

    assert available("panel") == extract(
        WEB_WORKBENCH / "pluginPanelContext.ts", "DEFAULT_PLUGIN_PANEL_CAPABILITIES"
    )
    assert available("view") == extract(
        WEB_WORKBENCH / "pluginViewContext.ts", "DEFAULT_PLUGIN_VIEW_CAPABILITIES"
    )
    assert available("projectionView") == extract(
        WEB_WORKBENCH / "pluginProjectionViewContext.ts",
        "DEFAULT_PLUGIN_PROJECTION_VIEW_CAPABILITIES",
    )

    # contextSchemaVersions: the seven v1 host context files' schemaVersion
    # literals.
    shipped_schema_versions: set[str] = set()
    for name in [
        "pluginViewContext.ts",
        "pluginPanelContext.ts",
        "pluginDockTabContext.ts",
        "pluginDetailContext.ts",
        "pluginPeekContext.ts",
        "pluginCommandContext.ts",
        "pluginProjectionViewContext.ts",
    ]:
        # rule19: two-sources: context schemaVersion literals diffed against the generated manifest
        text = (WEB_WORKBENCH / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "schemaVersion: 'frisket.plugin_" in line:
                shipped_schema_versions.add(line.split("'")[1])
    assert set(manifest["contextSchemaVersions"]) == shipped_schema_versions


def test_sdk_grid_shapes_mirror_the_host_types() -> None:
    """Review F1: the SDK's grid filter/sort mirrors must match the shapes the
    host passes through unchanged (GridFilterSpec/GridSortSpec,
    web/src/api/types.ts). Textual pins on the load-bearing lines. Grid
    state is host-owned pass-through data with no Python representation —
    out of scope for the Python-generation cutover (the dialect split non-goals)."""
    # rule19: two-sources: web api types diffed against sdk contexts (parity both ways)
    web_types = (REPO_ROOT / "web" / "src" / "api" / "types.ts").read_text(
        encoding="utf-8"
    )
    # rule19: two-sources: sdk contexts diffed against web api types (parity both ways)
    sdk_contexts = (REPO_ROOT / "sdk" / "src" / "contexts.ts").read_text(
        encoding="utf-8"
    )
    for load_bearing in (
        "export type GridFilterSpec = Record<string, Partial<Record<GridFilterOperator, GridFilterValue>>>;",
        "export type GridSortSpec = GridSortRule[];",
        "export type GridFilterValue =",
        "  | GridFilterEntityValue",
        # Interface BODIES too: renaming a field inside these must fail here.
        "  column: string;",
        "  dir: GridSortDirection;",
        "  start: string;",
        "  end: string;",
        "  min_lon: number;",
        "  max_lat: number;",
        # A structured filter payload crossing the plugin boundary: a plugin
        # that reads the grid filter must be able to see an entity selector
        # rather than receiving an object it has no type for.
        "  fingerprint?: string;",
        "  amount: number;",
        "  unit: 'days' | 'weeks' | 'months';",
    ):
        assert load_bearing in web_types, load_bearing
        assert load_bearing in sdk_contexts, load_bearing
    # The fragment consumes the spec types, not bespoke snapshots.
    assert "filter: GridFilterSpec | null;" in sdk_contexts
    assert "sort: GridSortSpec | null;" in sdk_contexts
    assert "GridFilterSnapshot" not in sdk_contexts

    # Operator unions stay equal.
    def operators(text: str) -> set[str]:
        start = text.index("export type GridFilterOperator =")
        block = text[start : text.index(";", start)]
        return {
            line.strip().strip("|").strip().strip("'")
            for line in block.splitlines()
            if line.strip().startswith("|")
        }

    assert operators(sdk_contexts) == operators(web_types)

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.contracts.plugin import (
    PLUGIN_MANIFEST_SCHEMA_VERSION,
)


FRONTEND_COMPONENT_BINDING_SCHEMA_VERSION = (
    "frisket.workbench_plugin_frontend_component_binding.v1"
)
PLUGIN_INSTALL_STATE_SCHEMA_VERSION = "frisket.workbench_plugin_install_state.v1"
RUNTIME_INDEX_SCHEMA_VERSION = "frisket.workbench_plugin_runtime_index.v1"
RUNTIME_PLUGIN_SCHEMA_VERSION = "frisket.workbench_plugin_runtime_plugin.v1"
WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION = "frisket.workbench_descriptor_package.v1"
WORKBENCH_PANEL_SCHEMA_VERSION = "frisket.workbench.panel.v1"
WORKBENCH_VIEW_SCHEMA_VERSION = "frisket.workbench.view.v1"
WORKBENCH_COMMAND_SCHEMA_VERSION = "frisket.command.v1"
MAX_WORKBENCH_DESCRIPTOR_PACKAGE_BYTES = 1_000_000

CANONICAL_REGIONS = {
    "activityRail",
    "leftSidebar",
    "rightInspector",
    "bottomDock",
    "mainView",
    "modalOrPeek",
}
DETAIL_HOSTS = {"rowDetail", "entityDetail", "sourceDetail", "columnDetail"}
OTHER_HOSTS = {"rowInspector", "columnInspector", "commandPalette"}
LEGAL_HOSTS = CANONICAL_REGIONS | DETAIL_HOSTS | OTHER_HOSTS
TAB_HOSTS = {"mainView", "bottomDock"} | DETAIL_HOSTS
LEGAL_MODES = {"panel", "pane", "tab", "peek", "section", "command"}
CANONICAL_SLOTS = {
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
FORBIDDEN_TAB_CONTRACTS = {"BottomTab", "MainViewTab", "DetailTab"}
RUNTIME_ONLY_FIELDS = {
    "componentKey",
    "handlerKey",
    "lazyImport",
    "component",
    "callback",
}

# ---------------------------------------------------------------------------
# Plugin contract source-of-truth tables. These are the Python originals for
# the tables that used to be hand-maintained only in sdk/src/contracts.ts
# and mirrored into sdk/contract-manifest.json by the SDK build.
# src/frisket/authoring/workbench/contract_manifest.py reads them to build the
# manifest; scripts/ci/gen_contract_manifest.py writes it;
# sdk/scripts/gen-contracts.mjs codegens sdk/src/contracts.gen.ts FROM the
# manifest. The frontend's own hand-written copy
# (web/src/workbench/pluginRuntimeDescriptors.ts and the other
# web/src/workbench/*.ts tables) is pinned against the manifest by
# tests/test_plugin_sdk_contract_parity.py.
# ---------------------------------------------------------------------------

# The kind-keyed placement allowlist: which host/mode pairs a plugin
# panel/view/command may declare. Every pair must be legal per LEGAL_HOSTS/
# LEGAL_MODES above (asserted below) — this is the single change point new
# placement cohorts add a row to.
PLUGIN_LEGAL_PLACEMENTS: dict[str, list[dict[str, str]]] = {
    "panel": [
        {"host": "rightInspector", "mode": "panel"},
        {"host": "bottomDock", "mode": "tab"},
        {"host": "leftSidebar", "mode": "panel"},
        {"host": "rowDetail", "mode": "tab"},
        {"host": "columnDetail", "mode": "tab"},
        {"host": "columnInspector", "mode": "section"},
        {"host": "entityDetail", "mode": "tab"},
        {"host": "sourceDetail", "mode": "tab"},
        {"host": "modalOrPeek", "mode": "peek"},
        {"host": "activityRail", "mode": "command"},
    ],
    "view": [
        {"host": "mainView", "mode": "pane"},
        {"host": "activityRail", "mode": "command"},
    ],
    "command": [
        {"host": "commandPalette", "mode": "command"},
    ],
}
assert all(
    entry["host"] in LEGAL_HOSTS and entry["mode"] in LEGAL_MODES
    for entries in PLUGIN_LEGAL_PLACEMENTS.values()
    for entry in entries
), "PLUGIN_LEGAL_PLACEMENTS declares a host/mode pair outside LEGAL_HOSTS/LEGAL_MODES"

# The default placement slot for each host (a placement may override it).
WORKBENCH_SLOT_BY_HOST: dict[str, str] = {
    "activityRail": "launcher",
    "leftSidebar": "scope",
    "mainView": "work.primary",
    "rightInspector": "inspection",
    "bottomDock": "companion.output",
    "modalOrPeek": "interruption",
    "rowDetail": "detail",
    "entityDetail": "detail",
    "sourceDetail": "detail",
    "columnDetail": "detail",
    "rowInspector": "detail",
    "columnInspector": "detail",
    "commandPalette": "launcher",
}
assert set(WORKBENCH_SLOT_BY_HOST) == LEGAL_HOSTS, (
    "WORKBENCH_SLOT_BY_HOST must cover every legal host"
)
assert set(WORKBENCH_SLOT_BY_HOST.values()) <= CANONICAL_SLOTS, (
    "WORKBENCH_SLOT_BY_HOST must only use canonical slots"
)

# Every dataRequirements[].kind a plugin descriptor may declare. The first
# nine are context-presence kinds (host context carries a truthy value keyed
# by the kind); selectedRows/sheetHasColumnType are evaluated structurally
# (see _first_missing_data_requirement below).
DATA_REQUIREMENT_KINDS: tuple[str, ...] = (
    "activeProject",
    "activeSheet",
    "activeRow",
    "activeColumn",
    "activeCell",
    "activeEvidence",
    "activeSource",
    "activeEntity",
    "activeProjection",
    "selectedRows",
    "sheetHasColumnType",
)

# Host-capability defaults per host family — the capabilities an SDK-defined
# panel/view/projectionView gets without explicitly declaring `requires`.
CAPABILITY_DEFAULTS: dict[str, list[str]] = {
    "panel": [
        "sheet.active",
        "selection.rows",
        "host.navigation.openRow",
        "grid.state.read",
        "action.run",
    ],
    "view": [
        "sheet.rows.read",
        "media.blob.resolve",
        "host.navigation.openRow",
        "grid.state.read",
        "grid.filter.applyBbox",
        "action.run",
    ],
    "projectionView": [
        "projection.status",
        "projection.build",
        "projection.artifact.read",
        "projection.data.read",
        "host.navigation.openRow",
        "grid.state.read",
        # The projection-view family wires the same grid-filter fragment
        # plain views get, so a projection view (the geo map) can apply the
        # canonical {geo_col: {bbox}} filter — "filter to this area".
        "grid.filter.applyBbox",
        "action.run",
    ],
}

# Host capabilities that are AVAILABLE (the host can genuinely grant them —
# declaring one never fails a contribution closed with missing_capability)
# but never auto-defaulted: unlike everything in CAPABILITY_DEFAULTS above, a
# plugin author must explicitly write `requires: [{kind:'hostCapability',
# id:...}]` (capability(...) in the SDK) to get one — the SDK's own default-
# requires generation (sdk/src/define.ts) never adds these on its own.
# `host.library.deckgl` is the first entry: the host
# lazily injects ctx.libs.deckgl (the deck.gl namespace, dynamic-imported
# from the SAME chunk the first-party map ships —
# web/src/components/map/deckglNamespace.ts) on view/projectionView contexts
# IFF declared. It stays out of CAPABILITY_DEFAULTS on purpose — deck.gl is
# ~780KB; every SDK-built view/projectionView getting it "for free" would
# undercut the entire reason this capability exists (working around the
# 256,000-byte plugin-module cap without every third-party author having to
# think about it). The frontend's full AVAILABLE set
# (web/src/workbench/pluginViewContext.ts DEFAULT_PLUGIN_VIEW_CAPABILITIES /
# pluginProjectionViewContext.ts DEFAULT_PLUGIN_PROJECTION_VIEW_CAPABILITIES)
# is therefore CAPABILITY_DEFAULTS[family] | CAPABILITY_DECLARED_ONLY[family]
# — the union, not CAPABILITY_DEFAULTS alone.
CAPABILITY_DECLARED_ONLY: dict[str, list[str]] = {
    "view": ["host.library.deckgl"],
    "projectionView": ["host.library.deckgl"],
}

# The schema_version literals stamped on the seven v1 plugin host contexts.
CONTEXT_SCHEMA_VERSIONS: tuple[str, ...] = (
    "frisket.plugin_view_context.v1",
    "frisket.plugin_panel_context.v1",
    "frisket.plugin_dock_tab_context.v1",
    "frisket.plugin_detail_context.v1",
    "frisket.plugin_peek_context.v1",
    "frisket.plugin_command_context.v1",
    "frisket.plugin_projection_view_context.v1",
)

# schemaVersion literals for the descriptor/manifest document kinds a plugin
# package emits.
DESCRIPTOR_SCHEMA_VERSIONS: dict[str, str] = {
    "plugin": PLUGIN_MANIFEST_SCHEMA_VERSION,
    "descriptorPackage": WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION,
    "panel": WORKBENCH_PANEL_SCHEMA_VERSION,
    "view": WORKBENCH_VIEW_SCHEMA_VERSION,
    "command": WORKBENCH_COMMAND_SCHEMA_VERSION,
}


@dataclass(frozen=True)
class WorkbenchResolvedContribution:
    id: str
    kind: str
    schema_version: str | None
    descriptor: dict[str, Any] | None
    status: str
    reasons: list[dict[str, str]]
    legal_placements: list[dict[str, Any]]
    active_placement: dict[str, Any] | None
    activation: str


@dataclass(frozen=True)
class LoadedWorkbenchDescriptorPackage:
    schema_version: str
    sha256: str
    byte_count: int
    descriptor_manifests: list[dict[str, Any]]
    runtime_only_fields_stripped: list[str]


class WorkbenchDescriptorPackageLoadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_activity_recovery_entries(
    resolved: dict[str, WorkbenchResolvedContribution],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for contribution in resolved.values():
        if (
            contribution.status not in {"hidden", "disabled", "missing"}
            and contribution.activation != "failed"
        ):
            continue
        reason = contribution.reasons[0]["code"] if contribution.reasons else "unknown"
        actions = ["reveal", "move_to_mainView", "reset_profile"]
        if contribution.status == "missing":
            actions = ["install_plugin", "remove_from_layout", "replace_with_grid"]
        elif contribution.activation == "failed":
            actions = ["retry_activation", "disable", "remove_from_layout"]
        entries.append(
            {
                "schemaVersion": "frisket.activity_recovery_entry.v1",
                "contributionId": contribution.id,
                "status": contribution.status,
                "activation": contribution.activation,
                "reason": reason,
                "actions": actions,
            }
        )
    return sorted(entries, key=lambda item: item["contributionId"])


def load_fixture_descriptors(root: str | Path) -> list[dict[str, Any]]:
    fixture_root = Path(root)
    descriptors: list[dict[str, Any]] = []
    for path in sorted(fixture_root.glob("*.json")):
        if path.name == "expected_matrix.json":
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        for case in document.get("cases", []):
            if not isinstance(case, dict):
                continue
            descriptors.extend(_iter_descriptors(case))
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for descriptor in descriptors:
        descriptor_id = descriptor.get("id")
        if not isinstance(descriptor_id, str) or descriptor_id in seen:
            continue
        seen.add(descriptor_id)
        unique.append(descriptor)
    return unique


def project_manifest(descriptor: dict[str, Any]) -> dict[str, Any]:
    manifest = {
        key: _strip_runtime_fields(value)
        for key, value in descriptor.items()
        if key not in RUNTIME_ONLY_FIELDS
    }
    try:
        json.dumps(manifest, sort_keys=True)
    except TypeError as exc:
        raise ValueError(
            f"descriptor {descriptor.get('id')!r} contains non-JSON values"
        ) from exc
    return manifest


def load_workbench_descriptor_package_file(
    path: Path,
) -> LoadedWorkbenchDescriptorPackage:
    try:
        if path.is_symlink():
            raise WorkbenchDescriptorPackageLoadError(
                "invalid_workbench_descriptor_package_source",
                "workbench descriptor package must be a regular file, not a symlink",
            )
        if not path.is_file():
            raise WorkbenchDescriptorPackageLoadError(
                "invalid_workbench_descriptor_package_source",
                "workbench descriptor package path must point to a readable file",
            )
        if path.stat().st_size > MAX_WORKBENCH_DESCRIPTOR_PACKAGE_BYTES:
            raise WorkbenchDescriptorPackageLoadError(
                "invalid_workbench_descriptor_package_source",
                "workbench descriptor package exceeds the v1 validation limit",
            )
        data = path.read_bytes()
        if len(data) > MAX_WORKBENCH_DESCRIPTOR_PACKAGE_BYTES:
            raise WorkbenchDescriptorPackageLoadError(
                "invalid_workbench_descriptor_package_source",
                "workbench descriptor package exceeds the v1 validation limit",
            )
    except OSError as exc:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package_source", str(exc)
        ) from exc
    try:
        payload: Any = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            f"workbench descriptor package is not valid JSON: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "workbench descriptor package must be a JSON object",
        )
    if payload.get("schemaVersion") != WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "workbench descriptor package schemaVersion is not supported",
        )
    raw_descriptors = payload.get("descriptors")
    if not isinstance(raw_descriptors, list) or not raw_descriptors:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "workbench descriptor package must include at least one descriptor",
        )
    descriptors = [item for item in raw_descriptors if isinstance(item, dict)]
    if len(descriptors) != len(raw_descriptors):
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "workbench descriptor package descriptors must be objects",
        )

    errors: list[str] = []
    runtime_only_fields_stripped: set[str] = set()
    forbidden_tab_contracts: set[str] = set()
    legacy_regions: set[str] = set()
    duplicate_ids = _duplicate_values(
        str(descriptor.get("id", ""))
        for descriptor in descriptors
        if isinstance(descriptor.get("id"), str)
    )
    if duplicate_ids:
        errors.append(
            "workbench descriptor package has duplicate descriptor ids: "
            + ", ".join(sorted(duplicate_ids))
        )
    for descriptor in descriptors:
        descriptor_id = descriptor.get("id")
        _validate_descriptor(
            path.name,
            str(descriptor_id or "<missing>"),
            descriptor,
            errors=errors,
            runtime_only_fields_stripped=runtime_only_fields_stripped,
            forbidden_tab_contracts=forbidden_tab_contracts,
            legacy_regions=legacy_regions,
        )
    if forbidden_tab_contracts:
        errors.append(
            "workbench descriptor package uses forbidden tab-specific contracts: "
            + ", ".join(sorted(forbidden_tab_contracts))
        )
    if legacy_regions:
        errors.append(
            "workbench descriptor package uses unsupported hosts: "
            + ", ".join(sorted(legacy_regions))
        )
    if errors:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package", errors[0]
        )

    return LoadedWorkbenchDescriptorPackage(
        schema_version=WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION,
        sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        descriptor_manifests=[
            project_manifest(descriptor) for descriptor in descriptors
        ],
        runtime_only_fields_stripped=sorted(runtime_only_fields_stripped),
    )


# First-party workbench descriptor DATA lives in this checked-in package,
# validated by the SAME loader plugin packages
# go through above — first-party descriptors can no longer use shapes
# plugins couldn't. The web app imports this exact file directly (Vite
# relative import, web/src/workbench/descriptors.ts); runtime-only fields
# (componentKey, handlerKey) are deliberately absent from the artifact —
# binding moves to the in-bundle registry (web/src/workbench/
# firstPartyComponents.ts).
FIRST_PARTY_WORKBENCH_DESCRIPTOR_PACKAGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "first_party_workbench_descriptors.json"
)


def load_first_party_workbench_descriptor_package() -> LoadedWorkbenchDescriptorPackage:
    """Load and validate the checked-in first-party descriptor package.

    Raises WorkbenchDescriptorPackageLoadError exactly like a plugin package
    would if the artifact regressed to an invalid shape — there is no
    separate, more permissive validation path for first-party data.
    """
    return load_workbench_descriptor_package_file(
        FIRST_PARTY_WORKBENCH_DESCRIPTOR_PACKAGE_PATH
    )


def resolve_contributions(
    descriptors: list[dict[str, Any]],
    *,
    installed_plugins: set[str],
    trusted_plugins: set[str],
    host_capabilities: set[str],
    data_context: dict[str, Any],
    hidden_contributions: set[str],
    granted_permissions: set[str] | None = None,
    activation_errors: dict[str, dict[str, str]] | None = None,
) -> dict[str, WorkbenchResolvedContribution]:
    resolved: dict[str, WorkbenchResolvedContribution] = {}
    activation_errors = activation_errors or {}
    granted_permissions = granted_permissions or set()
    duplicate_ids = _duplicate_values(
        str(descriptor.get("id", ""))
        for descriptor in descriptors
        if isinstance(descriptor.get("id"), str)
    )
    duplicate_id_details = {
        duplicate_id: _duplicate_descriptor_details(descriptors, duplicate_id)
        for duplicate_id in duplicate_ids
    }
    duplicate_aliases = _duplicate_values(
        alias for descriptor in descriptors for alias in _descriptor_aliases(descriptor)
    )
    available_contribution_ids = {
        descriptor["id"]
        for descriptor in descriptors
        if isinstance(descriptor.get("id"), str) and descriptor["id"]
    }
    for index, descriptor in enumerate(descriptors):
        raw_contribution_id = descriptor.get("id")
        contribution_id = (
            raw_contribution_id
            if isinstance(raw_contribution_id, str) and raw_contribution_id
            else f"<invalid:{index}>"
        )
        raw_owner_plugin_id = descriptor.get("ownerPluginId")
        owner_plugin_id = (
            raw_owner_plugin_id
            if isinstance(raw_owner_plugin_id, str) and raw_owner_plugin_id
            else ""
        )
        reasons: list[dict[str, str]] = []
        status = "enabled"
        activation = "loadAllowed"

        descriptor_duplicate_aliases = [
            alias
            for alias in _descriptor_aliases(descriptor)
            if alias in duplicate_aliases
        ]

        if contribution_id.startswith("<invalid:"):
            status = "disabled"
            activation = "blocked"
            reasons.append(
                {
                    "code": "invalid_descriptor_id",
                    "message": "Descriptor is missing a non-empty contribution id.",
                }
            )
        elif not owner_plugin_id:
            status = "disabled"
            activation = "blocked"
            reasons.append(
                {
                    "code": "missing_owner_plugin",
                    "message": "Descriptor is missing ownerPluginId.",
                }
            )
        elif contribution_id in duplicate_ids:
            status = "disabled"
            activation = "blocked"
            details = duplicate_id_details.get(contribution_id, [])
            suffix = f" Conflicts: {'; '.join(details)}." if details else ""
            reasons.append(
                {
                    "code": "duplicate_id",
                    "message": f"Contribution id {contribution_id} is duplicated.{suffix}",
                }
            )
        elif descriptor_duplicate_aliases:
            status = "disabled"
            activation = "blocked"
            reasons.append(
                {
                    "code": "duplicate_alias",
                    "message": f"Contribution alias {descriptor_duplicate_aliases[0]} is duplicated.",
                }
            )
        elif owner_plugin_id and owner_plugin_id not in installed_plugins:
            status = "missing"
            activation = "blocked"
            reasons.append(
                {
                    "code": "missing_plugin",
                    "message": f"Plugin {owner_plugin_id} is not installed.",
                }
            )
        elif owner_plugin_id and owner_plugin_id not in trusted_plugins:
            status = "disabled"
            activation = "blocked"
            reasons.append(
                {
                    "code": "trust_not_granted",
                    "message": f"Plugin {owner_plugin_id} is installed but not trusted.",
                }
            )
        elif unknown_slot := _first_unknown_slot(descriptor):
            status = "disabled"
            activation = "blocked"
            reasons.append(unknown_slot)
        elif unsupported_placement := _first_unsupported_placement(descriptor):
            status = "disabled"
            activation = "blocked"
            reasons.append(unsupported_placement)
        elif contribution_id in hidden_contributions:
            status = "hidden"
            activation = "blocked"
            reasons.append(
                {
                    "code": "hidden_by_profile",
                    "message": "Contribution is hidden by the active workspace profile.",
                }
            )
        else:
            missing_requirement = _first_missing_requirement(
                descriptor,
                host_capabilities,
                granted_permissions,
                available_contribution_ids,
            )
            missing_data = _first_missing_data_requirement(descriptor, data_context)
            if missing_requirement:
                status = "disabled"
                activation = "blocked"
                reasons.append(missing_requirement)
            elif missing_data:
                status = "disabled"
                activation = "blocked"
                reasons.append(missing_data)
            elif contribution_id in activation_errors:
                status = "enabled"
                activation = "failed"
                error = activation_errors[contribution_id]
                reasons.append(
                    {
                        "code": "runtime_activation_failed",
                        "message": error.get(
                            "message", "Contribution runtime activation failed."
                        ),
                    }
                )

        legal_placements = [
            placement
            for placement in descriptor.get("placements", [])
            if isinstance(placement, dict)
            and placement.get("host") in LEGAL_HOSTS
            and placement.get("mode") in LEGAL_MODES
            and (placement.get("mode") != "tab" or placement.get("host") in TAB_HOSTS)
            and ("slot" not in placement or placement.get("slot") in CANONICAL_SLOTS)
        ]
        active_placement = next(
            (placement for placement in legal_placements if placement.get("default")),
            None,
        )
        if active_placement is None and legal_placements:
            active_placement = legal_placements[0]

        resolved[contribution_id] = WorkbenchResolvedContribution(
            id=contribution_id,
            kind=str(descriptor.get("kind", "")),
            schema_version=descriptor.get("schemaVersion")
            if isinstance(descriptor.get("schemaVersion"), str)
            else None,
            descriptor=descriptor,
            status=status,
            reasons=reasons,
            legal_placements=legal_placements,
            active_placement=active_placement,
            activation=activation,
        )
    return resolved


def _duplicate_values(values: Iterable[str]) -> set[str]:
    counts: Counter[str] = Counter(value for value in values if value)
    return {value for value, count in counts.items() if count > 1}


def _duplicate_descriptor_details(
    descriptors: list[dict[str, Any]], contribution_id: str
) -> list[str]:
    details: list[str] = []
    for descriptor in descriptors:
        if descriptor.get("id") != contribution_id:
            continue
        owner = descriptor.get("ownerPluginId")
        schema = descriptor.get("schemaVersion")
        details.append(f"owner={owner!r}, schema={schema!r}")
    return details


def _descriptor_aliases(descriptor: dict[str, Any]) -> list[str]:
    aliases = descriptor.get("aliases")
    if not isinstance(aliases, list):
        return []
    return [alias for alias in aliases if isinstance(alias, str)]


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "open"}
    return bool(value)


def _strip_runtime_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_runtime_fields(nested)
            for key, nested in value.items()
            if key not in RUNTIME_ONLY_FIELDS
        }
    if isinstance(value, list):
        return [_strip_runtime_fields(item) for item in value]
    return value


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(
            _contains_key(nested, key) for nested in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _first_missing_requirement(
    descriptor: dict[str, Any],
    host_capabilities: set[str],
    granted_permissions: set[str],
    available_contribution_ids: set[str],
) -> dict[str, str] | None:
    for requirement in descriptor.get("requires", []):
        if not isinstance(requirement, dict) or requirement.get("optional"):
            continue
        kind = requirement.get("kind")
        requirement_id = requirement.get("id")
        if (
            kind == "hostCapability"
            and isinstance(requirement_id, str)
            and requirement_id not in host_capabilities
        ):
            return {
                "code": "missing_host_capability",
                "message": f"Host capability {requirement_id} is unavailable.",
            }
        if (
            kind == "permission"
            and isinstance(requirement_id, str)
            and requirement_id not in granted_permissions
        ):
            return {
                "code": "missing_permission",
                "message": f"Permission {requirement_id} is unavailable.",
            }
        if (
            kind == "contribution"
            and isinstance(requirement_id, str)
            and requirement_id not in available_contribution_ids
        ):
            return {
                "code": "missing_contribution",
                "message": f"Required contribution {requirement_id} is unavailable.",
            }
    return None


# The context-presence subset of DATA_REQUIREMENT_KINDS (everything except
# the two structurally-evaluated kinds handled separately below).
DATA_REQUIREMENT_CONTEXT_KINDS = frozenset(DATA_REQUIREMENT_KINDS) - {
    "selectedRows",
    "sheetHasColumnType",
}


def _first_missing_data_requirement(
    descriptor: dict[str, Any], data_context: dict[str, Any]
) -> dict[str, str] | None:
    for requirement in descriptor.get("dataRequirements", []):
        if not isinstance(requirement, dict) or requirement.get("optional"):
            continue
        kind = requirement.get("kind")
        if kind in DATA_REQUIREMENT_CONTEXT_KINDS:
            if not data_context.get(kind):
                return {
                    "code": "missing_context",
                    "message": f"Required context {kind} is unavailable.",
                }
        elif kind == "sheetHasColumnType":
            wanted = requirement.get("columnType")
            column_types = data_context.get("columnTypes", {})
            if not isinstance(column_types, dict) or wanted not in set(
                column_types.values()
            ):
                return {
                    "code": "data_requirement_unmet",
                    "message": f"Active sheet does not contain required column type {wanted}.",
                }
        elif kind == "selectedRows":
            selected = data_context.get("selectedRowIds", [])
            try:
                min_rows = int(requirement.get("min", 1))
            except (TypeError, ValueError):
                min_rows = 1
            if not isinstance(selected, list) or len(selected) < min_rows:
                return {
                    "code": "missing_context",
                    "message": f"At least {min_rows} selected row(s) required.",
                }
    return None


def _first_unknown_slot(descriptor: dict[str, Any]) -> dict[str, str] | None:
    for placement in descriptor.get("placements", []):
        if not isinstance(placement, dict) or "slot" not in placement:
            continue
        slot = placement.get("slot")
        if slot not in CANONICAL_SLOTS:
            return {
                "code": "unknown_slot",
                "message": f"Placement slot {slot!r} is not supported.",
            }
    return None


def _first_unsupported_placement(descriptor: dict[str, Any]) -> dict[str, str] | None:
    for placement in descriptor.get("placements", []):
        if not isinstance(placement, dict):
            continue
        host = placement.get("host")
        mode = placement.get("mode")
        if host not in LEGAL_HOSTS:
            return {
                "code": "unsupported_host",
                "message": f"Placement host {host!r} is not supported.",
            }
        if mode not in LEGAL_MODES:
            return {
                "code": "unsupported_mode",
                "message": f"Placement mode {mode!r} is not supported.",
            }
        if mode == "tab" and host not in TAB_HOSTS:
            return {
                "code": "unsupported_mode",
                "message": f"Tab placement is not supported on host {host!r}.",
            }
    return None


def _iter_descriptors(case: dict[str, Any]) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    raw = case.get("rawDescriptor")
    if isinstance(raw, dict):
        descriptors.append(raw)
    for key in ("sourceKindForms", "actionForms", "commands", "descriptors"):
        value = case.get(key)
        if isinstance(value, list):
            descriptors.extend(item for item in value if isinstance(item, dict))
    return descriptors


def _validate_descriptor(
    source_name: str,
    case_id: str,
    descriptor: dict[str, Any],
    *,
    errors: list[str],
    runtime_only_fields_stripped: set[str],
    forbidden_tab_contracts: set[str],
    legacy_regions: set[str],
) -> None:
    contribution_id = descriptor.get("id")
    kind = descriptor.get("kind")
    if kind in FORBIDDEN_TAB_CONTRACTS:
        forbidden_tab_contracts.add(str(kind))
    if isinstance(contribution_id, str) and any(
        part in contribution_id for part in ("BottomTab", "MainViewTab", "DetailTab")
    ):
        forbidden_tab_contracts.add(contribution_id)
    if not isinstance(contribution_id, str) or len(contribution_id.split(".")) < 4:
        errors.append(
            f"{source_name}:{case_id}: descriptor id must use namespaced contribution grammar"
        )
    if not isinstance(kind, str):
        errors.append(f"{source_name}:{case_id}: descriptor kind must be a string")

    for runtime_key in RUNTIME_ONLY_FIELDS:
        if runtime_key in descriptor:
            runtime_only_fields_stripped.add(runtime_key)

    placements = descriptor.get("placements")
    if not isinstance(placements, list) or not placements:
        errors.append(
            f"{source_name}:{case_id}: descriptor {contribution_id!r} needs placements"
        )
        return
    for placement in placements:
        if not isinstance(placement, dict):
            errors.append(f"{source_name}:{case_id}: placement must be object")
            continue
        host = placement.get("host")
        mode = placement.get("mode")
        if host not in LEGAL_HOSTS:
            legacy_regions.add(str(host))
            errors.append(
                f"{source_name}:{case_id}: unsupported placement host {host!r}"
            )
        if mode not in LEGAL_MODES:
            errors.append(
                f"{source_name}:{case_id}: unsupported placement mode {mode!r}"
            )
        if mode == "tab" and host not in TAB_HOSTS:
            errors.append(
                f"{source_name}:{case_id}: tab placement on unsupported host {host!r}"
            )

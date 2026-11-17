"""Reference resolver for the shared workbench descriptor fixtures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.authoring.workbench.contracts import (
    CANONICAL_SLOTS,
    DATA_REQUIREMENT_KINDS,
    LEGAL_HOSTS,
    LEGAL_MODES,
    TAB_HOSTS,
    _duplicate_values,
)


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

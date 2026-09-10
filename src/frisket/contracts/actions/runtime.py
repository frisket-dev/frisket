"""Explicit extension seams for trusted non-V1 runtime actions."""

from __future__ import annotations

from typing import Any

from frisket.contracts.action import ActionSpec, ActionValidationResult
from frisket.contracts.actions.validation_helpers import _error
from frisket.contracts.plugin_write_plan import (
    PROJECT_READS_CAPABILITY,
    PROJECT_WRITES_CAPABILITY,
)

TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"

# The manifest capabilities plugin.load understands. project_writes/
# project_reads are consumed by the bundled frisket.ftm plugin. Anything
# outside this set stays a loud invalid_plugin_manifest rejection.
KNOWN_MANIFEST_CAPABILITIES = frozenset(
    {
        TRUSTED_LOCAL_BACKEND_CAPABILITY,
        "external:opencorporates",
        PROJECT_WRITES_CAPABILITY,
        PROJECT_READS_CAPABILITY,
    }
)


def runtime_action_binding_exists(kind: str) -> bool:
    from frisket.authoring.plugin_registry import default_registry

    return any(
        spec.kind == kind and spec.handler is not None
        for spec in default_registry().runtime_binding_specs("actions")
    )


def _plugin_manifest_contribution_error(
    action: ActionSpec,
    loaded: Any,
) -> ActionValidationResult | None:
    manifest = loaded.manifest
    required_capabilities = set(manifest.requires.capabilities)
    # Ordinary Actions project their capability requirements from their Python
    # definitions. They are admitted by the same manifest grant checks, not a
    # second plugin-specific roster of native capabilities.
    action_capabilities = {
        capability
        for binding in manifest.runtime.actions
        if binding.handler_api == "typed_action"
        for capability in (binding.catalog_entry or {}).get("required_capabilities", ())
    }
    unknown_capabilities = sorted(
        required_capabilities - KNOWN_MANIFEST_CAPABILITIES - action_capabilities
    )
    if unknown_capabilities:
        return _error(
            "invalid_plugin_manifest",
            "plugin.load does not support manifest capability requirements in this v1 slice",
            action_kind=action.kind,
            field="params.manifest.requires.capabilities",
            details={"capabilities": unknown_capabilities},
        )
    runtime_bindings = (
        manifest.runtime.actions
        or manifest.runtime.importers
        or manifest.runtime.operators
        or manifest.runtime.projections
        or manifest.runtime.job_handlers
    )
    if (
        runtime_bindings
        and TRUSTED_LOCAL_BACKEND_CAPABILITY not in required_capabilities
    ):
        runtime_field = (
            "params.manifest.runtime.job_handlers"
            if (
                manifest.runtime.job_handlers
                and not manifest.runtime.actions
                and not manifest.runtime.importers
                and not manifest.runtime.operators
                and not manifest.runtime.projections
            )
            else "params.manifest.runtime"
        )
        return _error(
            "invalid_plugin_manifest",
            "plugin.load runtime bindings require trusted local backend capability",
            action_kind=action.kind,
            field=runtime_field,
        )

    from frisket.authoring.plugin_registry import default_registry

    registry = default_registry()
    importer_names = {spec.name for spec in registry.importer_specs()}
    column_type_names = {spec.name for spec in registry.column_type_specs()}
    job_handler_names = {spec.kind for spec in registry.job_handler_specs()}
    contributions = manifest.contributes
    from frisket.actions.registry import ACTION_REGISTRY

    invalid_actions = sorted(
        set(contributions.actions) - set(ACTION_REGISTRY.action_ids)
    )
    invalid_importers = sorted(set(contributions.importers) - importer_names)
    invalid_column_types = sorted(set(contributions.column_types) - column_type_names)
    invalid_job_handlers = sorted(set(contributions.job_handlers) - job_handler_names)
    if TRUSTED_LOCAL_BACKEND_CAPABILITY in required_capabilities:
        invalid_actions = []
        invalid_importers = []
        invalid_column_types = []
        invalid_job_handlers = []
    if (
        invalid_actions
        or invalid_importers
        or invalid_column_types
        or invalid_job_handlers
    ):
        return _error(
            "invalid_plugin_manifest",
            "plugin.load manifest declares unavailable contributions",
            action_kind=action.kind,
            field="params.manifest.contributes",
            details={
                "actions": invalid_actions,
                "importers": invalid_importers,
                "column_types": invalid_column_types,
                "job_handlers": invalid_job_handlers,
            },
        )
    return None


def validate_plugin_manifest_contributions(
    action: ActionSpec,
    loaded: Any,
) -> ActionValidationResult | None:
    return _plugin_manifest_contribution_error(action, loaded)

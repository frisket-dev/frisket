"""Resolve admitted installed Python Actions without a global registry mutation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from frisket.actions.core import RegisteredAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, PluginSecrets
from frisket.authoring.plugin_registry import RuntimeBindingSpec
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    _json_string_list,
    _workbench_plugin_install_state,
    project_runtime_binding,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    runtime_binding_dispatch_error,
)
from frisket.authoring.workbench.plugin_runtime_shared import (
    WorkbenchPluginActivationError,
)
from frisket.plugins.action_loader import load_action_module
from frisket.plugins.sdk import Plugin


def resolve_installed_action(
    project: Any, action_id: str
) -> tuple[RegisteredAction, RuntimeBindingSpec] | None:
    binding = project_runtime_binding(project, binding_type="actions", kind=action_id)
    if binding is None or binding.handler_api != "plugin_typed_action_native":
        return None
    install = _workbench_plugin_install_state(project, plugin_id=binding.plugin)
    grants = _json_string_list((install or {}).get("permissions_accepted"))
    error = runtime_binding_dispatch_error(
        binding, capabilities=[*grants, "project:write"]
    )
    if error:
        raise WorkbenchPluginActivationError(
            error["code"],
            error["message"],
            field=error.get("field"),
            details=error.get("details"),
        )
    metadata = binding.metadata
    root = Path(metadata["plugin_root"])
    module = load_action_module(
        root,
        root / metadata["module_path"],
        package_identity=metadata["package_sha256"],
    )
    candidates = {
        id(value): value
        for value in vars(module).values()
        if isinstance(value, Plugin) and value.id == binding.plugin
    }
    if len(candidates) != 1:
        raise ValueError("installed module must declare exactly one matching Plugin")
    plugin = next(iter(candidates.values()))
    action = plugin.action_for(action_id)
    if action is None or action.catalog_entry() != metadata["catalog_entry"]:
        raise ValueError("installed Action differs from its admitted package catalog")
    if binding.handler_key != f"{binding.plugin}:{action.definition.name}":
        raise ValueError("installed Action handler identity does not match")
    return action, binding


def bind_installed_action(
    project: Any, request: ActionRequest
) -> BoundTypedActionRequest | None:
    resolved = resolve_installed_action(project, request.action_id)
    if resolved is None:
        return None
    action, binding = resolved
    if PluginSecrets in getattr(action.definition.run, "injections", ()):
        from frisket.authoring.workbench.native_plugin_secrets import (
            missing_native_plugin_secrets,
        )

        missing = missing_native_plugin_secrets(project, binding)
        if missing:
            raise ValueError(
                "required plugin secrets are not configured: " + ", ".join(missing)
            )
    identity = {
        "plugin_id": binding.plugin,
        "handler_key": binding.handler_key,
        **{
            key: binding.metadata[key]
            for key in (
                "manifest_sha256",
                "package_sha256",
                "module_path",
                "module_sha256",
            )
        },
    }
    return replace(
        BoundTypedActionRequest.bind(action, request),
        implementation_identity=identity,
        runtime_binding=binding,
    )

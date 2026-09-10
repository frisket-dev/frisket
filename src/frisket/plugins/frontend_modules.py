"""Author-time validation for trusted-local frontend component modules.

Shared by ``frisket plugin validate`` and ``plugin.load`` so a broken frontend
module binding fails with the same error code the workbench serve path uses
(`workbench_plugin_frontend_component_module` in
``frisket.workbench.plugin_runtime``), instead of deferring to a 404 at mount
time. The serve path keeps its own checks as defense in depth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.contracts.plugin import LoadedPluginManifest, PluginManifest

MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES = 256_000

FRONTEND_COMPONENT_BINDING_MODULE = "module"
FRONTEND_COMPONENT_BINDING_HOST_COMPONENT = "host-component"


def typed_action_ui_bindings(manifest: PluginManifest) -> dict[str, Any]:
    """Validate action-owned UI exports on the existing component bindings."""
    components = {
        binding.contribution_id: binding
        for binding in manifest.runtime.workbench_components
    }
    result = {}
    for action in manifest.runtime.actions:
        if action.handler_api != "typed_action":
            continue
        form = action.catalog_entry.get("ui_hints", {}).get("form")
        if form == "generated":
            continue
        binding = components.get(action.kind)
        if (
            action.kind not in manifest.contributes.actions
            or binding is None
            or binding.component_key != form
            or binding.module_path is None
        ):
            raise PluginFrontendModuleError(
                "plugin_action_ui_binding_invalid",
                "Typed action form needs its exact action-owned module export binding",
                contribution_id=action.kind,
            )
        result[action.kind] = binding
    return result


class PluginFrontendModuleError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        contribution_id: str | None = None,
        module_path: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.contribution_id = contribution_id
        self.module_path = module_path

    def details(self) -> dict[str, Any]:
        details: dict[str, Any] = {}
        if self.contribution_id is not None:
            details["contribution_id"] = self.contribution_id
        if self.module_path is not None:
            details["module_path"] = self.module_path
        return details


def validate_frontend_component_modules(
    loaded: LoadedPluginManifest, plugin_root: Path
) -> list[dict[str, Any]]:
    """Validate declared frontend modules and classify every binding.

    Returns one ``{"contribution_id", "binding_kind", "module_path"}`` entry per
    ``runtime.workbench_components`` binding. A binding without a
    ``module_path`` is a ``host-component`` binding (the plugin relies on a
    component built into the host, e.g. the demo_selection_summary fixture);
    a binding with one is a ``module`` binding whose file must satisfy the
    serve-path constraints now. Raises :class:`PluginFrontendModuleError` with
    the serve-path error code on the first violation.
    """
    root = Path(plugin_root).resolve()
    typed_action_ui_bindings(loaded.manifest)
    bindings: list[dict[str, Any]] = []
    for binding in loaded.manifest.runtime.workbench_components:
        if binding.module_path is None:
            bindings.append(
                {
                    "contribution_id": binding.contribution_id,
                    "binding_kind": FRONTEND_COMPONENT_BINDING_HOST_COMPONENT,
                    "module_path": None,
                }
            )
            continue
        module_path = (root / binding.module_path).resolve()
        if not module_path.is_relative_to(root):
            raise PluginFrontendModuleError(
                "plugin_frontend_module_path_escape",
                "frontend module path must stay inside the plugin package",
                contribution_id=binding.contribution_id,
                module_path=binding.module_path,
            )
        if module_path.suffix not in {".js", ".mjs"}:
            raise PluginFrontendModuleError(
                "plugin_frontend_module_type_invalid",
                "frontend module path must point to a JavaScript module",
                contribution_id=binding.contribution_id,
                module_path=binding.module_path,
            )
        if not module_path.is_file():
            raise PluginFrontendModuleError(
                "plugin_frontend_module_file_missing",
                "frontend module file is not available",
                contribution_id=binding.contribution_id,
                module_path=binding.module_path,
            )
        if module_path.stat().st_size > MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES:
            raise PluginFrontendModuleError(
                "plugin_frontend_module_too_large",
                "frontend module file exceeds the trusted-local size limit",
                contribution_id=binding.contribution_id,
                module_path=binding.module_path,
            )
        try:
            module_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PluginFrontendModuleError(
                "plugin_frontend_module_encoding_invalid",
                "frontend module file must be UTF-8 text",
                contribution_id=binding.contribution_id,
                module_path=binding.module_path,
            ) from exc
        bindings.append(
            {
                "contribution_id": binding.contribution_id,
                "binding_kind": FRONTEND_COMPONENT_BINDING_MODULE,
                "module_path": binding.module_path,
            }
        )
    return bindings

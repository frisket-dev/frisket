"""Generate plugin.json from a Python Plugin's declarations.

A `Plugin(id=..., version=..., actions=(...))` declaration in plugin.py
carries every field the manifest needs, so for Python-declared plugins the
manifest is a GENERATED artifact: `frisket plugin build` calls this module to
emit plugin.json deterministically, and the drift check
(frisket.plugins.manifest_drift) regenerates and byte-compares instead of
spot-checking handler existence.

Each Action contributes ONE `runtime.actions[]` binding carrying its
canonical catalog entry — the same projection the built-in catalog publishes
— and nothing else. The manifest never restates an Action's inputs, writes,
params, or execution policy: the Action definition is the single authority,
and `PluginManifestRuntimeBinding` rejects a typed binding that tries to
redeclare any of it.

NOT generated here (deliberate scope):
- config.mjs plugins (workbench view/panel/command contributions, e.g.
  frisket.geo) keep the node build path in frisket.plugin_build untouched.
- importer/operator/projection registrations have no generation consumer
  yet; a Plugin that declares manifest meta AND registers one fails loudly
  below, naming the config.mjs path as the supported alternative.

Emission policy (canonical form): `json.dumps(..., indent=2, sort_keys=True)`
plus trailing newline — the same rendering `frisket plugin build`'s
config.mjs path uses — with top-level `settings` omitted when empty. Ordering
inside lists is declaration order, which is source order, so two runs over
the same source are byte-identical.

Generation imports the plugin's backend with the same package-qualified
loader as native dispatch. Plugin Python executes with host authority;
installation is a trusted server-operator action, not a sandbox boundary.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from frisket.contracts.plugin import (
    PLUGIN_MANIFEST_SCHEMA_VERSION,
    PluginManifest,
)
from frisket.plugins.sdk import Plugin


class PluginManifestGenerationError(ValueError):
    """The plugin module cannot serve as a manifest generation source (no
    declared meta, unloadable module, or a registration outside the
    generatable surface)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def manifest_dict_from_plugin(
    plugin: Plugin,
    *,
    module_rel: str = "plugin.py",
) -> dict[str, Any]:
    """Build the full manifest dict from one meta-declaring Plugin.

    Raises PluginManifestGenerationError when the Plugin is not a valid
    generation source; the result is additionally validated against the
    PluginManifest contract before being returned, so a generated manifest
    can never be one the loader would reject.
    """
    if not plugin.declares_manifest:
        raise PluginManifestGenerationError(
            "plugin_manifest_generation_no_meta",
            "manifest generation requires Plugin(id=..., version=...) in the "
            "backend module",
        )
    if plugin.importers or plugin.operators or plugin.projections:
        raise PluginManifestGenerationError(
            "plugin_manifest_generation_unsupported_surface",
            "manifest generation covers Plugin(actions=...) registrations "
            "only; importer/operator/projection contributions need the "
            "plugin.config.mjs build path",
        )
    if not plugin.actions:
        raise PluginManifestGenerationError(
            "plugin_manifest_generation_no_actions",
            "manifest generation requires at least one Action in Plugin(actions=...)",
        )

    plugin_id = plugin.id
    assert plugin_id is not None
    # Action ids are namespaced by registration (frisket.plugins.sdk's
    # register_actions prefixes the validated plugin id), so the catalog
    # entry below is the only per-Action authority the manifest carries.
    catalog_entries = [registered.catalog_entry() for registered in plugin.actions]
    actions: list[dict[str, Any]] = [
        {
            "kind": registered.action_id,
            "handler_key": f"{plugin_id}:{registered.definition.name}",
            "handler_api": "typed_action",
            "module_path": module_rel,
            "catalog_entry": entry,
        }
        for registered, entry in zip(plugin.actions, catalog_entries, strict=True)
    ]

    manifest: dict[str, Any] = {
        "schema_version": PLUGIN_MANIFEST_SCHEMA_VERSION,
        "id": plugin_id,
        "version": plugin.version,
        "auto_enable": plugin.auto_enable,
        "contributes": {
            "actions": [item["kind"] for item in actions],
            "column_types": [],
            "importers": [],
            "job_handlers": [],
            "operators": [],
            "projections": [],
            "workbench_commands": [],
            "workbench_panels": [],
            "workbench_views": [],
        },
        "requires": {
            "capabilities": list(
                dict.fromkeys(
                    [
                        *plugin.capabilities,
                        *(
                            cap
                            for entry in catalog_entries
                            for cap in entry["required_capabilities"]
                            if cap != "project:write"
                        ),
                    ]
                )
            ),
            "secrets": list(
                dict.fromkeys(
                    [
                        *plugin.secrets,
                        *(
                            secret
                            for entry in catalog_entries
                            for secret in entry["required_credentials"]
                        ),
                    ]
                )
            ),
        },
        "runtime": {
            "actions": actions,
            "importers": [],
            "job_handlers": [],
            "operators": [],
            "projections": [],
        },
    }
    if plugin.settings:
        manifest["settings"] = [dict(item) for item in plugin.settings]
    components = [
        {
            "contribution_id": registered.action_id,
            "module_key": f"{plugin_id}.frontend",
            "component_key": registered.definition.form,
            "module_path": "frontend/plugin.js",
        }
        for registered in plugin.actions
        if registered.definition.form != "generated"
    ]
    if components:
        manifest["runtime"]["workbench_components"] = components

    try:
        PluginManifest.model_validate(manifest)
    except Exception as exc:
        raise PluginManifestGenerationError(
            "plugin_manifest_generation_invalid",
            f"generated manifest does not satisfy frisket.plugin.v1: {exc}",
        ) from exc
    return manifest


def render_manifest_json(manifest: dict[str, Any]) -> str:
    """Canonical byte-stable rendering — matches the config.mjs build path's
    emission exactly (json.dumps indent=2 sort_keys + newline)."""
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def render_plugin_action_types(plugin: Plugin) -> str:
    """Plugin-local Params cache; the shared JSON Schema renderer owns syntax."""
    from frisket.contracts.http.typescript import render_typescript_declaration

    declarations = []
    mappings = []
    for index, action in enumerate(plugin.actions):
        name = f"ActionParams{index}"
        declarations.append(
            render_typescript_declaration(
                action.definition.run.params_model.model_json_schema(), type_name=name
            )
        )
        mappings.append(f"  {json.dumps(action.action_id)}: {name};")
    return (
        "// Generated by frisket plugin build. Do not edit.\n\n"
        + "export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };\n\n"
        + "\n\n".join(declarations)
        + "\n\nexport interface GeneratedActionParams {\n"
        + "\n".join(mappings)
        + "\n}\n"
    )


def plugin_source_identity(plugin_root: Path) -> str:
    """Digest of every Python source file in the package.

    `load_action_module` gives each admitted package version ordinary
    sys.modules lifetime, keyed by root plus package identity — so within ONE
    process, re-importing the same root returns the module it first imported.
    Real dispatch wants that. Author tooling does not: `frisket plugin dev`
    watches the source and rebuilds in a long-lived process, so without a
    content-derived identity it would regenerate the manifest from the code
    the author has already edited away. Hashing the whole tree, not just
    plugin.py, because a backend module may import siblings (frisket.ftm's
    Actions live in ftm_actions.py).
    """
    digest = hashlib.sha256()
    for path in sorted(plugin_root.rglob("*.py")):
        if path.is_file() and not path.is_symlink():
            digest.update(str(path.relative_to(plugin_root)).encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def load_generation_source(plugin_root: Path) -> Plugin | None:
    """Import <plugin_root>/plugin.py with the loader real dispatch uses and
    return its meta-declaring Plugin, or None when the package has no
    plugin.py or the module declares no Plugin(id=..., version=...).

    Import failures propagate — a package that opts into generation must
    have an importable backend module, and callers (build/validate) surface
    the exception as their own error.
    """
    module_path = plugin_root / "plugin.py"
    if not module_path.is_file():
        return None
    # Same lazy import + bytecode-cache suppression as the drift check: the
    # exact loading machinery real dispatch uses, and no __pycache__ left
    # behind in a package that should stay exactly the files authored/built.
    from frisket.plugins.action_loader import load_action_module

    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        module = load_action_module(
            plugin_root.resolve(),
            module_path.resolve(),
            package_identity=plugin_source_identity(plugin_root),
        )
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    for value in vars(module).values():
        if isinstance(value, Plugin) and value.declares_manifest:
            return value
    return None


def generate_manifest_text(plugin_root: Path) -> str | None:
    """End-to-end: load plugin.py, generate, render. None when the package
    is not a generation source (no plugin.py / no declared meta)."""
    plugin = load_generation_source(plugin_root)
    if plugin is None:
        return None
    return render_manifest_json(manifest_dict_from_plugin(plugin))

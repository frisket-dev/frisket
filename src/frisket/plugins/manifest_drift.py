from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from frisket.contracts.plugin import LoadedPluginManifest, PluginManifestRuntimeBinding


class PluginManifestDriftError(ValueError):
    """A manifest `runtime.actions[]` binding names an Action that the
    plugin's Python backend does not actually declare, or declares with a
    different catalog entry (or the backend module itself does not import
    cleanly)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def check_action_handlers_against_backend(
    loaded: LoadedPluginManifest,
    plugin_root: Path,
) -> None:
    """Raise `PluginManifestDriftError` on manifest/backend drift.

    Two regimes:
    - Generated manifests (plugin.py declares `Plugin(id=..., version=...)`,
      so plugin.json is generated output): regenerate and BYTE-compare — any
      divergence in any field is drift.
    - Everything else (config.mjs-built plugins, hand-written fixtures):
      resolve each typed `runtime.actions[]` binding against the Action its
      backend module actually registers, and compare the binding's declared
      catalog entry with the live one. That is the same identity check
      native dispatch makes when it admits an installed Action
      (`workbench/installed_actions.resolve_installed_action`), so a package
      that passes validate is a package dispatch will accept. No-op for
      plugins with no actions.
    """
    if _check_generated_manifest_bytes(loaded, plugin_root):
        return
    actions = loaded.manifest.runtime.actions
    if not actions:
        return

    module_cache: dict[Path, Any] = {}
    # Validate is a read-only inspection of the package — it must not leave
    # `__pycache__/*.pyc` bytecode-cache files behind inside the plugin
    # package as a side effect of this check (a package that only ever went
    # through `frisket plugin build`/`validate` should stay exactly the
    # files the build emitted).
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        for binding in actions:
            _check_one_action_binding(
                plugin_id=loaded.manifest.id,
                plugin_root=plugin_root,
                binding=binding,
                module_cache=module_cache,
            )
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode


def _check_generated_manifest_bytes(
    loaded: LoadedPluginManifest,
    plugin_root: Path,
) -> bool:
    """Byte-equality drift check for generated manifests.

    Returns True when the check ran (the manifest is generated output and
    matched its regeneration byte-for-byte), False to fall through to the
    per-binding catalog check — including when plugin.py is missing, fails to
    import, declares no manifest meta, declares a different plugin id, or
    registers surfaces generation does not cover.
    """
    from frisket.plugins.manifest_generate import (
        PluginManifestGenerationError,
        load_generation_source,
        manifest_dict_from_plugin,
        render_manifest_json,
    )

    manifest_path = plugin_root / "plugin.json"
    if not (plugin_root / "plugin.py").is_file() or not manifest_path.is_file():
        return False
    try:
        plugin = load_generation_source(plugin_root)
    except Exception:  # noqa: BLE001 - the per-binding path surfaces import failures
        return False
    if plugin is None or plugin.id != loaded.manifest.id:
        return False
    try:
        expected = render_manifest_json(manifest_dict_from_plugin(plugin))
    except PluginManifestGenerationError:
        return False
    if manifest_path.read_bytes() != expected.encode("utf-8"):
        raise PluginManifestDriftError(
            "plugin_manifest_generated_drift",
            "plugin.json is not the byte-exact regeneration of plugin.py's "
            "Plugin(...) declaration — the manifest is generated output; run "
            "`frisket plugin build` on the package instead of editing "
            "plugin.json",
        )
    return True


def _check_one_action_binding(
    *,
    plugin_id: str,
    plugin_root: Path,
    binding: PluginManifestRuntimeBinding,
    module_cache: dict[Path, Any],
) -> None:
    # Imported here (not at module scope) because these are the exact
    # loading/lookup machinery real dispatch uses, and importing the action
    # catalog eagerly would make every manifest load pay for it.
    from frisket.plugins.action_loader import load_action_module
    from frisket.plugins.manifest_generate import plugin_source_identity
    from frisket.plugins.sdk import Plugin

    if binding.handler_api != "typed_action":
        # Drift-checking is scoped to Plugin(actions=...) declarations;
        # importers/operators/projections have their own decorator families
        # and are out of scope here.
        return

    module_rel = binding.module_path or "plugin.py"
    module_path = (plugin_root / module_rel).resolve()
    if not module_path.is_relative_to(plugin_root.resolve()):
        raise PluginManifestDriftError(
            "plugin_manifest_action_handler_module_invalid",
            f"runtime action {binding.kind!r} module_path {module_rel!r} "
            "escapes the plugin package",
        )
    if not module_path.is_file():
        raise PluginManifestDriftError(
            "plugin_manifest_action_handler_module_missing",
            f"runtime action {binding.kind!r} declares module_path "
            f"{module_rel!r}, which does not exist in the built package",
        )

    module = module_cache.get(module_path)
    if module is None:
        try:
            module = load_action_module(
                plugin_root.resolve(),
                module_path,
                package_identity=plugin_source_identity(plugin_root),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a validate error
            raise PluginManifestDriftError(
                "plugin_manifest_action_handler_module_import_failed",
                f"runtime action {binding.kind!r} module {module_rel!r} "
                f"failed to import: {exc}",
            ) from exc
        module_cache[module_path] = module

    registered = None
    for value in vars(module).values():
        if isinstance(value, Plugin):
            registered = value.action_for(binding.kind)
            if registered is not None:
                break

    if registered is None:
        raise PluginManifestDriftError(
            "plugin_manifest_action_handler_missing",
            f"runtime action {binding.kind!r} is not registered by any "
            f"Plugin(actions=...) declaration in {module_rel!r} — the "
            "manifest and the backend have drifted apart",
        )
    if binding.catalog_entry != registered.catalog_entry():
        raise PluginManifestDriftError(
            "plugin_manifest_action_catalog_drift",
            f"runtime action {binding.kind!r} declares a catalog entry that "
            f"differs from the Action registered in {module_rel!r} — run "
            "`frisket plugin build` on the package instead of editing "
            "plugin.json",
        )
    # Same handler identity native admission requires before it will bind an
    # installed Action (workbench/installed_actions.resolve_installed_action),
    # so validate cannot pass a package dispatch would then refuse.
    expected_handler_key = f"{plugin_id}:{registered.definition.name}"
    if binding.handler_key != expected_handler_key:
        raise PluginManifestDriftError(
            "plugin_manifest_action_handler_identity_mismatch",
            f"runtime action {binding.kind!r} declares handler_key "
            f"{binding.handler_key!r}, but its registered Action binds as "
            f"{expected_handler_key!r}",
        )

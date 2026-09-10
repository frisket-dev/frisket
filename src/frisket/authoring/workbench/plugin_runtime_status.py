"""Workbench plugin runtime read projection and status helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from frisket.authoring.plugin_registry import RuntimeBindingSpec, default_registry
from frisket.authoring.workbench.plugin_package_catalog import (
    plugin_package_catalog_for_project,
)
from frisket.authoring.workbench.contracts import (
    FRONTEND_COMPONENT_BINDING_SCHEMA_VERSION,
    PLUGIN_INSTALL_STATE_SCHEMA_VERSION,
    RUNTIME_INDEX_SCHEMA_VERSION,
    RUNTIME_PLUGIN_SCHEMA_VERSION,
    WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION,
    load_first_party_workbench_descriptor_package,
)
from frisket.authoring.workbench.plugin_runtime_shared import (
    BUNDLED_PLUGIN_VALUE_RE,
    MAX_PLUGIN_LOAD_RECEIPTS_TO_SCAN,
    PLUGIN_INSTALL_PLAN_EXECUTION_SCHEMA_VERSION,
    PluginCompositionPolicy,
    WorkbenchPluginActivationError,
    WorkbenchPluginLifecycleError,
    active_plugin_composition_policy,
)
from frisket.contracts.actions.runtime import TRUSTED_LOCAL_BACKEND_CAPABILITY
from frisket.contracts.plugin import (
    LoadedPluginManifest,
    PluginManifest,
    PluginManifestLoadError,
    PluginManifestWorkbenchComponentBinding,
    load_plugin_manifest_file,
)
from frisket.engine.store import Project
from frisket.plugins.frontend_modules import MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES
from frisket.plugins.package_identity import (
    PluginPackageIdentityError,
    build_plugin_package_identity,
    live_package_file_identity,
    live_plugin_package_identity_from_ref,
    package_file_identity_from_ref,
)


def _first_party_workbench_runtime_section() -> dict[str, Any]:
    """Project the checked-in first-party descriptor package into the index.

    The descriptors are served as an honest ``firstParty`` section
    — NOT fake plugin entries. First-party contributions have no
    installState/receipts/moduleUrl, so this section carries only what the
    checked-in artifact carries (descriptor-metadata-honesty precedent).
    ``descriptor_manifests`` is byte-for-byte the artifact's ``descriptors``
    list here because the artifact contains no runtime-only fields to strip
    (tests/test_first_party_descriptor_package.py pins served == file).
    """
    loaded = load_first_party_workbench_descriptor_package()
    return {
        "schemaVersion": loaded.schema_version,
        "descriptors": loaded.descriptor_manifests,
    }


def _shipped_bundled_package_identity(value: str) -> tuple[str, str] | None:
    """Hash a shipped bundle exactly as ``plugin.load`` does."""
    if not BUNDLED_PLUGIN_VALUE_RE.fullmatch(value):
        return None
    root = _bundled_plugins_root().resolve()
    package_root = (root / value).resolve()
    if package_root != root and root not in package_root.parents:
        return None
    manifest_path = package_root / "plugin.json"
    if not manifest_path.is_file():
        return None
    try:
        loaded = load_plugin_manifest_file(manifest_path)
    except PluginManifestLoadError:
        return None
    descriptor_path = manifest_path.parent / "workbench-descriptors.json"
    try:
        identity = build_plugin_package_identity(
            loaded,
            manifest_path,
            descriptor_path=descriptor_path if descriptor_path.is_file() else None,
        )
    except PluginPackageIdentityError:
        return None
    return loaded.sha256, identity.package_sha256


def catalog_entry_is_reviewed_bundled(entry: dict[str, Any] | None) -> bool:
    """Require bundled provenance and exact current shipped bytes."""
    if not isinstance(entry, dict):
        return False
    source = _json_object(entry.get("install_source"))
    if source.get("kind") != "bundled":
        return False
    value = source.get("value")
    if not isinstance(value, str):
        return False
    shipped = _shipped_bundled_package_identity(value)
    if shipped is None:
        return False
    shipped_manifest_sha, shipped_package_sha = shipped
    recorded_manifest_sha = str(entry.get("manifest_sha256") or "")
    recorded_package_sha = str(entry.get("package_sha256") or "")
    return (
        recorded_manifest_sha == shipped_manifest_sha
        and recorded_package_sha == shipped_package_sha
    )


_CATALOG_ENTRY_UNSET = object()


def composition_policy_permits_catalog_entry(
    plugin_id: str,
    entry: dict[str, Any] | None,
    *,
    policy: PluginCompositionPolicy | None = None,
) -> bool:
    """Apply the process policy to workspace-owned package evidence."""
    effective_policy = policy or active_plugin_composition_policy()
    return effective_policy.permits(
        plugin_id,
        is_reviewed_bundled=catalog_entry_is_reviewed_bundled(entry),
    )


def workspace_permits_plugin(
    project: Project,
    plugin_id: str,
    *,
    catalog_entry: dict[str, Any] | None | object = _CATALOG_ENTRY_UNSET,
    policy: PluginCompositionPolicy | None = None,
) -> bool:
    """Apply the process policy to the workspace catalog's package evidence."""
    if catalog_entry is _CATALOG_ENTRY_UNSET:
        catalog_entry = plugin_package_catalog_for_project(project).get(plugin_id)
    resolved_entry = catalog_entry if isinstance(catalog_entry, dict) else None
    return composition_policy_permits_catalog_entry(
        plugin_id, resolved_entry, policy=policy
    )


def project_accepts_current_plugin_capabilities(
    install_state: dict[str, Any] | None,
    manifest: PluginManifest,
) -> bool:
    """Require this project to have accepted the current package manifest."""
    if not isinstance(install_state, dict):
        return False
    accepted = set(_json_string_list(install_state.get("permissions_accepted")))
    required = set(_effective_required_capabilities(manifest))
    return required.issubset(accepted)


def project_accepts_current_plugin_capabilities_from_ref(
    install_state: dict[str, Any] | None,
    manifest_ref: dict[str, Any],
) -> bool:
    """Capability gate for fail-soft index projection of durable evidence."""
    if not isinstance(install_state, dict):
        return False
    accepted = set(_json_string_list(install_state.get("permissions_accepted")))
    required = set(
        _effective_required_capabilities_from_ref(
            _dict_or_empty(manifest_ref.get("requires")),
            _dict_or_empty(manifest_ref.get("runtime")),
        )
    )
    return required.issubset(accepted)


def workbench_plugin_runtime_index(
    project: Project, *, project_id: str
) -> dict[str, Any]:
    """Project the workspace package catalog + this project's enablement rows.

    Single authority: package IDENTITY (manifest evidence, digests,
    validated source) comes from the workspace-owned catalog, keyed by
    ``plugin_id``; ENABLEMENT (enabled/disabled/failed + reason) comes from this
    project's ``workbench_plugin_installs`` row. The registry is consulted ONLY
    to annotate whether the catalog's manifest is live-registered, never for
    discovery and never to execute code.
    """
    registered_manifests = {
        loaded.manifest.id: loaded for loaded in default_registry().plugin_manifests()
    }
    catalog = plugin_package_catalog_for_project(project)
    catalog_entries = catalog.all()
    install_states = _workbench_plugin_install_states(project)
    plugins: dict[str, dict[str, Any]] = {}
    for plugin_id in sorted(catalog_entries):
        entry = catalog_entries[plugin_id]
        install_state = install_states.get(plugin_id)
        manifest_ref = catalog.manifest_ref(plugin_id)
        install_source = _json_object(entry.get("install_source"))
        if not workspace_permits_plugin(project, plugin_id, catalog_entry=entry):
            continue
        if manifest_ref is None:
            plugins[plugin_id] = _runtime_failed_plugin_from_catalog(
                project_id=project_id,
                plugin_id=plugin_id,
                catalog_entry=entry,
                install_state=install_state,
            )
            continue
        registered = registered_manifests.get(plugin_id)
        current_capabilities_accepted = (
            project_accepts_current_plugin_capabilities_from_ref(
                install_state, manifest_ref
            )
        )
        activated = (
            install_state is not None
            and install_state.get("install_state") == "enabled"
            and current_capabilities_accepted
            and registered is not None
            and registered.sha256 == str(manifest_ref.get("manifest_sha256") or "")
        )
        projected_install_state = install_state
        if (
            install_state is not None
            and install_state.get("install_state") == "enabled"
            and not current_capabilities_accepted
        ):
            projected_install_state = dict(install_state)
            projected_install_state["install_state"] = "installed"
            projected_install_state["activation"] = "manifestLoaded"
        plugins[plugin_id] = _runtime_plugin_from_manifest_ref(
            project_id=project_id,
            receipt_id=str(entry.get("receipt_id") or ""),
            manifest_ref=manifest_ref,
            install_state=projected_install_state,
            install_source=install_source,
            activated=activated,
        )
    # A project row marked failed for a plugin with no catalog identity still
    # surfaces (e.g. a failed install whose catalog failure row was pruned).
    for plugin_id, install_state in install_states.items():
        if plugin_id in plugins:
            continue
        if install_state.get("install_state") == "failed":
            if not workspace_permits_plugin(
                project,
                plugin_id,
                catalog_entry=catalog_entries.get(plugin_id),
            ):
                continue
            plugins[plugin_id] = _runtime_failed_plugin_from_catalog(
                project_id=project_id,
                plugin_id=plugin_id,
                catalog_entry=catalog_entries.get(plugin_id) or {},
                install_state=install_state,
            )
    ordered_plugins = [plugins[plugin_id] for plugin_id in sorted(plugins)]
    return {
        "schemaVersion": RUNTIME_INDEX_SCHEMA_VERSION,
        "projectId": project_id,
        "arbitraryPackageLoadAllowed": False,
        "receiptScanLimit": MAX_PLUGIN_LOAD_RECEIPTS_TO_SCAN,
        "skippedInvalidReceipts": 0,
        "skippedInvalidManifestRefs": 0,
        "loadedPluginCount": len(ordered_plugins),
        "plugins": ordered_plugins,
        "firstParty": _first_party_workbench_runtime_section(),
    }


def workbench_plugin_frontend_component_module(
    project: Project,
    *,
    plugin_id: str,
    contribution_id: str,
    expected_package_sha256: str | None = None,
) -> str:
    install_state = _workbench_plugin_install_state(project, plugin_id=plugin_id)
    if install_state is None or install_state.get("install_state") != "enabled":
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_not_enabled",
            "frontend module serving requires an enabled workbench plugin",
            status_code=409,
        )
    if _safe_bool(install_state.get("arbitrary_package_load_allowed")):
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_arbitrary_package_load_blocked",
            "frontend module serving does not allow arbitrary package loading",
            status_code=403,
        )
    catalog = plugin_package_catalog_for_project(project)
    entry = catalog.get(plugin_id)
    manifest_ref = catalog.manifest_ref(plugin_id)
    if entry is None or manifest_ref is None:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_manifest_mismatch",
            "workspace catalog has no package identity for this plugin",
            status_code=409,
        )
    if not workspace_permits_plugin(project, plugin_id, catalog_entry=entry):
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_not_available",
            "frontend module serving is not available for this plugin under "
            "the active composition policy",
            status_code=404,
        )
    receipt_id, manifest_sha256, package_sha256 = _catalog_identity_refs(entry)
    if manifest_ref.get("plugin_id") != plugin_id:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_manifest_mismatch",
            "workspace catalog package does not match the requested plugin id",
            status_code=409,
        )
    recorded_package_sha256 = str(manifest_ref.get("package_sha256") or "")
    if package_sha256 != recorded_package_sha256:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_package_mismatch",
            "enabled plugin install state does not match plugin.load package sha",
            status_code=409,
            details={
                "expected_package_sha256": package_sha256,
                "recorded_package_sha256": recorded_package_sha256,
            },
        )
    if not isinstance(expected_package_sha256, str) or not expected_package_sha256:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_package_required",
            "frontend module serving requires the package digest from the runtime index URL",
            status_code=409,
            details={"plugin_id": plugin_id, "contribution_id": contribution_id},
        )
    if expected_package_sha256 != recorded_package_sha256:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_package_mismatch",
            "frontend module URL package digest does not match the enabled plugin",
            status_code=409,
            details={
                "expected_package_sha256": recorded_package_sha256,
                "requested_package_sha256": expected_package_sha256,
            },
        )
    loaded = _loaded_manifest_from_ref(manifest_ref)
    if not project_accepts_current_plugin_capabilities(install_state, loaded.manifest):
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_permissions_missing",
            "frontend module serving requires acceptance of the current manifest capabilities",
            status_code=409,
        )
    if loaded.sha256 != manifest_sha256:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_manifest_mismatch",
            "enabled plugin install state does not match plugin.load manifest sha",
            status_code=409,
        )
    from frisket.plugins.frontend_modules import typed_action_ui_bindings

    action_ui = typed_action_ui_bindings(loaded.manifest)
    if contribution_id in action_ui:
        from frisket.authoring.workbench.plugin_runtime_capabilities import (
            project_runtime_binding,
        )

        action_binding = project_runtime_binding(
            project, binding_type="actions", kind=contribution_id
        )
        if (
            action_binding is None
            or action_binding.plugin != plugin_id
            or action_binding.handler_api != "plugin_typed_action_native"
        ):
            raise WorkbenchPluginActivationError(
                "plugin_action_ui_not_available",
                "Action editor requires its enabled typed action runtime binding",
                status_code=404,
            )
    declared_ids = {
        *loaded.manifest.contributes.workbench_views,
        *loaded.manifest.contributes.workbench_panels,
        *loaded.manifest.contributes.workbench_commands,
        *action_ui,
    }
    if contribution_id not in declared_ids:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_contribution_not_declared",
            "frontend module contribution is not declared by the plugin manifest",
            status_code=404,
        )
    binding = next(
        (
            item
            for item in loaded.manifest.runtime.workbench_components
            if item.contribution_id == contribution_id
        ),
        None,
    )
    if binding is None or binding.module_path is None:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_missing",
            "frontend module path is not declared for this contribution",
            status_code=404,
        )
    plugin_root = _manifest_ref_plugin_root(manifest_ref)
    module_path = (plugin_root / binding.module_path).resolve()
    if not module_path.is_relative_to(plugin_root):
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_path_escape",
            "frontend module path must stay inside the plugin package",
            status_code=400,
        )
    if module_path.suffix not in {".js", ".mjs"}:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_type_invalid",
            "frontend module path must point to a JavaScript module",
            status_code=400,
        )
    if not module_path.is_file():
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_file_missing",
            "frontend module file is not available",
            status_code=404,
        )
    _assert_manifest_ref_module_integrity(
        manifest_ref,
        plugin_root=plugin_root,
        module_path=binding.module_path,
    )
    if module_path.stat().st_size > MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_too_large",
            "frontend module file exceeds the trusted-local size limit",
            status_code=413,
        )
    try:
        return module_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbenchPluginActivationError(
            "plugin_frontend_module_encoding_invalid",
            "frontend module file must be UTF-8 text",
            status_code=400,
        ) from exc


def _loaded_manifest_from_ref(manifest_ref: dict[str, Any]) -> LoadedPluginManifest:
    manifest_payload = {
        "schema_version": manifest_ref.get("schema_version"),
        "id": manifest_ref.get("plugin_id"),
        "version": manifest_ref.get("version"),
        "contributes": manifest_ref.get("contributes"),
        "requires": manifest_ref.get("requires") or {},
        "runtime": manifest_ref.get("runtime") or {},
        "settings": manifest_ref.get("settings") or [],
        # Preserve an opted-out manifest's auto_enable:false through the
        # receipt roundtrip. Legacy evidence that predates the field has no
        # key -> default True (auto-enabled, unchanged).
        "auto_enable": manifest_ref.get("auto_enable", True),
    }
    try:
        manifest = PluginManifest.model_validate(manifest_payload)
    except Exception as exc:
        raise WorkbenchPluginActivationError(
            "plugin_activation_manifest_invalid",
            "plugin.load receipt manifest evidence is not a valid frisket.plugin.v1 manifest",
            status_code=400,
        ) from exc
    sha256 = str(manifest_ref.get("manifest_sha256") or "")
    if not sha256:
        raise WorkbenchPluginActivationError(
            "plugin_activation_manifest_sha_missing",
            "plugin.load receipt evidence is missing manifest sha256",
            status_code=400,
        )
    return LoadedPluginManifest(
        manifest=manifest,
        sha256=sha256,
        byte_count=_safe_int(manifest_ref.get("byte_count")),
    )


def _manifest_ref_plugin_root(manifest_ref: dict[str, Any]) -> Path:
    source_path = manifest_ref.get("path")
    if not isinstance(source_path, str) or not source_path:
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin.load receipt evidence is missing a local source path",
            status_code=409,
        )
    manifest_path = Path(source_path).resolve()
    if not manifest_path.is_file():
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin manifest source file is no longer available",
            status_code=409,
        )
    return manifest_path.parent


def _assert_manifest_ref_module_integrity(
    manifest_ref: dict[str, Any],
    *,
    plugin_root: Path,
    module_path: str,
) -> dict[str, Any]:
    package_ref = manifest_ref.get("package_identity")
    expected = package_file_identity_from_ref(
        package_ref if isinstance(package_ref, dict) else None,
        module_path,
    )
    if expected is None:
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin.load receipt evidence is missing package module identity",
            status_code=409,
            details={"module_path": module_path},
        )
    try:
        current = live_package_file_identity(plugin_root, module_path)
    except PluginPackageIdentityError as exc:
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin package module is no longer available",
            status_code=409,
            details={"module_path": exc.path or module_path},
        ) from exc
    if current.sha256 != expected.sha256 or current.byte_count != expected.byte_count:
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin source changed after plugin.load; reload the plugin before execution",
            status_code=409,
            details={
                "module_path": module_path,
                "expected_sha256": expected.sha256,
                "current_sha256": current.sha256,
            },
        )
    _assert_live_package_identity(
        package_ref if isinstance(package_ref, dict) else None,
        plugin_root=plugin_root,
        expected_package_sha256=str(manifest_ref.get("package_sha256") or ""),
    )
    return expected.to_ref()


def _assert_live_package_identity(
    package_ref: dict[str, Any] | None,
    *,
    plugin_root: Path,
    expected_package_sha256: str,
) -> None:
    if not expected_package_sha256.startswith("sha256:"):
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin.load receipt evidence is missing package identity",
            status_code=409,
        )
    try:
        current_package = live_plugin_package_identity_from_ref(
            plugin_root, package_ref
        )
    except PluginPackageIdentityError as exc:
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin package identity could not be computed",
            status_code=409,
            details={"module_path": exc.path} if exc.path else {},
        ) from exc
    if current_package.package_sha256 != expected_package_sha256:
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin package source changed after plugin.load; reload the plugin before execution",
            status_code=409,
            details={
                "expected_package_sha256": expected_package_sha256,
                "current_package_sha256": current_package.package_sha256,
            },
        )


def _assert_runtime_binding_integrity(binding: RuntimeBindingSpec) -> None:
    metadata = dict(getattr(binding, "metadata", {}) or {})
    module_path = metadata.get("module_path")
    plugin_root = metadata.get("plugin_root")
    package_ref = metadata.get("package_identity")
    package_sha256 = metadata.get("package_sha256")
    module_sha256 = metadata.get("module_sha256")
    module_byte_count = metadata.get("module_byte_count")
    identity_values = (
        module_path,
        plugin_root,
        package_sha256,
        module_sha256,
        module_byte_count,
    )
    if not any(value not in (None, "") for value in identity_values):
        if str(getattr(binding, "handler_api", "")).endswith("_subprocess") or (
            binding.handler_api == "plugin_typed_action_native"
        ):
            raise WorkbenchPluginActivationError(
                "plugin_code_integrity_mismatch",
                "plugin runtime binding is missing package module identity",
                status_code=409,
                details={"binding_kind": binding.kind},
            )
        return
    if (
        not isinstance(module_path, str)
        or not module_path
        or not isinstance(plugin_root, str)
        or not plugin_root
        or not isinstance(package_sha256, str)
        or not package_sha256.startswith("sha256:")
        or not isinstance(module_sha256, str)
        or not module_sha256
        or isinstance(module_byte_count, bool)
        or not isinstance(module_byte_count, int)
    ):
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin runtime binding is missing package module identity",
            status_code=409,
            details={"binding_kind": binding.kind},
        )
    try:
        current = live_package_file_identity(Path(plugin_root), module_path)
    except PluginPackageIdentityError as exc:
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin package module is no longer available",
            status_code=409,
            details={"module_path": exc.path or module_path},
        ) from exc
    if current.sha256 != module_sha256 or current.byte_count != module_byte_count:
        raise WorkbenchPluginActivationError(
            "plugin_code_integrity_mismatch",
            "plugin source changed after plugin.load; reload the plugin before execution",
            status_code=409,
            details={
                "module_path": module_path,
                "expected_sha256": module_sha256,
                "current_sha256": current.sha256,
            },
        )
    _assert_live_package_identity(
        package_ref if isinstance(package_ref, dict) else None,
        plugin_root=Path(plugin_root),
        expected_package_sha256=package_sha256,
    )


def runtime_binding_dispatch_error(
    binding: RuntimeBindingSpec,
    *,
    capabilities: list[str] | tuple[str, ...],
) -> dict[str, Any] | None:
    metadata = dict(getattr(binding, "metadata", {}) or {})
    required = _string_list(metadata.get("required_capabilities"))
    if (
        str(getattr(binding, "handler_api", "")).startswith("plugin_")
        and str(getattr(binding, "handler_api", "")).endswith("_subprocess")
        or binding.handler_api == "plugin_typed_action_native"
    ) and TRUSTED_LOCAL_BACKEND_CAPABILITY not in required:
        required = [*required, TRUSTED_LOCAL_BACKEND_CAPABILITY]
    if (
        getattr(binding, "binding_type", None) == "actions"
        and "project:write" not in required
    ):
        required = ["project:write", *required]
    granted = {str(item) for item in capabilities}
    missing = [item for item in required if item not in granted]
    if missing:
        return {
            "code": "plugin_capability_required",
            "message": "Plugin action requires manifest capabilities that were not granted.",
            "field": "capabilities",
            "details": {
                "plugin_id": binding.plugin,
                "action_kind": binding.kind,
                "missing": missing,
            },
        }
    try:
        _assert_runtime_binding_integrity(binding)
    except WorkbenchPluginActivationError as exc:
        return {
            "code": exc.code,
            "message": exc.message,
            "field": None,
            "details": exc.details,
        }
    return None


def _runtime_plugin_from_manifest_ref(
    *,
    project_id: str,
    receipt_id: str,
    manifest_ref: dict[str, Any],
    install_state: dict[str, Any] | None = None,
    install_source: dict[str, Any] | None = None,
    activated: bool = False,
) -> dict[str, Any]:
    contributes = _dict_or_empty(manifest_ref.get("contributes"))
    requires = _dict_or_empty(manifest_ref.get("requires"))
    runtime = _dict_or_empty(manifest_ref.get("runtime"))
    path = manifest_ref.get("path")
    byte_count = _safe_int(manifest_ref.get("byte_count"))
    persisted_install_state = (
        str(install_state.get("install_state") or "") if install_state else ""
    )
    persisted_activation = (
        str(install_state.get("activation") or "") if install_state else ""
    )
    disabled_reason = (
        str(install_state.get("disabled_reason") or "") if install_state else ""
    )
    install_source = dict(install_source or {})
    descriptor_package = _workbench_descriptor_package(manifest_ref)
    if persisted_install_state == "disabled":
        public_install_state = "disabled"
        public_activation = persisted_activation or "blocked"
        public_registry_activated = False
    elif persisted_install_state == "uninstalled":
        public_install_state = "uninstalled"
        public_activation = persisted_activation or "removed"
        public_registry_activated = False
    elif persisted_install_state == "enabled":
        public_install_state = "enabled"
        public_activation = persisted_activation or "registryManifestRegistered"
        public_registry_activated = activated
    else:
        public_install_state = "enabled" if activated else "installed"
        public_activation = (
            "registryManifestRegistered" if activated else "manifestLoaded"
        )
        public_registry_activated = activated
    plugin: dict[str, Any] = {
        "schemaVersion": RUNTIME_PLUGIN_SCHEMA_VERSION,
        "pluginId": str(manifest_ref.get("plugin_id") or ""),
        "version": str(manifest_ref.get("version") or ""),
        "installState": public_install_state,
        "activation": public_activation,
        "runtimeSource": "plugin.load_receipt",
        "receiptId": receipt_id,
        "manifestSha256": str(manifest_ref.get("manifest_sha256") or ""),
        "packageSha256": str(manifest_ref.get("package_sha256") or ""),
        "byteCount": byte_count,
        "source": install_source
        or {
            "kind": "local_file",
            "path": str(path) if isinstance(path, str) else "",
        },
        "contributionSummary": _contribution_summary(contributes),
        "frontendComponentBindings": _frontend_component_bindings(
            project_id=project_id,
            plugin_id=str(manifest_ref.get("plugin_id") or ""),
            contributes=contributes,
            runtime=runtime,
            package_sha256=str(manifest_ref.get("package_sha256") or ""),
        ),
        "requires": {
            "capabilities": _effective_required_capabilities_from_ref(
                requires, runtime
            ),
            "secrets": _string_list(requires.get("secrets")),
        },
        "arbitraryPackageLoadAllowed": False,
        "registryActivated": public_registry_activated,
        "installStateSchemaVersion": (
            PLUGIN_INSTALL_STATE_SCHEMA_VERSION if install_state else None
        ),
        "disabledReason": disabled_reason or None,
    }
    if descriptor_package is not None:
        plugin["workbenchDescriptorPackage"] = descriptor_package["package"]
        plugin["workbenchDescriptorManifests"] = descriptor_package["manifests"]
    return plugin


def _runtime_failed_plugin_from_catalog(
    *,
    project_id: str,
    plugin_id: str,
    catalog_entry: dict[str, Any],
    install_state: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schemaVersion": RUNTIME_PLUGIN_SCHEMA_VERSION,
        "pluginId": plugin_id,
        "version": "",
        "installState": "failed",
        "activation": str((install_state or {}).get("activation") or "failed"),
        "runtimeSource": str(
            catalog_entry.get("runtime_source") or "plugin.load_receipt"
        ),
        "receiptId": None,
        "manifestSha256": str(catalog_entry.get("manifest_sha256") or ""),
        "packageSha256": "",
        "byteCount": 0,
        "source": _json_object(catalog_entry.get("install_source")),
        "contributionSummary": [],
        "frontendComponentBindings": [],
        "requires": {"capabilities": [], "secrets": []},
        "arbitraryPackageLoadAllowed": _safe_bool(
            (install_state or {}).get("arbitrary_package_load_allowed")
        ),
        "registryActivated": False,
        "installStateSchemaVersion": PLUGIN_INSTALL_STATE_SCHEMA_VERSION,
        "disabledReason": (install_state or {}).get("disabled_reason"),
        "installFailure": _json_object_or_none(catalog_entry.get("install_failure")),
        "projectId": project_id,
    }


def _install_plan_execution_response(
    *,
    project_id: str,
    plugin_id: str,
    source: dict[str, Any],
    install_state: str,
    activation: str,
    receipt_id: str | None,
    manifest_sha256: str,
    package_sha256: str,
    arbitrary_package_load_allowed: bool,
    install_failure: dict[str, Any] | None,
    descriptor_package: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schemaVersion": PLUGIN_INSTALL_PLAN_EXECUTION_SCHEMA_VERSION,
        "projectId": project_id,
        "pluginId": plugin_id,
        "source": source,
        "installState": install_state,
        "activation": activation,
        "runtimeSource": "plugin.load_receipt",
        "receiptId": receipt_id,
        "manifestSha256": manifest_sha256,
        "packageSha256": package_sha256,
        "arbitraryPackageLoadAllowed": arbitrary_package_load_allowed,
        "installFailure": install_failure,
        "layoutMutated": False,
    }
    if descriptor_package is not None:
        payload["workbenchDescriptorPackage"] = descriptor_package["package"]
        payload["workbenchDescriptorManifests"] = descriptor_package["manifests"]
    return payload


def _install_failure_response(payload: dict[str, Any]) -> dict[str, Any]:
    failure = payload.get("installFailure")
    if isinstance(failure, dict):
        return dict(failure)
    return {
        "code": "plugin_install_failed",
        "message": "trusted-local plugin install failed",
        "retryable": True,
    }


def _bundled_plugins_root() -> Path:
    """Host-side resolution root for the 'bundled' install source kind.

    Bundled plugins ship as normal SDK-built packages checked into
    src/frisket/authoring/bundled_plugins/<plugin_id>/ inside the wheel. They are
    trusted by *provenance*, not by any new grant: the package bytes are
    part of the product build the operator already installed, so this
    function's only job is to resolve a bare id to that fixed on-disk
    location before handing off to the SAME trusted-local install/load/
    activate path (execute_workbench_plugin_local_install_plan below) --
    bundled installs get no capability, no arbitraryPackageLoadAllowed
    bypass, and no manifest-validation exemption beyond what trusted-local
    install already grants.

    This is a plain function (not a module constant) so tests can
    monkeypatch it to a fixture root without ever writing a real package
    into the shipped src/frisket/authoring/bundled_plugins/ tree -- keeping every
    OTHER project's runtime index (and the empty-by-default assumption
    baked into existing plugin runtime-index tests) unaffected until a real
    bundled package is added.
    """
    return Path(__file__).resolve().parent.parent / "bundled_plugins"


def shipped_bundled_plugin_ids() -> frozenset[str]:
    """Ids of package directories shipped with a manifest."""
    root = _bundled_plugins_root()
    if not root.is_dir():
        return frozenset()
    return frozenset(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and (entry / "plugin.json").is_file()
    )


def _bundled_install_manifest_path(source: dict[str, Any], *, plugin_id: str) -> Path:
    value = source.get("value")
    if not isinstance(value, str) or not BUNDLED_PLUGIN_VALUE_RE.fullmatch(value):
        raise WorkbenchPluginLifecycleError(
            "plugin_install_source_invalid",
            "bundled install source value must be a bare bundled plugin "
            "package name (no path separators or leading '.')",
            status_code=400,
        )
    root = _bundled_plugins_root().resolve()
    package_root = (root / value).resolve()
    # Defense in depth: BUNDLED_PLUGIN_VALUE_RE already forbids '/', '\\',
    # and any leading '.' (so '..' and absolute-looking values never reach
    # here), but re-derive containment from the resolved path too, so a
    # symlinked bundled root -- or a future regex change -- still can't
    # walk the resolution outside the bundled plugins tree.
    if package_root != root and root not in package_root.parents:
        raise WorkbenchPluginLifecycleError(
            "plugin_install_source_invalid",
            "bundled install source resolved outside the bundled plugins root",
            status_code=400,
        )
    manifest_path = package_root / "plugin.json"
    if not manifest_path.is_file():
        raise WorkbenchPluginLifecycleError(
            "plugin_install_source_invalid",
            f"no bundled plugin package named {value!r}",
            status_code=400,
        )
    return manifest_path


def _local_install_manifest_path(source: dict[str, Any], *, plugin_id: str) -> Path:
    kind = source.get("kind")
    if kind == "bundled":
        return _bundled_install_manifest_path(source, plugin_id=plugin_id)
    raw_path = source.get("value") if kind == "localPath" else source.get("path")
    if kind not in {"localPath", "local_file"} or not isinstance(raw_path, str):
        raise WorkbenchPluginLifecycleError(
            "plugin_install_source_invalid",
            "trusted-local install source must be a localPath or local_file source",
            status_code=400,
        )
    path = Path(raw_path).expanduser().resolve()
    if path.is_dir():
        path = path / "plugin.json"
    return path


def _local_install_idempotency_key(*, plugin_id: str, manifest_path: Path) -> str:
    try:
        package_identity = live_plugin_package_identity_from_ref(
            manifest_path.resolve().parent,
            None,
        )
        package_marker = package_identity.package_sha256
    except PluginPackageIdentityError:
        package_marker = "package_identity_unavailable"
    payload = {
        "plugin_id": plugin_id,
        "manifest_path": str(manifest_path.resolve()),
        "package_sha256": package_marker,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"workbench-plugin-install-local@sha256:{digest}"


def _plugin_manifest_ref_from_action_result(result: Any) -> dict[str, Any] | None:
    refs = []
    for output in getattr(result, "outputs", []) or []:
        ref = getattr(output, "ref", None)
        if isinstance(ref, dict) and ref.get("kind") == "plugin_manifest":
            refs.append(ref)
    return refs[0] if len(refs) == 1 else None


def _install_failure_from_action_result(result: Any) -> dict[str, Any]:
    errors = getattr(result, "errors", []) or []
    first = errors[0] if errors else None
    code = str(getattr(first, "code", "") or "plugin_install_failed")
    message = str(
        getattr(first, "message", "") or "trusted-local plugin install failed"
    )
    details = getattr(first, "details", None)
    failure: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": True,
    }
    if isinstance(details, dict):
        failure["details"] = details
    return failure


_PROJECT_INSTALL_COLUMNS = (
    "plugin_id, install_state, activation, permissions_accepted, "
    "arbitrary_package_load_allowed, disabled_reason, "
    "executable_handlers_allowed, updated_at"
)


def _workbench_plugin_install_states(project: Project) -> dict[str, dict[str, Any]]:
    rows = project.db.execute(
        f"SELECT {_PROJECT_INSTALL_COLUMNS} FROM workbench_plugin_installs"
    ).fetchall()
    return {str(row["plugin_id"]): dict(row) for row in rows}


def _workbench_plugin_install_state(
    project: Project, *, plugin_id: str
) -> dict[str, Any] | None:
    row = project.db.execute(
        f"SELECT {_PROJECT_INSTALL_COLUMNS} "
        "FROM workbench_plugin_installs WHERE plugin_id=?",
        (plugin_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _upsert_workbench_plugin_install_state(
    project: Project,
    *,
    plugin_id: str,
    install_state: str,
    activation: str,
    permissions_accepted: list[str],
    arbitrary_package_load_allowed: bool = False,
    disabled_reason: str | None = None,
) -> None:
    """Write ONLY this project's enablement row.

    Package identity lives in the workspace catalog, never here. The executable
    trust grant (``executable_handlers_allowed``) is owned by
    ``_record_backend_activation_executable_handlers_grant`` and is deliberately
    NOT touched on conflict, so an enablement flip preserves an existing grant.
    """
    if install_state not in {
        "installed",
        "enabled",
        "disabled",
        "uninstalled",
        "failed",
    }:
        raise WorkbenchPluginLifecycleError(
            "plugin_lifecycle_state_invalid",
            "workbench plugin install state is not recognized",
            status_code=500,
        )
    normalized_permissions = sorted(
        {
            permission
            for permission in permissions_accepted
            if isinstance(permission, str) and permission
        }
    )
    try:
        project.db.execute(
            "INSERT INTO workbench_plugin_installs ("
            "plugin_id, install_state, activation, permissions_accepted, "
            "arbitrary_package_load_allowed, disabled_reason, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(plugin_id) DO UPDATE SET "
            "install_state=excluded.install_state, "
            "activation=excluded.activation, "
            "permissions_accepted=excluded.permissions_accepted, "
            "arbitrary_package_load_allowed=excluded.arbitrary_package_load_allowed, "
            "disabled_reason=excluded.disabled_reason, "
            "updated_at=excluded.updated_at",
            (
                plugin_id,
                install_state,
                activation,
                json.dumps(normalized_permissions, separators=(",", ":")),
                1 if arbitrary_package_load_allowed else 0,
                disabled_reason,
            ),
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise


def _record_backend_activation_executable_handlers_grant(
    project: Project, *, plugin_id: str, executable_handlers_allowed: bool
) -> None:
    """Persist backend activation's executableHandlersAllowed grant onto the
    plugin's existing install-state row (PERSIST THE GRANT, constraint A of
    plugin-runtime-binding-restart-rehydration-nonbundled-v1).

    Deliberately a narrow UPDATE rather than routing through
    ``_upsert_workbench_plugin_install_state``: this call only ever runs after
    ``activate_workbench_plugin_backend_contributions`` has already confirmed
    an ``enabled`` install-state row exists for ``plugin_id`` (it reads that
    row earlier in the same call), so there is nothing to insert here, and a
    full upsert would require re-deriving/overwriting unrelated fields
    (``activation``, ``permissions_accepted``, ...) this call has no opinion
    about. A changed grant is recorded whether True or False, so an explicit
    later False overwrites an earlier True; an unchanged grant leaves the
    existing timestamp intact."""
    desired = 1 if executable_handlers_allowed else 0
    try:
        current = project.db.execute(
            "SELECT executable_handlers_allowed FROM workbench_plugin_installs "
            "WHERE plugin_id=?",
            (plugin_id,),
        ).fetchone()
        if (
            current is not None
            and int(current["executable_handlers_allowed"]) == desired
        ):
            return
        project.db.execute(
            "UPDATE workbench_plugin_installs "
            "SET executable_handlers_allowed=?, updated_at=datetime('now') "
            "WHERE plugin_id=?",
            (desired, plugin_id),
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise


def _restore_backend_activation_executable_handlers_grant(
    project: Project,
    *,
    plugin_id: str,
    executable_handlers_allowed: int,
    updated_at: str,
) -> None:
    try:
        current = project.db.execute(
            "SELECT executable_handlers_allowed, updated_at "
            "FROM workbench_plugin_installs WHERE plugin_id=?",
            (plugin_id,),
        ).fetchone()
        if current is None:
            raise RuntimeError(
                f"plugin install disappeared while restoring activation: {plugin_id}"
            )
        if (
            int(current["executable_handlers_allowed"]) == executable_handlers_allowed
            and str(current["updated_at"]) == updated_at
        ):
            return
        project.db.execute(
            "UPDATE workbench_plugin_installs "
            "SET executable_handlers_allowed=?, updated_at=? WHERE plugin_id=?",
            (executable_handlers_allowed, updated_at, plugin_id),
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise


def _install_state_response(
    row: dict[str, Any],
    catalog_entry: dict[str, Any],
    *,
    project_id: str,
    registry_activated: bool,
) -> dict[str, Any]:
    """Merge this project's enablement row with the workspace catalog identity."""
    return {
        "schemaVersion": PLUGIN_INSTALL_STATE_SCHEMA_VERSION,
        "projectId": project_id,
        "pluginId": str(row.get("plugin_id") or ""),
        "receiptId": str(catalog_entry.get("receipt_id") or ""),
        "manifestSha256": str(catalog_entry.get("manifest_sha256") or ""),
        "packageSha256": str(catalog_entry.get("package_sha256") or ""),
        "installState": str(row.get("install_state") or ""),
        "activation": str(row.get("activation") or ""),
        "runtimeSource": str(
            catalog_entry.get("runtime_source") or "plugin.load_receipt"
        ),
        "permissionsAccepted": _json_string_list(row.get("permissions_accepted")),
        "registryActivated": registry_activated,
        "arbitraryPackageLoadAllowed": _safe_bool(
            row.get("arbitrary_package_load_allowed")
        ),
        "disabledReason": row.get("disabled_reason"),
        "source": _json_object(catalog_entry.get("install_source")),
        "installFailure": _json_object_or_none(catalog_entry.get("install_failure")),
    }


def _catalog_identity_refs(entry: dict[str, Any]) -> tuple[str, str, str]:
    """(receipt_id, manifest_sha256, package_sha256) from a catalog entry.

    The workspace catalog is the single identity authority; a catalog entry
    without these is corrupt (a valid identity always carries all three)."""
    receipt_id = str(entry.get("receipt_id") or "")
    manifest_sha256 = str(entry.get("manifest_sha256") or "")
    package_sha256 = str(entry.get("package_sha256") or "")
    if not receipt_id or not manifest_sha256 or not package_sha256:
        raise WorkbenchPluginLifecycleError(
            "plugin_lifecycle_state_corrupt",
            "workspace catalog package is missing identity evidence",
            status_code=409,
        )
    return receipt_id, manifest_sha256, package_sha256


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return False


def _json_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return (
        [item for item in parsed if isinstance(item, str)]
        if isinstance(parsed, list)
        else []
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_object_or_none(value: Any) -> dict[str, Any] | None:
    parsed = _json_object(value)
    return parsed or None


def _effective_required_capabilities(manifest: PluginManifest) -> list[str]:
    capabilities = list(manifest.requires.capabilities)
    if (
        _manifest_declares_executable_backend(manifest)
        and TRUSTED_LOCAL_BACKEND_CAPABILITY not in capabilities
    ):
        capabilities.append(TRUSTED_LOCAL_BACKEND_CAPABILITY)
    return capabilities


def _effective_required_capabilities_from_ref(
    requires: dict[str, Any],
    runtime: dict[str, Any],
) -> list[str]:
    capabilities = _string_list(requires.get("capabilities"))
    if (
        _runtime_ref_declares_executable_backend(runtime)
        and TRUSTED_LOCAL_BACKEND_CAPABILITY not in capabilities
    ):
        capabilities.append(TRUSTED_LOCAL_BACKEND_CAPABILITY)
    return capabilities


def _manifest_declares_executable_backend(manifest: PluginManifest) -> bool:
    runtime = manifest.runtime
    for bindings in (
        runtime.actions,
        runtime.importers,
        runtime.operators,
        runtime.projections,
        runtime.job_handlers,
    ):
        for binding in bindings:
            if binding.module_path:
                return True
            if binding.handler_api in {
                "typed_action",
                "plugin_importer",
                "plugin_operator",
                "plugin_projection",
            }:
                return True
    return False


def _runtime_ref_declares_executable_backend(runtime: dict[str, Any]) -> bool:
    for key in ("actions", "importers", "operators", "projections", "job_handlers"):
        values = runtime.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("module_path"), str) and item.get("module_path"):
                return True
            if item.get("handler_api") in {
                "typed_action",
                "plugin_importer",
                "plugin_operator",
                "plugin_projection",
            }:
                return True
    return False


def _contribution_summary(contributes: dict[str, Any]) -> list[dict[str, Any]]:
    kinds = (
        ("workbench_views", "workbench_view"),
        ("workbench_panels", "workbench_panel"),
        ("workbench_commands", "workbench_command"),
        ("actions", "action"),
        ("importers", "importer"),
        ("operators", "operator"),
        ("projections", "projection"),
        ("column_types", "column_type"),
        ("job_handlers", "job_handler"),
    )
    summary: list[dict[str, Any]] = []
    for manifest_key, public_kind in kinds:
        ids = _string_list(contributes.get(manifest_key))
        if ids:
            summary.append({"kind": public_kind, "count": len(ids), "ids": ids})
    return summary


def _frontend_component_bindings(
    *,
    project_id: str,
    plugin_id: str,
    contributes: dict[str, Any],
    runtime: dict[str, Any],
    package_sha256: str = "",
) -> list[dict[str, str]]:
    declared_contribution_ids = {
        *_string_list(contributes.get("workbench_views")),
        *_string_list(contributes.get("workbench_panels")),
        *_string_list(contributes.get("workbench_commands")),
        *(
            item.get("kind")
            for item in runtime.get("actions", [])
            if item.get("handler_api") == "typed_action"
            and item.get("kind") in _string_list(contributes.get("actions"))
        ),
    }
    raw_bindings = runtime.get("workbench_components")
    if not declared_contribution_ids or not isinstance(raw_bindings, list):
        return []
    bindings: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_bindings:
        if not isinstance(item, dict):
            continue
        try:
            binding = PluginManifestWorkbenchComponentBinding.model_validate(item)
        except Exception:
            continue
        contribution_id = binding.contribution_id
        if contribution_id not in declared_contribution_ids:
            continue
        if contribution_id in seen:
            return []
        seen.add(contribution_id)
        public_binding = {
            "schemaVersion": FRONTEND_COMPONENT_BINDING_SCHEMA_VERSION,
            "contributionId": contribution_id,
            "moduleKey": binding.module_key,
            "componentKey": binding.component_key,
        }
        if binding.module_path is not None:
            public_binding["modulePath"] = binding.module_path
            module_url = (
                f"/api/projects/{quote(project_id, safe='')}/workbench/plugins/"
                f"{quote(plugin_id, safe='')}/frontend-components/"
                f"{quote(contribution_id, safe='')}/module.js"
            )
            if package_sha256:
                module_url = f"{module_url}?package={quote(package_sha256, safe='')}"
            public_binding["moduleUrl"] = module_url
        bindings.append(public_binding)
    return bindings


def _workbench_descriptor_package(
    manifest_ref: dict[str, Any],
) -> dict[str, Any] | None:
    package = manifest_ref.get("workbench_descriptor_package")
    if not isinstance(package, dict):
        return None
    if package.get("kind") != "workbench_descriptor_package":
        return None
    if package.get("schema_version") != WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION:
        return None
    descriptor_manifests = package.get("descriptor_manifests")
    if not isinstance(descriptor_manifests, list):
        return None
    manifests = [item for item in descriptor_manifests if isinstance(item, dict)]
    if len(manifests) != len(descriptor_manifests):
        return None
    path = package.get("path")
    runtime_only_fields_stripped = _string_list(
        package.get("runtime_only_fields_stripped")
    )
    return {
        "package": {
            "schemaVersion": WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION,
            "sourcePath": str(path) if isinstance(path, str) else "",
            "descriptorCount": len(manifests),
            "runtimeOnlyFieldsStripped": runtime_only_fields_stripped,
        },
        "manifests": manifests,
    }


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

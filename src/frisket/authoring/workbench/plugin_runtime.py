"""Workbench plugin activation and lifecycle orchestration.

Package IDENTITY (validated source, manifest/package
digests, ``plugin.load`` manifest evidence) is owned by ONE workspace catalog
(``plugin_package_catalog``), keyed by ``plugin_id`` -- exactly one durable
package per plugin per workspace. A project owns ONLY enablement
(enabled/disabled + reason), accepted capabilities, settings, secrets and the
per-project executable trust grant. Every surface that answers "what package is
this plugin / may this project use it" derives from (workspace catalog for
identity) + (project row for enablement); no surface holds its own identity.

Because the package is registered once per workspace (at ``Workspace``
construction) and stays registered, the old cross-project reconciliation
machinery is gone: no workspace-root claims, no ``*.frisket/project.db`` glob,
no per-restart re-registration sweeps, no drift/conflict repair. A project
disable only flips that project's enablement; it never unregisters the shared
package. Dispatch consent stays project-scoped and is enforced at the
``project_runtime_binding`` seam.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from frisket.authoring.plugin_registry import default_registry
from frisket.authoring.workbench.plugin_package_catalog import (
    PluginPackageCatalog,
    plugin_package_lifecycle_lock,
    plugin_package_catalog_for_project,
    plugin_package_catalog_for_root,
)
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    _empty_runtime_bindings,
    _register_backend_contribution_metadata,
    _register_executable_job_handlers,
    _register_executable_runtime_bindings,
    _restore_backend_contributions,
    _snapshot_backend_contributions,
    _validated_backend_contribution_metadata,
    _validated_executable_job_handlers,
    _validated_executable_runtime_bindings,
)
from frisket.authoring.workbench.plugin_runtime_shared import (
    PLUGIN_ACTIVATION_SCHEMA_VERSION,
    PLUGIN_BACKEND_ACTIVATION_SCHEMA_VERSION,
    PluginCompositionPolicy,
    WorkbenchPluginActivationError,
    WorkbenchPluginLifecycleError,
    active_plugin_composition_policy,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    _bundled_plugins_root,
    _catalog_identity_refs,
    _effective_required_capabilities,
    _install_failure_from_action_result,
    _install_plan_execution_response,
    _install_state_response,
    _json_object,
    _json_string_list,
    _loaded_manifest_from_ref,
    _local_install_idempotency_key,
    _local_install_manifest_path,
    _manifest_declares_executable_backend,
    _plugin_manifest_ref_from_action_result,
    _record_backend_activation_executable_handlers_grant,
    _upsert_workbench_plugin_install_state,
    _workbench_descriptor_package,
    _workbench_plugin_install_state,
    composition_policy_permits_catalog_entry,
    workspace_permits_plugin,
)
from frisket.contracts.plugin import LoadedPluginManifest
from frisket.engine.store import Project

_LOGGER = logging.getLogger(__name__)
_PLUGIN_ACTIVATION_LOCK = threading.RLock()
_WORKSPACE_SEED_LOCK = threading.RLock()


def _run_activation_cleanup(
    primary: Exception,
    *,
    plugin_id: str,
    operations: tuple[tuple[str, Callable[[], None]], ...],
) -> None:
    for label, cleanup in operations:
        try:
            cleanup()
        except Exception as cleanup_error:
            note = (
                f"plugin activation cleanup {label} failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
            try:
                primary.add_note(note)
            except Exception:
                pass
            try:
                _LOGGER.exception(
                    "plugin activation cleanup %s failed for %s",
                    label,
                    plugin_id,
                    extra={
                        "event": "plugin_activation_cleanup_failed",
                        "plugin_id": plugin_id,
                        "cleanup": label,
                    },
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Workspace-owned package catalog: seed once, register once
# ---------------------------------------------------------------------------


def ensure_workspace_plugin_packages(
    workspace_root: str | Path,
    *,
    policy: PluginCompositionPolicy | None = None,
) -> PluginPackageCatalog:
    """Seed the workspace package catalog and register its packages ONCE.

    Called at workspace/worker construction and idempotently by direct project
    bootstrap. The active composition policy is the sole admission authority:

    1. Seed only admitted shipped bundled packages into the catalog.
    2. Rehydrate the process-global registry from admitted catalog entries
       (bundled and previously-installed non-bundled): register the
       manifest and its backend contributions. Registering executable runtime
       bindings imports NO plugin code (the handlers are subprocess proxies +
       metadata), so it is safe to do for every package that ships executable
       backend; per-project CONSENT to actually dispatch that code stays in the
       project row and is enforced at ``project_runtime_binding``.

    Idempotent and fail-soft: a broken bundled package or a package whose
    on-disk source vanished logs loudly and is skipped; it never bricks
    construction for the rest of the workspace.
    """
    with plugin_package_lifecycle_lock(workspace_root):
        catalog = plugin_package_catalog_for_root(workspace_root)
        effective_policy = policy or active_plugin_composition_policy()
        with _WORKSPACE_SEED_LOCK:
            _seed_bundled_catalog_packages(catalog, policy=effective_policy)
            _rehydrate_registry_from_catalog(catalog, policy=effective_policy)
    return catalog


def _seed_bundled_catalog_packages(
    catalog: PluginPackageCatalog,
    *,
    policy: PluginCompositionPolicy,
) -> None:
    from frisket.plugins.load_evidence import (
        PluginLoadEvidenceError,
        build_bundled_plugin_load_evidence,
    )

    root = _bundled_plugins_root()
    try:
        package_dirs = (
            sorted(entry.name for entry in root.iterdir() if entry.is_dir())
            if root.is_dir()
            else []
        )
    except OSError:
        _LOGGER.exception(
            "bundled plugins root could not be listed for catalog seeding",
            extra={"event": "bundled_plugins_root_listing_failed"},
        )
        return
    for plugin_id in package_dirs:
        manifest_path = root / plugin_id / "plugin.json"
        if not manifest_path.is_file():
            continue
        if catalog.is_deleted(plugin_id):
            continue
        if not policy.permits(plugin_id, is_reviewed_bundled=True):
            continue
        try:
            evidence = build_bundled_plugin_load_evidence(manifest_path)
        except PluginLoadEvidenceError as exc:
            _LOGGER.exception(
                "bundled plugin %r failed catalog seeding validation -- skipped",
                plugin_id,
                extra={
                    "event": "bundled_plugin_catalog_seed_failed",
                    "plugin_id": plugin_id,
                    "code": exc.code,
                },
            )
            catalog.record_failure(
                plugin_id=plugin_id,
                install_source={"kind": "bundled", "value": plugin_id},
                install_failure={
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": True,
                },
                clear_tombstone=False,
            )
            continue
        except Exception:
            _LOGGER.exception(
                "bundled plugin %r failed catalog seeding -- skipped",
                plugin_id,
                extra={
                    "event": "bundled_plugin_catalog_seed_failed",
                    "plugin_id": plugin_id,
                },
            )
            continue
        manifest_ref = evidence.manifest_ref
        package_sha256 = str(manifest_ref.get("package_sha256") or "")
        existing = catalog.get(plugin_id)
        if (
            existing is not None
            and existing.get("package_sha256") == package_sha256
            and existing.get("manifest_sha256") == evidence.loaded.sha256
        ):
            # Unchanged bundled package: keep the durable evidence as-is. A
            # drifted package (new bytes) falls through and re-seeds -- this is
            # the ONLY drift path now (no per-project drift repair).
            continue
        catalog.upsert(
            plugin_id=plugin_id,
            install_source={"kind": "bundled", "value": plugin_id},
            manifest_sha256=evidence.loaded.sha256,
            package_sha256=package_sha256,
            receipt_id=f"bundled:{plugin_id}@{package_sha256}",
            manifest_ref=manifest_ref,
            has_executable_backend=_manifest_declares_executable_backend(
                evidence.loaded.manifest
            ),
            clear_tombstone=False,
        )


def _rehydrate_registry_from_catalog(
    catalog: PluginPackageCatalog, *, policy: PluginCompositionPolicy
) -> None:
    for plugin_id, entry in sorted(catalog.all().items()):
        if not composition_policy_permits_catalog_entry(
            plugin_id, entry, policy=policy
        ):
            continue
        manifest_ref = catalog.manifest_ref(plugin_id)
        if manifest_ref is None:
            continue
        try:
            loaded = _loaded_manifest_from_ref(manifest_ref)
            install_source = _json_object(entry.get("install_source"))
            # Register executable code into the SHARED registry only where this
            # workspace has consented: a bundled package carries the standing
            # host grant, any other package must have been granted executable
            # handlers at least once (catalog ``executable_activated``). A
            # merely-installed, never-granted package gets manifest + metadata
            # only, so its executable backend never enters the registry until a
            # project consents. Per-project dispatch consent is still enforced
            # at ``project_runtime_binding``.
            register_executable = bool(entry.get("has_executable_backend")) and (
                install_source.get("kind") == "bundled"
                or bool(entry.get("executable_activated"))
            )
            _register_catalog_package_into_registry(
                loaded,
                manifest_ref=manifest_ref,
                install_source=install_source,
                register_executable=register_executable,
            )
        except Exception:
            _LOGGER.exception(
                "workspace catalog package %r failed registry rehydration -- "
                "workspace keeps serving without it",
                plugin_id,
                extra={
                    "event": "catalog_package_rehydration_failed",
                    "plugin_id": plugin_id,
                },
            )
            continue


def _register_catalog_package_into_registry(
    loaded: LoadedPluginManifest,
    *,
    manifest_ref: dict[str, Any],
    install_source: dict[str, Any],
    register_executable: bool,
) -> None:
    """Register one catalog package's manifest + backend contributions.

    The catalog is the single identity authority, so a stale registry entry for
    the same id (different sha) is REPLACED with the catalog's identity rather
    than raising a cross-project conflict. Registering executable runtime
    bindings imports no plugin code (subprocess proxies); dispatch consent is a
    per-project gate."""
    registry = default_registry()
    plugin_id = loaded.manifest.id
    validated_metadata = _validated_backend_contribution_metadata(
        loaded, manifest_ref=manifest_ref
    )
    try:
        job_handlers = (
            _validated_executable_job_handlers(loaded) if register_executable else {}
        )
    except WorkbenchPluginActivationError:
        job_handlers = {}
    runtime_bindings = (
        _validated_executable_runtime_bindings(
            loaded,
            manifest_ref=manifest_ref,
            install_source=install_source,
        )
        if register_executable
        else _empty_runtime_bindings()
    )
    with _PLUGIN_ACTIVATION_LOCK:
        existing = {item.manifest.id: item for item in registry.plugin_manifests()}.get(
            plugin_id
        )
        if existing is not None and existing.sha256 != loaded.sha256:
            registry.unload_plugin_contributions(plugin_id)
        if existing is None or existing.sha256 != loaded.sha256:
            registry.register_plugin_manifest(loaded, replace=True)
        _register_backend_contribution_metadata(
            loaded, manifest_ref=manifest_ref, validated=validated_metadata
        )
        if register_executable:
            if job_handlers:
                _register_executable_job_handlers(loaded, job_handlers)
            _register_executable_runtime_bindings(loaded, runtime_bindings)


def execute_workbench_plugin_local_install_plan(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    source: dict[str, Any],
    arbitrary_package_load_allowed: bool,
) -> tuple[int, dict[str, Any]]:
    with plugin_package_lifecycle_lock(project.path.parent):
        return _execute_workbench_plugin_local_install_plan_locked(
            project,
            project_id=project_id,
            plugin_id=plugin_id,
            source=source,
            arbitrary_package_load_allowed=arbitrary_package_load_allowed,
        )


def _execute_workbench_plugin_local_install_plan_locked(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    source: dict[str, Any],
    arbitrary_package_load_allowed: bool,
) -> tuple[int, dict[str, Any]]:
    catalog = plugin_package_catalog_for_project(project)

    def record_failure(
        failure: dict[str, Any], *, status_code: int, record_catalog: bool = True
    ) -> tuple[int, dict[str, Any]]:
        with _PLUGIN_ACTIVATION_LOCK:
            current_entry = catalog.get(plugin_id)
            preserves_package = bool(
                current_entry
                and current_entry.get("manifest_sha256")
                and current_entry.get("package_sha256")
                and current_entry.get("manifest_ref")
            )
            preserves_tombstone = catalog.is_deleted(plugin_id)
            if record_catalog and not preserves_package and not preserves_tombstone:
                catalog.record_failure(
                    plugin_id=plugin_id,
                    install_source=source,
                    install_failure=failure,
                )
            if not preserves_package and not preserves_tombstone:
                _upsert_workbench_plugin_install_state(
                    project,
                    plugin_id=plugin_id,
                    install_state="failed",
                    activation="failed",
                    permissions_accepted=[],
                    arbitrary_package_load_allowed=False,
                    disabled_reason=None,
                )
        return status_code, _install_plan_execution_response(
            project_id=project_id,
            plugin_id=plugin_id,
            source=source,
            install_state="failed",
            activation="failed",
            receipt_id=None,
            manifest_sha256="",
            package_sha256="",
            arbitrary_package_load_allowed=False,
            install_failure=failure,
        )

    policy = active_plugin_composition_policy()
    source_kind = source.get("kind")
    install_permitted = (
        policy.permits(plugin_id, is_reviewed_bundled=True)
        if source_kind == "bundled"
        else policy.nonbundled_enabled
    )
    if not install_permitted:
        return record_failure(
            {
                "code": "plugin_install_not_available_under_composition_policy",
                "message": (
                    "plugin installation is not available for this plugin under "
                    "the active composition policy"
                ),
                "retryable": False,
            },
            status_code=403,
            # A denied request may not overwrite an admitted package shared by
            # other projects in the workspace catalog.
            record_catalog=False,
        )

    if arbitrary_package_load_allowed:
        return record_failure(
            {
                "code": "plugin_install_arbitrary_package_load_blocked",
                "message": "trusted-local install plans do not allow arbitrary package loading",
                "retryable": False,
            },
            status_code=403,
        )

    try:
        manifest_path = _local_install_manifest_path(source, plugin_id=plugin_id)
    except WorkbenchPluginLifecycleError as exc:
        return record_failure(
            {"code": exc.code, "message": exc.message, "retryable": True},
            status_code=exc.status_code,
        )

    from frisket.engine.executor import run_action_spec

    action = {
        "action_id": "plugin.load",
        "scope": {"kind": "project"},
        "params": {
            "manifest": {
                "kind": "local_file",
                "path": str(manifest_path),
            }
        },
        "idempotency_key": _local_install_idempotency_key(
            plugin_id=plugin_id,
            manifest_path=manifest_path,
        ),
    }
    result = run_action_spec(project, action, project_id=project_id)
    manifest_ref = _plugin_manifest_ref_from_action_result(result)
    if (
        result.status != "completed"
        or manifest_ref is None
        or result.receipt_id is None
    ):
        return record_failure(
            _install_failure_from_action_result(result), status_code=409
        )
    if manifest_ref.get("plugin_id") != plugin_id:
        return record_failure(
            {
                "code": "plugin_install_manifest_mismatch",
                "message": "plugin.load manifest evidence does not match the requested plugin id",
                "retryable": False,
            },
            status_code=409,
        )

    manifest_sha256 = str(manifest_ref.get("manifest_sha256") or "")
    package_sha256 = str(manifest_ref.get("package_sha256") or "")
    loaded = _loaded_manifest_from_ref(manifest_ref)
    with _PLUGIN_ACTIVATION_LOCK:
        catalog.upsert(
            plugin_id=plugin_id,
            install_source=source,
            manifest_sha256=manifest_sha256,
            package_sha256=package_sha256,
            receipt_id=result.receipt_id,
            manifest_ref=manifest_ref,
            has_executable_backend=_manifest_declares_executable_backend(
                loaded.manifest
            ),
        )
        _upsert_workbench_plugin_install_state(
            project,
            plugin_id=plugin_id,
            install_state="installed",
            activation="manifestLoaded",
            permissions_accepted=[],
            arbitrary_package_load_allowed=False,
            disabled_reason=None,
        )
    descriptor_package = _workbench_descriptor_package(manifest_ref)
    return 200, _install_plan_execution_response(
        project_id=project_id,
        plugin_id=plugin_id,
        source=source,
        install_state="installed",
        activation="manifestLoaded",
        receipt_id=result.receipt_id,
        manifest_sha256=manifest_sha256,
        package_sha256=package_sha256,
        arbitrary_package_load_allowed=False,
        install_failure=None,
        descriptor_package=descriptor_package,
    )


def bootstrap_project_bundled_plugins(
    project: Project, *, project_id: str
) -> list[dict[str, Any]]:
    with plugin_package_lifecycle_lock(project.path.parent):
        return _bootstrap_project_bundled_plugins_locked(project, project_id=project_id)


def _bootstrap_project_bundled_plugins_locked(
    project: Project, *, project_id: str
) -> list[dict[str, Any]]:
    """Seed this project's ENABLEMENT rows for bundled catalog packages.

    Package identity is workspace-owned and seeded once at ``Workspace``
    construction; this only owns the per-project enablement half. It ensures the
    workspace startup has already seeded the catalog and registry exactly once.
    For each BUNDLED catalog package, it seeds an enablement
    row for out-of-box parity: enabled (with the standing host executable grant)
    unless the manifest opted out of auto-enable, in which case it rests
    installed. ANY existing project row is an explicit operator choice and is
    left untouched -- re-bootstrap never re-enables a disabled/uninstalled
    plugin. Non-bundled packages are never auto-seeded: a project owns its own
    non-bundled enablement through the install/activate routes.
    """
    catalog = plugin_package_catalog_for_project(project)
    results: list[dict[str, Any]] = []
    for plugin_id, entry in sorted(catalog.all().items()):
        if not composition_policy_permits_catalog_entry(plugin_id, entry):
            continue
        source = _json_object(entry.get("install_source"))
        if source.get("kind") != "bundled":
            continue
        manifest_ref = catalog.manifest_ref(plugin_id)
        if manifest_ref is None:
            # A bundled package that failed catalog seeding: surface it as a
            # per-project failed row so Settings -> Plugins shows it.
            results.append({"pluginId": plugin_id, "installState": "failed"})
            continue
        existing = _workbench_plugin_install_state(project, plugin_id=plugin_id)
        if existing is not None:
            results.append(
                {
                    "pluginId": plugin_id,
                    "installState": str(existing.get("install_state")),
                    "bootstrap": "skipped_existing_state",
                }
            )
            continue
        try:
            loaded = _loaded_manifest_from_ref(manifest_ref)
        except WorkbenchPluginActivationError:
            _LOGGER.exception(
                "bundled plugin %r evidence could not be loaded for project %s",
                plugin_id,
                project_id,
                extra={
                    "event": "bundled_plugin_enablement_seed_failed",
                    "plugin_id": plugin_id,
                    "project_id": project_id,
                },
            )
            continue
        if not getattr(loaded.manifest, "auto_enable", True):
            _upsert_workbench_plugin_install_state(
                project,
                plugin_id=plugin_id,
                install_state="installed",
                activation="manifestLoaded",
                permissions_accepted=[],
                arbitrary_package_load_allowed=False,
                disabled_reason=None,
            )
            results.append(
                {
                    "pluginId": plugin_id,
                    "installState": "installed",
                    "bootstrap": "installed_auto_enable_opt_out",
                }
            )
            continue
        required_capabilities = sorted(
            _effective_required_capabilities(loaded.manifest)
        )
        _upsert_workbench_plugin_install_state(
            project,
            plugin_id=plugin_id,
            install_state="enabled",
            activation="registryManifestRegistered",
            permissions_accepted=required_capabilities,
            arbitrary_package_load_allowed=False,
            disabled_reason=None,
        )
        # Bundled packages carry the standing host executable grant.
        _record_backend_activation_executable_handlers_grant(
            project, plugin_id=plugin_id, executable_handlers_allowed=True
        )
        results.append(
            {
                "pluginId": plugin_id,
                "installState": "enabled",
                "bootstrap": "installed",
            }
        )
    return results


def activate_workbench_plugin_manifest(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    receipt_id: str,
    trust_acknowledged: bool,
    permissions_accepted: list[str],
    arbitrary_package_load_allowed: bool,
) -> dict[str, Any]:
    if not trust_acknowledged:
        raise WorkbenchPluginActivationError(
            "plugin_activation_trust_required",
            "plugin activation requires explicit trust acknowledgement",
            status_code=403,
        )
    if arbitrary_package_load_allowed:
        raise WorkbenchPluginActivationError(
            "plugin_activation_arbitrary_package_load_blocked",
            "workbench plugin activation does not allow arbitrary package loading",
            status_code=403,
        )
    catalog = plugin_package_catalog_for_project(project)
    entry = catalog.get(plugin_id)
    manifest_ref = catalog.manifest_ref(plugin_id)
    if entry is None or manifest_ref is None:
        raise WorkbenchPluginActivationError(
            "plugin_activation_manifest_missing",
            "workspace catalog has no package identity for this plugin",
            status_code=404,
        )
    if str(entry.get("receipt_id") or "") != receipt_id:
        raise WorkbenchPluginActivationError(
            "plugin_activation_manifest_mismatch",
            "activation receipt does not match the workspace catalog package",
            status_code=404,
        )
    if manifest_ref.get("plugin_id") != plugin_id:
        raise WorkbenchPluginActivationError(
            "plugin_activation_manifest_mismatch",
            "workspace catalog package does not match the requested plugin id",
            status_code=404,
        )
    loaded = _loaded_manifest_from_ref(manifest_ref)
    if not workspace_permits_plugin(project, plugin_id, catalog_entry=entry):
        raise WorkbenchPluginActivationError(
            "plugin_activation_not_available_under_composition_policy",
            "plugin activation is not available for this plugin under the "
            "active composition policy",
            status_code=403,
        )
    required_capabilities = set(_effective_required_capabilities(loaded.manifest))
    accepted_capabilities = set(permissions_accepted)
    missing_permissions = [
        capability
        for capability in sorted(required_capabilities - accepted_capabilities)
    ]
    if missing_permissions:
        raise WorkbenchPluginActivationError(
            "plugin_activation_permissions_missing",
            "plugin activation did not accept all manifest capability requirements",
            status_code=403,
        )
    unexpected_permissions = sorted(accepted_capabilities - required_capabilities)
    if unexpected_permissions:
        raise WorkbenchPluginActivationError(
            "plugin_activation_permissions_unexpected",
            "plugin activation accepted capabilities not declared by the manifest",
            status_code=400,
        )

    with _PLUGIN_ACTIVATION_LOCK:
        return _publish_workbench_plugin_manifest(
            project,
            project_id=project_id,
            plugin_id=plugin_id,
            receipt_id=receipt_id,
            loaded=loaded,
            manifest_ref=manifest_ref,
            permissions_accepted=permissions_accepted,
        )


def _publish_workbench_plugin_manifest(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    receipt_id: str,
    loaded: LoadedPluginManifest,
    manifest_ref: dict[str, Any],
    permissions_accepted: list[str],
) -> dict[str, Any]:
    registry = default_registry()
    existing = {item.manifest.id: item for item in registry.plugin_manifests()}.get(
        plugin_id
    )
    # The workspace catalog is the single identity authority: a stale registry
    # entry with a different sha is refreshed to the catalog's identity, never
    # treated as cross-project contention.
    if existing is not None and existing.sha256 != loaded.sha256:
        registry.unload_plugin_contributions(plugin_id)
    if existing is None or existing.sha256 != loaded.sha256:
        registry.register_plugin_manifest(loaded, replace=True)
    registered = {item.manifest.id: item for item in registry.plugin_manifests()}.get(
        plugin_id
    )
    if registered is None or registered.sha256 != loaded.sha256:
        raise WorkbenchPluginActivationError(
            "plugin_activation_registry_mismatch",
            "registry activation did not match catalog manifest evidence",
            status_code=500,
        )
    _upsert_workbench_plugin_install_state(
        project,
        plugin_id=plugin_id,
        install_state="enabled",
        activation="registryManifestRegistered",
        permissions_accepted=permissions_accepted,
        arbitrary_package_load_allowed=False,
        disabled_reason=None,
    )
    return {
        "schemaVersion": PLUGIN_ACTIVATION_SCHEMA_VERSION,
        "projectId": project_id,
        "pluginId": plugin_id,
        "receiptId": receipt_id,
        "manifestSha256": loaded.sha256,
        "packageSha256": str(manifest_ref.get("package_sha256") or ""),
        "runtimeSource": "plugin.load_receipt",
        "activation": "registryManifestRegistered",
        "installState": "enabled",
        "registryActivated": True,
        "arbitraryPackageLoadAllowed": False,
        "permissionsAccepted": permissions_accepted,
        "registeredPluginManifests": [plugin_id],
    }


def activate_workbench_plugin_backend_contributions(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    trust_acknowledged: bool,
    arbitrary_package_load_allowed: bool,
    executable_handlers_allowed: bool = False,
) -> dict[str, Any]:
    with plugin_package_lifecycle_lock(project.path.parent):
        return _activate_workbench_plugin_backend_contributions_locked_entry(
            project,
            project_id=project_id,
            plugin_id=plugin_id,
            trust_acknowledged=trust_acknowledged,
            arbitrary_package_load_allowed=arbitrary_package_load_allowed,
            executable_handlers_allowed=executable_handlers_allowed,
        )


def _activate_workbench_plugin_backend_contributions_locked_entry(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    trust_acknowledged: bool,
    arbitrary_package_load_allowed: bool,
    executable_handlers_allowed: bool,
) -> dict[str, Any]:
    if not trust_acknowledged:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_trust_required",
            "plugin backend activation requires explicit trust acknowledgement",
            status_code=403,
        )
    if arbitrary_package_load_allowed:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_arbitrary_package_load_blocked",
            "workbench backend activation does not allow arbitrary package loading",
            status_code=403,
        )
    with _PLUGIN_ACTIVATION_LOCK:
        return _activate_workbench_plugin_backend_contributions_locked(
            project,
            project_id=project_id,
            plugin_id=plugin_id,
            executable_handlers_allowed=executable_handlers_allowed,
        )


def _activate_workbench_plugin_backend_contributions_locked(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    executable_handlers_allowed: bool,
) -> dict[str, Any]:
    install_state = _workbench_plugin_install_state(project, plugin_id=plugin_id)
    if install_state is None or install_state.get("install_state") != "enabled":
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_not_enabled",
            "backend contribution activation requires an enabled workbench plugin",
            status_code=409,
        )
    catalog = plugin_package_catalog_for_project(project)
    entry = catalog.get(plugin_id)
    manifest_ref = catalog.manifest_ref(plugin_id)
    if entry is None or manifest_ref is None:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_manifest_mismatch",
            "workspace catalog has no package identity for this plugin",
            status_code=409,
        )
    if not workspace_permits_plugin(project, plugin_id, catalog_entry=entry):
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_not_available_under_composition_policy",
            "backend activation is not available for this plugin under the "
            "active composition policy",
            status_code=403,
        )
    receipt_id, manifest_sha256, package_sha256 = _catalog_identity_refs(entry)
    if manifest_ref.get("plugin_id") != plugin_id:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_manifest_mismatch",
            "workspace catalog package does not match the requested plugin id",
            status_code=409,
        )
    loaded = _loaded_manifest_from_ref(manifest_ref)
    if loaded.sha256 != manifest_sha256:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_manifest_mismatch",
            "workspace catalog manifest sha does not match its manifest evidence",
            status_code=409,
        )
    if str(manifest_ref.get("package_sha256") or "") != package_sha256:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_package_mismatch",
            "workspace catalog package sha does not match its manifest evidence",
            status_code=409,
        )
    accepted_capabilities = set(
        _json_string_list(install_state.get("permissions_accepted"))
    )
    required_capabilities = set(_effective_required_capabilities(loaded.manifest))
    if required_capabilities - accepted_capabilities:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_permissions_missing",
            "backend activation requires accepted manifest capabilities",
            status_code=409,
        )

    executable_job_handlers = (
        _validated_executable_job_handlers(loaded)
        if executable_handlers_allowed
        else {}
    )
    install_source = _json_object(entry.get("install_source"))
    executable_runtime_bindings = (
        _validated_executable_runtime_bindings(
            loaded,
            manifest_ref=manifest_ref,
            install_source=install_source,
        )
        if executable_handlers_allowed
        else _empty_runtime_bindings()
    )
    validated_metadata = _validated_backend_contribution_metadata(
        loaded, manifest_ref=manifest_ref
    )
    snapshot = _snapshot_backend_contributions(loaded)
    registry = default_registry()
    previous_manifest = {
        item.manifest.id: item for item in registry.plugin_manifests()
    }.get(plugin_id)
    previous_executable_grant = int(
        install_state.get("executable_handlers_allowed") or 0
    )
    previous_updated_at = str(install_state.get("updated_at") or "")
    try:
        _register_loaded_manifest_for_backend_activation(loaded)
        registered = _register_backend_contribution_metadata(
            loaded,
            manifest_ref=manifest_ref,
            validated=validated_metadata,
        )
        registered_executable = (
            _register_executable_job_handlers(loaded, executable_job_handlers)
            if executable_handlers_allowed
            else {"jobHandlers": []}
        )
        registered_runtime_bindings = (
            _register_executable_runtime_bindings(loaded, executable_runtime_bindings)
            if executable_handlers_allowed
            else _empty_runtime_bindings()
        )
        _record_backend_activation_executable_handlers_grant(
            project,
            plugin_id=plugin_id,
            executable_handlers_allowed=executable_handlers_allowed,
        )
        if executable_handlers_allowed:
            # Record the workspace-level consent so a later restart re-registers
            # this package's executable backend into the shared registry.
            catalog.mark_executable_activated(plugin_id, True)
    except Exception as primary:
        from frisket.authoring.workbench import plugin_runtime_status

        _run_activation_cleanup(
            primary,
            plugin_id=plugin_id,
            operations=(
                (
                    "durable grant",
                    lambda: (
                        plugin_runtime_status._restore_backend_activation_executable_handlers_grant(
                            project,
                            plugin_id=plugin_id,
                            executable_handlers_allowed=previous_executable_grant,
                            updated_at=previous_updated_at,
                        )
                    ),
                ),
                (
                    "backend contributions",
                    lambda: _restore_backend_contributions(loaded, snapshot),
                ),
                (
                    "manifest",
                    lambda: registry._restore_plugin_manifest(
                        plugin_id,
                        previous_manifest,
                        expected=loaded,
                    ),
                ),
            ),
        )
        raise
    return {
        "schemaVersion": PLUGIN_BACKEND_ACTIVATION_SCHEMA_VERSION,
        "projectId": project_id,
        "pluginId": plugin_id,
        "receiptId": receipt_id,
        "manifestSha256": loaded.sha256,
        "packageSha256": package_sha256,
        "runtimeSource": "plugin.load_receipt",
        "arbitraryPackageLoadAllowed": False,
        "executableHandlersRegistered": bool(registered_executable["jobHandlers"]),
        "trustedRuntimeBindingsRegistered": any(
            registered_runtime_bindings[key]
            for key in (
                "actions",
                "importers",
                "operators",
                "projections",
                "jobHandlers",
            )
        ),
        "registeredBackendContributions": registered,
        "registeredExecutableHandlers": registered_executable,
        "registeredRuntimeBindings": registered_runtime_bindings,
    }


def _register_loaded_manifest_for_backend_activation(
    loaded: LoadedPluginManifest,
) -> None:
    registry = default_registry()
    existing = {item.manifest.id: item for item in registry.plugin_manifests()}.get(
        loaded.manifest.id
    )
    # Single catalog authority: refresh a stale registry entry rather than
    # raising cross-project contention.
    if existing is None or existing.sha256 != loaded.sha256:
        registry.register_plugin_manifest(loaded, replace=True)


def disable_workbench_plugin(
    project: Project, *, project_id: str, plugin_id: str
) -> dict[str, Any]:
    with _PLUGIN_ACTIVATION_LOCK:
        return _lifecycle_transition(
            project,
            project_id=project_id,
            plugin_id=plugin_id,
            install_state="disabled",
            activation="blocked",
            disabled_reason="plugin_disabled",
        )


def uninstall_workbench_plugin(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    workspace_projects: tuple[Project, ...],
) -> dict[str, Any]:
    with plugin_package_lifecycle_lock(project.path.parent):
        return _uninstall_workbench_plugin_locked(
            project,
            project_id=project_id,
            plugin_id=plugin_id,
            workspace_projects=workspace_projects,
        )


def _uninstall_workbench_plugin_locked(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    workspace_projects: tuple[Project, ...],
) -> dict[str, Any]:
    with _PLUGIN_ACTIVATION_LOCK:
        current = _workbench_plugin_install_state(project, plugin_id=plugin_id)
        if current is None:
            raise WorkbenchPluginLifecycleError(
                "plugin_lifecycle_not_installed",
                "workbench plugin has no project install state to uninstall",
                status_code=404,
            )
        catalog = plugin_package_catalog_for_project(project)
        entry = catalog.get(plugin_id)
        if entry is None:
            if current.get("install_state") == "uninstalled":
                raise WorkbenchPluginLifecycleError(
                    "plugin_lifecycle_not_installed",
                    "workbench plugin has no workspace package to uninstall",
                    status_code=404,
                )
            raise WorkbenchPluginLifecycleError(
                "plugin_lifecycle_state_corrupt",
                "workbench plugin has enablement state but no workspace package identity",
                status_code=409,
            )
        if not workspace_permits_plugin(project, plugin_id, catalog_entry=entry):
            raise WorkbenchPluginLifecycleError(
                "plugin_not_available_under_composition_policy",
                "this workbench plugin is not available under the active composition policy",
                status_code=404,
            )
        _catalog_identity_refs(entry)

        # Clear every project's durable enablement and consent before removing
        # the catalog authority. If one project write fails, the catalog stays
        # intact and a retry can finish the idempotent cleanup; already-cleared
        # projects cannot regain consent from that still-present package.
        seen: set[Path] = set()
        for candidate in (*workspace_projects, project):
            candidate_path = candidate.path.resolve()
            if candidate_path in seen:
                continue
            seen.add(candidate_path)
            state = _workbench_plugin_install_state(candidate, plugin_id=plugin_id)
            if state is None:
                continue
            _upsert_workbench_plugin_install_state(
                candidate,
                plugin_id=plugin_id,
                install_state="uninstalled",
                activation="removed",
                permissions_accepted=[],
                arbitrary_package_load_allowed=False,
                disabled_reason=None,
            )
            _record_backend_activation_executable_handlers_grant(
                candidate,
                plugin_id=plugin_id,
                executable_handlers_allowed=False,
            )
        catalog.delete(plugin_id)
        default_registry().unload_plugin_entries(plugin_id)
        updated = _workbench_plugin_install_state(project, plugin_id=plugin_id)
        assert updated is not None
        return _install_state_response(
            updated, entry, project_id=project_id, registry_activated=False
        )


def _lifecycle_transition(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    install_state: str,
    activation: str,
    disabled_reason: str | None,
) -> dict[str, Any]:
    """Flip THIS project's enablement only.

    The shared workspace package stays registered and stays in the catalog --
    disabling in one project never unregisters code another project may still
    enable. Workspace-wide uninstall is a separate operation above.
    """
    current = _workbench_plugin_install_state(project, plugin_id=plugin_id)
    if current is None or current.get("install_state") == "uninstalled":
        raise WorkbenchPluginLifecycleError(
            "plugin_lifecycle_not_installed",
            "workbench plugin has no project install state to change",
            status_code=404,
        )
    catalog = plugin_package_catalog_for_project(project)
    entry = catalog.get(plugin_id)
    if entry is None:
        raise WorkbenchPluginLifecycleError(
            "plugin_lifecycle_state_corrupt",
            "workbench plugin has enablement state but no workspace package identity",
            status_code=409,
        )
    if not workspace_permits_plugin(project, plugin_id, catalog_entry=entry):
        raise WorkbenchPluginLifecycleError(
            "plugin_not_available_under_composition_policy",
            "this workbench plugin is not available under the active composition policy",
            status_code=404,
        )
    _catalog_identity_refs(entry)
    _upsert_workbench_plugin_install_state(
        project,
        plugin_id=plugin_id,
        install_state=install_state,
        activation=activation,
        permissions_accepted=_json_string_list(current.get("permissions_accepted")),
        arbitrary_package_load_allowed=False,
        disabled_reason=disabled_reason,
    )
    updated = _workbench_plugin_install_state(project, plugin_id=plugin_id)
    assert updated is not None
    return _install_state_response(
        updated, entry, project_id=project_id, registry_activated=False
    )

"""Shared plugin package validation and durable evidence construction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from frisket.contracts.actions.runtime import validate_plugin_manifest_contributions
from frisket.contracts.plugin import (
    LoadedPluginManifest,
    PluginManifestLoadError,
    load_plugin_manifest_file,
)
from frisket.plugins.frontend_modules import (
    PluginFrontendModuleError,
    validate_frontend_component_modules,
)
from frisket.plugins.package_identity import (
    PluginPackageIdentity,
    PluginPackageIdentityError,
    build_plugin_package_identity,
)
from frisket.authoring.workbench.contracts import (
    LoadedWorkbenchDescriptorPackage,
    WorkbenchDescriptorPackageLoadError,
    load_workbench_descriptor_package_file,
)


class PluginLoadEvidenceError(Exception):
    """A validation failure while building ``plugin.load`` manifest evidence.

    Carries the exact ``ActionError`` fields the receipt path reports, so the
    project-coupled action handler and the project-free workspace-catalog seeder
    share ONE validation + evidence-building path.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = details


@dataclass(frozen=True)
class PluginLoadEvidence:
    """Everything the ``plugin.load`` validation produces from a manifest path.

    ``manifest_ref`` is the durable evidence blob stored in the
    workspace package catalog; the remaining facts describe the validated
    package independently of canonical request identity.
    """

    loaded: LoadedPluginManifest
    descriptor_package: LoadedWorkbenchDescriptorPackage | None
    package_identity: PluginPackageIdentity
    frontend_component_bindings: list[dict[str, Any]]
    manifest_ref: dict[str, Any]


def build_plugin_load_evidence(
    action: Any,
    manifest_path: Path,
    *,
    request_hash: str,
) -> PluginLoadEvidence:
    """Validate a plugin manifest and build its ``plugin.load`` evidence.

    Pure (no project writes): the same validation the ``plugin.load`` action
    runs, factored out so bundled workspace-catalog seeding (which has no
    project in scope) shares it. ``action`` is used only for error labeling
    (``action.kind``); a lightweight namespace suffices for the seeder.
    """
    try:
        loaded = load_plugin_manifest_file(manifest_path)
    except PluginManifestLoadError as exc:
        raise PluginLoadEvidenceError(
            exc.code, exc.message, field="params.manifest.path"
        ) from exc
    descriptor_path = manifest_path.parent / "workbench-descriptors.json"
    descriptor_package: LoadedWorkbenchDescriptorPackage | None = None
    if descriptor_path.is_symlink() or (
        descriptor_path.exists() and not descriptor_path.is_file()
    ):
        raise PluginLoadEvidenceError(
            "invalid_workbench_descriptor_package_source",
            "workbench descriptor package path must point to a regular file",
            field="params.manifest.workbench_descriptors_path",
            details={"path": str(descriptor_path)},
        )
    if descriptor_path.is_file():
        try:
            descriptor_package = load_workbench_descriptor_package_file(descriptor_path)
            _validate_workbench_descriptor_package_for_manifest(
                loaded, descriptor_package
            )
        except WorkbenchDescriptorPackageLoadError as exc:
            raise PluginLoadEvidenceError(
                exc.code,
                exc.message,
                field="params.manifest.workbench_descriptors_path",
                details={"path": str(descriptor_path)},
            ) from exc
    contribution_error = validate_plugin_manifest_contributions(action, loaded)
    if contribution_error is not None and contribution_error.error is not None:
        err = contribution_error.error
        raise PluginLoadEvidenceError(
            err.code,
            err.message,
            field=err.field or "params.manifest.contributes",
            details=err.details,
        )
    try:
        frontend_component_bindings = validate_frontend_component_modules(
            loaded,
            manifest_path.parent,
        )
    except PluginFrontendModuleError as exc:
        raise PluginLoadEvidenceError(
            exc.code,
            exc.message,
            field="params.manifest.path",
            details=exc.details(),
        ) from exc
    try:
        package_identity = build_plugin_package_identity(
            loaded,
            manifest_path,
            descriptor_path=descriptor_path,
        )
    except PluginPackageIdentityError as exc:
        raise PluginLoadEvidenceError(
            exc.code,
            exc.message,
            field="params.manifest.path",
            details={"path": exc.path} if exc.path else {},
        ) from exc
    manifest_ref = _plugin_manifest_ref(
        loaded,
        str(manifest_path),
        request_hash=request_hash,
        descriptor_path=descriptor_path,
        descriptor_package=descriptor_package,
        package_identity=package_identity,
        frontend_component_bindings=frontend_component_bindings,
    )
    return PluginLoadEvidence(
        loaded=loaded,
        descriptor_package=descriptor_package,
        package_identity=package_identity,
        frontend_component_bindings=frontend_component_bindings,
        manifest_ref=manifest_ref,
    )


def build_bundled_plugin_load_evidence(manifest_path: Path) -> PluginLoadEvidence:
    """Project-free evidence build for workspace-catalog seeding.

    Uses a synthetic, deterministic ``request_hash`` derived from the manifest
    path so the seeded evidence is stable across runs without a real action.
    """
    request_hash = (
        "sha256:"
        + hashlib.sha256(str(manifest_path.resolve()).encode("utf-8")).hexdigest()
    )
    return build_plugin_load_evidence(
        SimpleNamespace(kind="plugin.load"),
        manifest_path,
        request_hash=request_hash,
    )


def _validate_workbench_descriptor_package_for_manifest(
    loaded: LoadedPluginManifest,
    descriptor_package: LoadedWorkbenchDescriptorPackage,
) -> None:
    declared_view_ids = set(loaded.manifest.contributes.workbench_views)
    declared_panel_ids = set(loaded.manifest.contributes.workbench_panels)
    declared_command_ids = set(loaded.manifest.contributes.workbench_commands)
    declared_ids = {*declared_view_ids, *declared_panel_ids, *declared_command_ids}
    undeclared_ids = [
        descriptor.get("id")
        for descriptor in descriptor_package.descriptor_manifests
        if descriptor.get("id") not in declared_ids
    ]
    if undeclared_ids:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "workbench descriptor package includes undeclared contributions: "
            + ", ".join(sorted(str(item) for item in undeclared_ids)),
        )
    # Namespacing hygiene: a trusted plugin must not ship contributions outside
    # its own id namespace, and a command's commandId must equal its
    # contribution id — otherwise a crafted package can shadow foreign
    # testids/ids at parse time.
    namespace_prefix = f"{loaded.manifest.id}."
    foreign_ids = [
        descriptor.get("id")
        for descriptor in descriptor_package.descriptor_manifests
        if not str(descriptor.get("id", "")).startswith(namespace_prefix)
    ]
    if foreign_ids:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "workbench contribution ids must be namespaced under the plugin id: "
            + ", ".join(sorted(str(item) for item in foreign_ids)),
        )
    mismatched_command_ids = [
        descriptor.get("id")
        for descriptor in descriptor_package.descriptor_manifests
        if descriptor.get("kind") == "command"
        and descriptor.get("commandId") != descriptor.get("id")
    ]
    if mismatched_command_ids:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "command descriptors must use commandId equal to the contribution id: "
            + ", ".join(sorted(str(item) for item in mismatched_command_ids)),
        )
    wrong_owner_ids = [
        descriptor.get("id")
        for descriptor in descriptor_package.descriptor_manifests
        if descriptor.get("ownerPluginId") != loaded.manifest.id
    ]
    if wrong_owner_ids:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "workbench descriptor package ownerPluginId must match the plugin id: "
            + ", ".join(sorted(str(item) for item in wrong_owner_ids)),
        )
    wrong_schema_ids = [
        descriptor.get("id")
        for descriptor in descriptor_package.descriptor_manifests
        if (
            (
                descriptor.get("id") in declared_view_ids
                and descriptor.get("schemaVersion") != "frisket.workbench.view.v1"
            )
            or (
                descriptor.get("id") in declared_panel_ids
                and descriptor.get("schemaVersion") != "frisket.workbench.panel.v1"
            )
            or (
                descriptor.get("id") in declared_command_ids
                and descriptor.get("schemaVersion") != "frisket.command.v1"
            )
        )
    ]
    if wrong_schema_ids:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "workbench descriptor package schemaVersion does not match manifest "
            "contribution category: "
            + ", ".join(sorted(str(item) for item in wrong_schema_ids)),
        )
    present_ids = {
        descriptor.get("id")
        for descriptor in descriptor_package.descriptor_manifests
        if isinstance(descriptor.get("id"), str)
    }
    component_bound_ids = {
        binding.contribution_id
        for binding in loaded.manifest.runtime.workbench_components
    }
    missing_component_bound_ids = sorted(component_bound_ids - present_ids)
    if missing_component_bound_ids:
        raise WorkbenchDescriptorPackageLoadError(
            "invalid_workbench_descriptor_package",
            "workbench descriptor package is missing runtime component "
            "contribution descriptors: " + ", ".join(missing_component_bound_ids),
        )


def _plugin_manifest_ref(
    loaded: LoadedPluginManifest,
    manifest_path: str,
    *,
    request_hash: str,
    descriptor_path: Path,
    descriptor_package: LoadedWorkbenchDescriptorPackage | None = None,
    package_identity: PluginPackageIdentity | None = None,
    frontend_component_bindings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "kind": "plugin_manifest",
        "schema_version": loaded.manifest.schema_version,
        "plugin_id": loaded.manifest.id,
        "version": loaded.manifest.version,
        "path": manifest_path,
        "source": {
            "kind": "local_file",
            "display_path": Path(manifest_path).name,
        },
        "request_hash": request_hash,
        "manifest_sha256": loaded.sha256,
        "byte_count": loaded.byte_count,
        "contributes": loaded.manifest.contributes.model_dump(mode="json"),
        "requires": loaded.manifest.requires.model_dump(mode="json"),
        "runtime": loaded.manifest.runtime.model_dump(mode="json"),
        "settings": [
            setting.model_dump(mode="json") for setting in loaded.manifest.settings
        ],
        # Carry the opt-in flag through the plugin.load manifest evidence so
        # the reconstructed manifest (_loaded_manifest_from_ref) preserves an
        # auto_enable:false opt-out rather than silently defaulting it back to
        # True at bootstrap.
        "auto_enable": loaded.manifest.auto_enable,
    }
    if package_identity is not None:
        package_ref = package_identity.to_ref()
        ref["package_sha256"] = package_ref["package_sha256"]
        ref["package_identity"] = package_ref
    if frontend_component_bindings is not None:
        ref["frontend_component_bindings"] = frontend_component_bindings
    if descriptor_package is not None:
        ref["workbench_descriptor_package"] = {
            "kind": "workbench_descriptor_package",
            "schema_version": descriptor_package.schema_version,
            "path": str(descriptor_path),
            "source": {
                "kind": "local_file",
                "display_path": descriptor_path.name,
            },
            "descriptor_sha256": descriptor_package.sha256,
            "byte_count": descriptor_package.byte_count,
            "descriptor_manifests": descriptor_package.descriptor_manifests,
            "runtime_only_fields_stripped": (
                descriptor_package.runtime_only_fields_stripped
            ),
        }
    return ref

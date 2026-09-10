"""Workbench plugin capability and host registration."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.authoring import column_types
from frisket.authoring.plugin_registry import (
    ImporterSpec,
    JobHandlerSpec,
    PluginRegistry,
    RuntimeBindingSpec,
    default_registry,
    get_trusted_backend_handler,
)
from frisket.authoring.workbench.plugin_runtime_settings import (
    _normalize_plugin_env_name,
)
from frisket.authoring.workbench.plugin_runtime_shared import (
    WorkbenchPluginActivationError,
)
from frisket.authoring.workbench.plugin_package_catalog import (
    plugin_package_catalog_for_project,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    _assert_manifest_ref_module_integrity,
    _effective_required_capabilities,
    _loaded_manifest_from_ref,
    _json_string_list,
    _safe_bool,
    _workbench_plugin_install_state,
    _workbench_plugin_install_states,
    project_accepts_current_plugin_capabilities,
    workspace_permits_plugin,
    runtime_binding_dispatch_error,
)
from frisket.contracts.plugin import (
    LoadedPluginManifest,
)
from frisket.engine.store import Project
from frisket.features.url_classification.matchers import UrlMatcher


@dataclass(frozen=True)
class _ValidatedBackendContributionMetadata:
    column_type_ids: tuple[str, ...]
    importer_ids: tuple[str, ...]
    job_handler_ids: tuple[str, ...]
    column_type_metadata: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class _BackendContributionSnapshot:
    column_types: dict[str, column_types.ColumnTypeSpec | None]
    importers: dict[str, ImporterSpec | None]
    job_handlers: dict[str, JobHandlerSpec | None]
    matchers: dict[str, UrlMatcher | None]
    runtime_bindings: dict[str, dict[str, RuntimeBindingSpec | None]]


def project_runtime_binding(
    project: Project,
    *,
    binding_type: str,
    kind: str,
) -> RuntimeBindingSpec | None:
    """Return a runtime binding only when this project may dispatch it."""
    if not kind:
        return None
    registry = default_registry()
    binding = next(
        (
            spec
            for spec in registry.runtime_binding_specs(binding_type)
            if spec.kind == kind and spec.handler is not None
        ),
        None,
    )
    if binding is None:
        return None

    registered = {loaded.manifest.id: loaded for loaded in registry.plugin_manifests()}
    loaded = registered.get(binding.plugin)
    if loaded is None:
        return None
    if not _loaded_manifest_declares_runtime_binding(loaded, binding):
        return None

    # ENABLEMENT gate stays project-scoped (this project's row).
    install_state = _workbench_plugin_install_state(project, plugin_id=binding.plugin)
    if install_state is None or install_state.get("install_state") != "enabled":
        return None
    # IDENTITY gate comes from the workspace catalog: the one
    # durable package per plugin_id per workspace. No per-project package digest
    # exists any more.
    catalog = plugin_package_catalog_for_project(project)
    catalog_entry = catalog.get(binding.plugin)
    manifest_ref = catalog.manifest_ref(binding.plugin)
    if catalog_entry is None or manifest_ref is None:
        return None
    if not workspace_permits_plugin(
        project, binding.plugin, catalog_entry=catalog_entry
    ):
        return None
    try:
        current_loaded = _loaded_manifest_from_ref(manifest_ref)
    except WorkbenchPluginActivationError:
        return None
    if not _loaded_manifest_declares_runtime_binding(current_loaded, binding):
        return None
    if not project_accepts_current_plugin_capabilities(
        install_state, current_loaded.manifest
    ):
        return None
    manifest_sha256 = str(catalog_entry.get("manifest_sha256") or "")
    if manifest_sha256 != loaded.sha256 or current_loaded.sha256 != loaded.sha256:
        return None
    package_sha256 = str(catalog_entry.get("package_sha256") or "")
    if not package_sha256:
        return None
    binding_package_sha256 = str(binding.metadata.get("package_sha256") or "")
    if binding_package_sha256 and binding_package_sha256 != package_sha256:
        # Binding has package identity recorded but it does not match the
        # workspace catalog digest.  Reject to prevent dispatch of a stale or
        # mismatched package version.
        return None
    # CONSENT gate stays project-scoped: a plugin's OWN executable code
    # (subprocess bindings) may dispatch only where THIS project granted the
    # executable trust. The package is registered workspace-wide, so this
    # per-project grant is what keeps one project's consent from letting another
    # project dispatch. Envelope / in-process trusted handlers carry no package
    # identity and need no grant — consistent with _assert_runtime_binding_integrity.
    handler_api = str(getattr(binding, "handler_api", ""))
    if (
        handler_api.endswith("_subprocess")
        or handler_api == "plugin_typed_action_native"
    ) and not _safe_bool(install_state.get("executable_handlers_allowed")):
        return None
    return binding


def runtime_importer_dispatch_error(
    project: Project, binding: RuntimeBindingSpec, *, bounded: bool
) -> dict[str, Any] | None:
    """Check an already project-admitted importer before invoking trusted code."""
    install = _workbench_plugin_install_state(project, plugin_id=binding.plugin)
    grants = _json_string_list((install or {}).get("permissions_accepted"))
    error = runtime_binding_dispatch_error(binding, capabilities=grants)
    if error is not None or not bounded:
        return error
    loaded = next(
        item
        for item in default_registry().plugin_manifests()
        if item.manifest.id == binding.plugin
    )
    from frisket.contracts.actions.runtime import TRUSTED_LOCAL_BACKEND_CAPABILITY
    from frisket.contracts.plugin_write_plan import (
        PROJECT_READS_CAPABILITY,
        PROJECT_WRITES_CAPABILITY,
    )

    local_requirements = {
        TRUSTED_LOCAL_BACKEND_CAPABILITY,
        PROJECT_READS_CAPABILITY,
        PROJECT_WRITES_CAPABILITY,
    }

    if (
        loaded.manifest.requires.secrets
        or set(loaded.manifest.requires.capabilities) - local_requirements
    ):
        return {
            "code": "preview_effect_requires_run",
            "message": "Importer secret or external-effect requirements need a durable run.",
            "field": "params.importer_kind",
        }
    return None


def enabled_workbench_plugin_ids(project: Project) -> set[str]:
    """Plugin ids enabled for this project, used by project-scoped catalogs.

    Excludes any enabled plugin the composition policy does not expose -- a
    plugin persisted-enabled under a prior policy never re-surfaces here.
    """
    catalog = plugin_package_catalog_for_project(project)
    registered = {
        loaded.manifest.id: loaded for loaded in default_registry().plugin_manifests()
    }
    enabled: set[str] = set()
    for plugin_id, install_state in _workbench_plugin_install_states(project).items():
        if install_state.get("install_state") != "enabled":
            continue
        entry = catalog.get(plugin_id)
        manifest_ref = catalog.manifest_ref(plugin_id)
        registered_loaded = registered.get(plugin_id)
        if entry is None or manifest_ref is None or registered_loaded is None:
            continue
        if not workspace_permits_plugin(project, plugin_id, catalog_entry=entry):
            continue
        try:
            current_loaded = _loaded_manifest_from_ref(manifest_ref)
        except WorkbenchPluginActivationError:
            continue
        manifest_sha256 = str(entry.get("manifest_sha256") or "")
        if (
            not manifest_sha256
            or current_loaded.sha256 != manifest_sha256
            or registered_loaded.sha256 != manifest_sha256
        ):
            continue
        if not project_accepts_current_plugin_capabilities(
            install_state, current_loaded.manifest
        ):
            continue
        enabled.add(plugin_id)
    return enabled


def project_allows_plugin_column_type(
    project: Project,
    type_spec: column_types.ColumnTypeSpec,
) -> bool:
    if type_spec.core:
        return True
    if type_spec.plugin == "external":
        return True
    return type_spec.plugin in enabled_workbench_plugin_ids(project)


def _loaded_manifest_declares_runtime_binding(
    loaded: LoadedPluginManifest, binding: RuntimeBindingSpec
) -> bool:
    declarations = getattr(loaded.manifest.runtime, binding.binding_type, None)
    if declarations is None:
        return False
    return any(
        item.kind == binding.kind and item.handler_key == binding.handler_key
        for item in declarations
    )


def _register_backend_contribution_metadata(
    loaded: LoadedPluginManifest,
    *,
    manifest_ref: dict[str, Any],
    validated: _ValidatedBackendContributionMetadata | None = None,
) -> dict[str, list[str]]:
    registry = default_registry()
    plugin_id = loaded.manifest.id
    plan = validated or _validated_backend_contribution_metadata(
        loaded, manifest_ref=manifest_ref
    )

    for type_id in plan.column_type_ids:
        if column_types.get_column_type(type_id) is None:
            metadata = plan.column_type_metadata.get(type_id, {})
            registry.register_column_type(
                type_id,
                validate=_column_type_validator_from_metadata(metadata),
                parse=_column_type_parser_from_metadata(metadata),
                presentation=_column_type_presentation_from_metadata(metadata),
                description=str(
                    metadata.get("description")
                    or f"Declared by workbench plugin {plugin_id}."
                ),
                plugin=plugin_id,
            )
    for importer_id in plan.importer_ids:
        existing = {spec.name: spec for spec in registry.importer_specs()}.get(
            importer_id
        )
        if existing is None:
            registry.register_importer(
                importer_id,
                description=f"Declared by workbench plugin {plugin_id}.",
                plugin=plugin_id,
                handler=None,
            )
    for job_handler_id in plan.job_handler_ids:
        existing = {spec.kind: spec for spec in registry.job_handler_specs()}.get(
            job_handler_id
        )
        if existing is None:
            registry.register_job_handler(
                job_handler_id,
                description=f"Declared by workbench plugin {plugin_id}.",
                plugin=plugin_id,
                handler=None,
            )

    # Matcher evaluation is host-side metadata, NOT executable handler code,
    # so it registers here on the always-run metadata path (not gated behind
    # executableHandlersAllowed).
    # register_plugin_matchers re-applies the trust caps as defense in depth.
    from frisket.features.url_classification.plugin_matchers import (
        register_plugin_matchers,
    )

    matcher_ids = register_plugin_matchers(loaded.manifest, replace=True)

    return {
        "columnTypes": list(plan.column_type_ids),
        "importers": list(plan.importer_ids),
        "jobHandlers": list(plan.job_handler_ids),
        "matchers": matcher_ids,
    }


def _validated_backend_contribution_metadata(
    loaded: LoadedPluginManifest,
    *,
    manifest_ref: dict[str, Any],
) -> _ValidatedBackendContributionMetadata:
    contributes = loaded.manifest.contributes
    plan = _ValidatedBackendContributionMetadata(
        column_type_ids=tuple(contributes.column_types),
        importer_ids=tuple(contributes.importers),
        job_handler_ids=tuple(contributes.job_handlers),
        column_type_metadata=_load_plugin_column_type_metadata(
            loaded, manifest_ref=manifest_ref
        ),
    )
    _assert_backend_contribution_conflicts(
        plugin_id=loaded.manifest.id,
        column_type_ids=list(plan.column_type_ids),
        importer_ids=list(plan.importer_ids),
        job_handler_ids=list(plan.job_handler_ids),
    )
    from frisket.features.url_classification.plugin_matchers import (
        _validate_plugin_matchers,
    )

    _validate_plugin_matchers(loaded.manifest)
    return plan


def _snapshot_backend_contributions(
    loaded: LoadedPluginManifest,
) -> _BackendContributionSnapshot:
    registry = default_registry()
    contributes = loaded.manifest.contributes
    importer_specs = {spec.name: spec for spec in registry.importer_specs()}
    job_handler_specs = {spec.kind: spec for spec in registry.job_handler_specs()}
    from frisket.features.url_classification.registry import registered_matchers

    matchers = {matcher.matcher_id: matcher for matcher in registered_matchers()}
    runtime_bindings: dict[str, dict[str, RuntimeBindingSpec | None]] = {}
    for binding_type in ("actions", "importers", "operators", "projections"):
        existing = {
            spec.kind: spec for spec in registry.runtime_binding_specs(binding_type)
        }
        runtime_bindings[binding_type] = {
            binding.kind: existing.get(binding.kind)
            for binding in getattr(loaded.manifest.runtime, binding_type)
        }
    return _BackendContributionSnapshot(
        column_types={
            name: column_types.get_column_type(name)
            for name in contributes.column_types
        },
        importers={name: importer_specs.get(name) for name in contributes.importers},
        job_handlers={
            kind: job_handler_specs.get(kind) for kind in contributes.job_handlers
        },
        matchers={
            matcher.matcher_id: matchers.get(matcher.matcher_id)
            for matcher in loaded.manifest.runtime.matchers
        },
        runtime_bindings=runtime_bindings,
    )


def _restore_backend_contributions(
    loaded: LoadedPluginManifest, snapshot: _BackendContributionSnapshot
) -> None:
    registry = default_registry()
    plugin_id = loaded.manifest.id
    errors: list[Exception] = []

    def attempt(label: str, restore: Callable[[], None]) -> None:
        try:
            restore()
        except Exception as exc:
            try:
                exc.add_note(f"while restoring plugin activation {label}")
            except Exception:
                pass
            errors.append(exc)

    for binding_type, bindings in snapshot.runtime_bindings.items():
        for kind, prior in bindings.items():
            attempt(
                f"runtime binding {binding_type}/{kind}",
                lambda binding_type=binding_type, kind=kind, prior=prior: (
                    registry._restore_runtime_binding(
                        binding_type,
                        kind,
                        prior,
                        expected_plugin=plugin_id,
                    )
                ),
            )

    for kind, prior in snapshot.job_handlers.items():
        attempt(
            f"job handler {kind}",
            lambda kind=kind, prior=prior: registry._restore_job_handler(
                kind,
                prior,
                expected_plugin=plugin_id,
            ),
        )

    from frisket.features.url_classification.registry import (
        _restore_matcher,
    )

    for matcher_id, prior in snapshot.matchers.items():
        attempt(
            f"URL matcher {matcher_id}",
            lambda matcher_id=matcher_id, prior=prior: _restore_matcher(
                matcher_id,
                prior,
                expected_plugin_id=plugin_id,
            ),
        )

    for name, prior in snapshot.importers.items():
        attempt(
            f"importer {name}",
            lambda name=name, prior=prior: _restore_new_importer(
                registry, name=name, prior=prior, plugin_id=plugin_id
            ),
        )

    for name, prior in snapshot.column_types.items():
        attempt(
            f"column type {name}",
            lambda name=name, prior=prior: _restore_new_column_type(
                name=name, prior=prior, plugin_id=plugin_id
            ),
        )

    if errors:
        raise ExceptionGroup("plugin activation contribution rollback failed", errors)


def _restore_new_importer(
    registry: PluginRegistry,
    *,
    name: str,
    prior: ImporterSpec | None,
    plugin_id: str,
) -> None:
    published = {spec.name: spec for spec in registry.importer_specs()}.get(name)
    if published is prior:
        return
    if prior is not None or published is None or published.plugin != plugin_id:
        raise RuntimeError(f"importer changed while restoring activation: {name}")
    registry.unregister_importer(name)


def _restore_new_column_type(
    *,
    name: str,
    prior: column_types.ColumnTypeSpec | None,
    plugin_id: str,
) -> None:
    published = column_types.get_column_type(name)
    if published is prior:
        return
    if (
        prior is not None
        or published is None
        or published.core
        or published.plugin != plugin_id
    ):
        raise RuntimeError(f"column type changed while restoring activation: {name}")
    column_types.unregister_column_type(name)


def _load_plugin_column_type_metadata(
    loaded: LoadedPluginManifest,
    *,
    manifest_ref: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    path_value = manifest_ref.get("path")
    if not isinstance(path_value, str) or not path_value:
        return {}
    manifest_path = Path(path_value)
    metadata_path = manifest_path.parent / "column-types.json"
    if not metadata_path.is_file():
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = payload.get("column_types") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}
    declared = set(loaded.manifest.contributes.column_types)
    metadata: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name in declared:
            metadata[name] = entry
    return metadata


def _column_type_presentation_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    presentation = metadata.get("presentation")
    if isinstance(presentation, dict):
        return dict(presentation)
    base = metadata.get("base")
    renderer = metadata.get("renderer")
    result: dict[str, Any] = {}
    if isinstance(renderer, str) and renderer:
        result["renderer"] = renderer
    if isinstance(base, str) and base:
        result["base"] = base
    return result


def _column_type_validator_from_metadata(
    metadata: dict[str, Any],
) -> Any:
    validation = metadata.get("validation")
    if not isinstance(validation, dict):
        return None
    if validation.get("kind") == "regex":
        pattern = validation.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return None
        try:
            compiled = re.compile(pattern)
        except re.error:
            return None

        def validate_regex(value: Any) -> bool:
            return isinstance(value, str) and compiled.fullmatch(value) is not None

        return validate_regex
    if validation.get("kind") != "number_range":
        return None
    minimum = validation.get("min")
    maximum = validation.get("max")
    if not isinstance(minimum, (int, float)) or isinstance(minimum, bool):
        return None
    if not isinstance(maximum, (int, float)) or isinstance(maximum, bool):
        return None

    def validate(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and minimum <= value <= maximum
        )

    return validate


def _column_type_parser_from_metadata(
    metadata: dict[str, Any],
) -> Any:
    parse_from = metadata.get("parse_from")
    if isinstance(parse_from, dict):
        entries = [parse_from]
    elif isinstance(parse_from, list):
        entries = [entry for entry in parse_from if isinstance(entry, dict)]
    else:
        entries = []
    for entry in entries:
        if entry.get("source") != "text":
            continue
        if entry.get("kind") == "case_id":
            return _parse_case_id_text
    return None


def _parse_case_id_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("case_id values must be text")
    text = value.strip()
    if not text:
        return None
    match = re.fullmatch(r"case[\s_-]*(\d{1,4})", text, flags=re.IGNORECASE)
    if match is None:
        raise ValueError("case_id values must look like CASE-0000")
    return f"CASE-{int(match.group(1)):04d}"


def _validated_executable_job_handlers(
    loaded: LoadedPluginManifest,
) -> dict[str, Any]:
    plugin_id = loaded.manifest.id
    declared_job_handlers = set(loaded.manifest.contributes.job_handlers)
    bindings = loaded.manifest.runtime.job_handlers
    handlers: dict[str, Any] = {}
    registry = default_registry()
    existing_specs = {spec.kind: spec for spec in registry.job_handler_specs()}
    for binding in bindings:
        if not binding.handler_key.startswith(f"{plugin_id}:"):
            raise WorkbenchPluginActivationError(
                "plugin_backend_activation_handler_binding_invalid",
                "runtime job handler key must be namespaced by the plugin id",
                status_code=409,
            )
        if binding.kind not in declared_job_handlers:
            raise WorkbenchPluginActivationError(
                "plugin_backend_activation_handler_binding_invalid",
                "runtime job handler binding must reference a declared contribution",
                status_code=409,
            )
        handler = get_trusted_backend_handler(binding.handler_key)
        if handler is None:
            raise WorkbenchPluginActivationError(
                "plugin_backend_activation_handler_key_unknown",
                "runtime job handler binding references an unknown trusted handler key",
                status_code=409,
            )
        existing = existing_specs.get(binding.kind)
        if existing is not None and existing.plugin != plugin_id:
            _raise_backend_contribution_conflict("job_handler", binding.kind)
        handlers[binding.kind] = handler
    return handlers


def _empty_runtime_bindings() -> dict[str, list[str]]:
    return {
        "actions": [],
        "importers": [],
        "operators": [],
        "projections": [],
        "jobHandlers": [],
    }


def _trusted_local_native_action_proxy(*_args: Any, **_kwargs: Any) -> object:
    raise RuntimeError("installed Actions dispatch through native Action binding")


def _trusted_local_subprocess_importer_proxy(*_args: Any, **_kwargs: Any) -> object:
    raise RuntimeError("trusted-local plugin importers dispatch through subprocesses")


def _trusted_local_subprocess_operator_proxy(*_args: Any, **_kwargs: Any) -> object:
    raise RuntimeError("trusted-local plugin operators dispatch through subprocesses")


def _trusted_local_subprocess_projection_proxy(*_args: Any, **_kwargs: Any) -> object:
    raise RuntimeError("trusted-local plugin projections dispatch through subprocesses")


def _validated_executable_runtime_bindings(
    loaded: LoadedPluginManifest,
    *,
    manifest_ref: dict[str, Any],
    install_source: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    plugin_id = loaded.manifest.id
    manifest = loaded.manifest
    specs = {
        "actions": (
            set(manifest.contributes.actions),
            manifest.runtime.actions,
        ),
        "importers": (
            set(manifest.contributes.importers),
            manifest.runtime.importers,
        ),
        "operators": (
            set(manifest.contributes.operators),
            manifest.runtime.operators,
        ),
        "projections": (
            set(manifest.contributes.projections),
            manifest.runtime.projections,
        ),
    }
    registry = default_registry()
    validated: dict[str, dict[str, Any]] = {
        "actions": {},
        "importers": {},
        "operators": {},
        "projections": {},
    }
    for binding_type, (declared, bindings) in specs.items():
        existing_specs = {
            spec.kind: spec for spec in registry.runtime_binding_specs(binding_type)
        }
        for binding in bindings:
            if not binding.handler_key.startswith(f"{plugin_id}:"):
                raise WorkbenchPluginActivationError(
                    "plugin_backend_activation_runtime_binding_invalid",
                    "runtime binding handler key must be namespaced by the plugin id",
                    status_code=409,
                )
            if binding.kind not in declared:
                raise WorkbenchPluginActivationError(
                    "plugin_backend_activation_runtime_binding_invalid",
                    "runtime binding must reference a declared contribution",
                    status_code=409,
                )
            if binding_type == "actions":
                _validate_public_action_collision(binding)
            existing = existing_specs.get(binding.kind)
            if existing is not None and existing.plugin != plugin_id:
                _raise_backend_contribution_conflict(binding_type[:-1], binding.kind)
            if binding_type == "actions" and binding.handler_api != "typed_action":
                raise WorkbenchPluginActivationError(
                    "plugin_backend_activation_runtime_binding_invalid",
                    "runtime action binding must use typed_action handler_api",
                    status_code=409,
                )
            if binding_type == "projections" and binding.handler_api not in {
                None,
                "plugin_projection",
            }:
                raise WorkbenchPluginActivationError(
                    "plugin_backend_activation_runtime_binding_invalid",
                    "runtime projection binding must use plugin_projection handler_api",
                    status_code=409,
                )
            if binding_type == "operators" and binding.handler_api not in {
                None,
                "plugin_operator",
            }:
                raise WorkbenchPluginActivationError(
                    "plugin_backend_activation_runtime_binding_invalid",
                    "runtime operator binding must use plugin_operator handler_api",
                    status_code=409,
                )
            if (
                binding_type not in {"actions", "importers", "operators", "projections"}
                and binding.handler_api
            ):
                raise WorkbenchPluginActivationError(
                    "plugin_backend_activation_runtime_binding_invalid",
                    "runtime binding handler_api is not valid for this binding type",
                    status_code=409,
                )
            if binding_type == "actions":
                metadata = _trusted_local_action_binding_metadata(
                    loaded,
                    binding,
                    manifest_ref=manifest_ref,
                    install_source=install_source,
                )
                validated[binding_type][binding.kind] = {
                    "handler": _trusted_local_native_action_proxy,
                    "handler_api": "plugin_typed_action_native",
                    "handler_key": binding.handler_key,
                    "metadata": metadata,
                }
                continue
            if binding_type == "importers" and binding.handler_api == "plugin_importer":
                metadata = _trusted_local_importer_binding_metadata(
                    loaded,
                    binding,
                    manifest_ref=manifest_ref,
                )
                validated[binding_type][binding.kind] = {
                    "handler": _trusted_local_subprocess_importer_proxy,
                    "handler_api": "plugin_importer_subprocess",
                    "handler_key": binding.handler_key,
                    "metadata": metadata,
                }
                continue
            if binding_type == "operators" and binding.handler_api == "plugin_operator":
                metadata = _trusted_local_operator_binding_metadata(
                    loaded,
                    binding,
                    manifest_ref=manifest_ref,
                )
                validated[binding_type][binding.kind] = {
                    "handler": _trusted_local_subprocess_operator_proxy,
                    "handler_api": "plugin_operator_subprocess",
                    "handler_key": binding.handler_key,
                    "metadata": metadata,
                }
                continue
            if (
                binding_type == "projections"
                and binding.handler_api == "plugin_projection"
            ):
                metadata = _trusted_local_projection_binding_metadata(
                    loaded,
                    binding,
                    manifest_ref=manifest_ref,
                )
                validated[binding_type][binding.kind] = {
                    "handler": _trusted_local_subprocess_projection_proxy,
                    "handler_api": "plugin_projection_subprocess",
                    "handler_key": binding.handler_key,
                    "metadata": metadata,
                }
                continue
            handler = get_trusted_backend_handler(binding.handler_key)
            if handler is None:
                raise WorkbenchPluginActivationError(
                    "plugin_backend_activation_runtime_handler_key_unknown",
                    "runtime binding references an unknown trusted handler key",
                    status_code=409,
                )
            validated[binding_type][binding.kind] = {
                "handler": handler,
                "handler_api": "envelope",
                "handler_key": binding.handler_key,
                "metadata": {},
            }
    return validated


def _validate_public_action_collision(binding: Any) -> None:
    action_kind = str(binding.kind)
    if action_kind != action_kind.strip() or "." not in action_kind:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_public_action_collision",
            "plugin action kinds must be canonical namespaced ids",
            status_code=409,
        )
    from frisket.actions.registry import ACTION_REGISTRY

    if action_kind in ACTION_REGISTRY.action_ids:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_public_action_collision",
            "plugin actions must not shadow builtin action IDs",
            status_code=409,
        )


def _trusted_local_action_binding_metadata(
    loaded: LoadedPluginManifest,
    binding: Any,
    *,
    manifest_ref: dict[str, Any],
    install_source: dict[str, Any],
) -> dict[str, Any]:
    executable = _trusted_local_executable_binding_metadata(
        loaded,
        binding,
        manifest_ref=manifest_ref,
        binding_name="runtime action",
    )
    from frisket.plugins.frontend_modules import typed_action_ui_bindings

    ui_binding = typed_action_ui_bindings(loaded.manifest).get(binding.kind)
    return {
        **executable,
        "handler_api": "typed_action",
        "catalog_entry": dict(binding.catalog_entry),
        **(
            {
                "action_ui": {
                    "plugin_id": loaded.manifest.id,
                    "export_name": ui_binding.component_key,
                    "package_sha256": executable["package_sha256"],
                }
            }
            if ui_binding is not None
            else {}
        ),
        "requires_secrets": [
            _normalize_plugin_env_name(name)
            for name in loaded.manifest.requires.secrets
        ],
    }


def _sanitized_runtime_binding_writes(
    writes: list[Any],
) -> list[dict[str, Any]]:
    # writes items are PluginManifestRuntimeBindingWrite; dump by alias so
    # the wire shape (`schema`, not the Python-side `schema_ref`) and
    # unset-optional-field omission match the pre-submodel raw-dict
    # behaviour byte-for-byte.
    return [
        item.model_dump(mode="json", by_alias=True, exclude_none=True)
        for item in writes
    ]


def _trusted_local_importer_binding_metadata(
    loaded: LoadedPluginManifest,
    binding: Any,
    *,
    manifest_ref: dict[str, Any],
) -> dict[str, Any]:
    metadata = _trusted_local_executable_binding_metadata(
        loaded,
        binding,
        manifest_ref=manifest_ref,
        binding_name="runtime importer",
    )
    metadata.update(
        {
            "handler_api": "plugin_importer",
            "title": binding.title or binding.kind,
            "description": binding.description or "",
            "writes": _sanitized_runtime_binding_writes(binding.writes),
            "params": [dict(item) for item in binding.params],
            "requires_secrets": [
                _normalize_plugin_env_name(name)
                for name in loaded.manifest.requires.secrets
            ],
        }
    )
    return metadata


def _trusted_local_projection_binding_metadata(
    loaded: LoadedPluginManifest,
    binding: Any,
    *,
    manifest_ref: dict[str, Any],
) -> dict[str, Any]:
    metadata = _trusted_local_executable_binding_metadata(
        loaded,
        binding,
        manifest_ref=manifest_ref,
        binding_name="runtime projection",
    )
    metadata.update(
        {
            "handler_api": "plugin_projection",
            "title": binding.title or binding.kind,
            "description": binding.description or "",
            "execution": _sanitized_projection_execution(binding.execution),
            "params": [dict(item) for item in binding.params],
            "inputs": [dict(item) for item in binding.inputs],
            "writes": _sanitized_runtime_binding_writes(binding.writes),
            "requires_secrets": [
                _normalize_plugin_env_name(name)
                for name in loaded.manifest.requires.secrets
            ],
        }
    )
    return metadata


def _trusted_local_operator_binding_metadata(
    loaded: LoadedPluginManifest,
    binding: Any,
    *,
    manifest_ref: dict[str, Any],
) -> dict[str, Any]:
    metadata = _trusted_local_executable_binding_metadata(
        loaded,
        binding,
        manifest_ref=manifest_ref,
        binding_name="runtime operator",
    )
    metadata.update(
        {
            "handler_api": "plugin_operator",
            "title": binding.title or binding.kind,
            "description": binding.description or "",
            "params": [dict(item) for item in binding.params],
            "requires_secrets": [
                _normalize_plugin_env_name(name)
                for name in loaded.manifest.requires.secrets
            ],
        }
    )
    return metadata


def _trusted_local_executable_binding_metadata(
    loaded: LoadedPluginManifest,
    binding: Any,
    *,
    manifest_ref: dict[str, Any],
    binding_name: str,
) -> dict[str, Any]:
    module_path = binding.module_path
    if module_path is None:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_runtime_entrypoint_missing",
            f"{binding_name} binding must declare a module_path",
            status_code=409,
        )
    path_value = manifest_ref.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_runtime_entrypoint_missing",
            "plugin.load manifest evidence is missing a local source path",
            status_code=409,
        )
    manifest_path = Path(path_value).resolve()
    plugin_root = manifest_path.parent
    entrypoint = (plugin_root / module_path).resolve()
    if not entrypoint.is_relative_to(plugin_root):
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_runtime_path_escape",
            f"{binding_name} module_path must stay inside the plugin package",
            status_code=400,
        )
    if entrypoint.suffix != ".py" or not entrypoint.is_file():
        raise WorkbenchPluginActivationError(
            "plugin_backend_activation_runtime_entrypoint_missing",
            f"{binding_name} module_path must point to a Python file",
            status_code=404,
        )
    module_identity = _assert_manifest_ref_module_integrity(
        manifest_ref,
        plugin_root=plugin_root,
        module_path=module_path,
    )
    return {
        "module_path": module_path,
        "plugin_root": str(plugin_root),
        "manifest_sha256": loaded.sha256,
        "package_identity": dict(manifest_ref.get("package_identity"))
        if isinstance(manifest_ref.get("package_identity"), dict)
        else None,
        "package_sha256": str(manifest_ref.get("package_sha256") or ""),
        "module_sha256": module_identity["sha256"],
        "module_byte_count": module_identity["byte_count"],
        "required_capabilities": _effective_required_capabilities(loaded.manifest),
    }


def _sanitized_projection_execution(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"mode": "artifact_timeline"}
    mode = str(value.get("mode") or "artifact_timeline")
    if mode not in {"artifact_timeline", "runtime_plan"}:
        mode = "artifact_timeline"
    out = {"mode": mode}
    role = value.get("role")
    if isinstance(role, str) and role.strip():
        out["role"] = role.strip()
    return out


def _register_executable_job_handlers(
    loaded: LoadedPluginManifest, handlers: dict[str, Any]
) -> dict[str, list[str]]:
    registry = default_registry()
    plugin_id = loaded.manifest.id
    for job_handler_id, handler in handlers.items():
        registry.register_job_handler(
            job_handler_id,
            handler,
            description=f"Executable handler bound by workbench plugin {plugin_id}.",
            plugin=plugin_id,
            replace=True,
        )
    return {"jobHandlers": sorted(handlers)}


def _register_executable_runtime_bindings(
    loaded: LoadedPluginManifest, bindings: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    registry = default_registry()
    plugin_id = loaded.manifest.id
    registered = _empty_runtime_bindings()
    for binding_type in ("actions", "importers", "operators", "projections"):
        for kind, binding in bindings[binding_type].items():
            registry.register_runtime_binding(
                binding_type,
                kind,
                handler_key=str(binding["handler_key"]),
                handler=binding["handler"],
                handler_api=str(binding["handler_api"]),
                metadata=dict(binding.get("metadata") or {}),
                plugin=plugin_id,
                replace=True,
            )
            registered[binding_type].append(kind)
    for key in registered:
        registered[key].sort()
    return registered


def _assert_backend_contribution_conflicts(
    *,
    plugin_id: str,
    column_type_ids: list[str],
    importer_ids: list[str],
    job_handler_ids: list[str],
) -> None:
    registry = default_registry()
    for type_id in column_type_ids:
        existing = column_types.get_column_type(type_id)
        if existing is not None and (existing.core or existing.plugin != plugin_id):
            _raise_backend_contribution_conflict("column_type", type_id)
    importer_specs = {spec.name: spec for spec in registry.importer_specs()}
    for importer_id in importer_ids:
        existing = importer_specs.get(importer_id)
        if existing is not None and existing.plugin != plugin_id:
            _raise_backend_contribution_conflict("importer", importer_id)
    job_handler_specs = {spec.kind: spec for spec in registry.job_handler_specs()}
    for job_handler_id in job_handler_ids:
        existing = job_handler_specs.get(job_handler_id)
        if existing is not None and existing.plugin != plugin_id:
            _raise_backend_contribution_conflict("job_handler", job_handler_id)


def _raise_backend_contribution_conflict(kind: str, contribution_id: str) -> None:
    raise WorkbenchPluginActivationError(
        "plugin_backend_activation_conflict",
        f"plugin backend contribution conflicts with existing {kind}: {contribution_id}",
        status_code=409,
    )

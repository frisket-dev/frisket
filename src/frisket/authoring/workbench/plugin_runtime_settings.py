"""Workbench plugin package settings and environment mutation."""

from __future__ import annotations

import json
from typing import Any

from frisket.authoring.workbench.plugin_runtime_shared import (
    PLUGIN_ENV_INSTALL_STATES,
    PLUGIN_ENV_NAME_RE,
    PLUGIN_ENV_SCHEMA_VERSION,
    PLUGIN_ENV_VAR_SCHEMA_VERSION,
    WorkbenchPluginLifecycleError,
)
from frisket.authoring.workbench.plugin_package_catalog import (
    plugin_package_catalog_for_project,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    _catalog_identity_refs,
    _loaded_manifest_from_ref,
    _workbench_plugin_install_state,
    project_accepts_current_plugin_capabilities,
    workspace_permits_plugin,
)
from frisket.contracts.plugin import LoadedPluginManifest
from frisket.engine.store import Project
from frisket.team.security.secrets import encrypt_secret, key_hint


def list_workbench_plugin_env_vars(
    project: Project, *, project_id: str, plugin_id: str
) -> dict[str, Any]:
    loaded = _loaded_manifest_for_plugin_env(project, plugin_id=plugin_id)
    required_names = _plugin_required_env_names(loaded)
    rows = _workbench_plugin_env_var_rows(project, plugin_id=plugin_id)
    names = sorted(required_names | set(rows))
    return {
        "schemaVersion": PLUGIN_ENV_SCHEMA_VERSION,
        "projectId": project_id,
        "pluginId": plugin_id,
        "env": [
            _plugin_env_var_metadata(
                project_id=project_id,
                plugin_id=plugin_id,
                name=name,
                row=rows.get(name),
                required=name in required_names,
            )
            for name in names
        ],
    }


def set_workbench_plugin_env_var(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    name: str,
    value: str,
) -> dict[str, Any]:
    from frisket.authoring.workbench.plugin_subprocess import (
        is_reserved_core_secret_name,
    )

    loaded = _loaded_manifest_for_plugin_env(project, plugin_id=plugin_id)
    normalized_name = _normalize_plugin_env_name(name)
    # A plugin must not set (and thereby, previously, overwrite the
    # global copy of) a core-owned provider credential. Refuse reserved names
    # outright rather than silently scoping them — the plugin should not present
    # a config surface for a secret it cannot own.
    if is_reserved_core_secret_name(normalized_name):
        raise WorkbenchPluginLifecycleError(
            "plugin_env_name_reserved",
            "this env var name is reserved for the project/organization "
            "provider-key layer and cannot be set through a plugin",
            status_code=400,
        )
    if value == "":
        raise WorkbenchPluginLifecycleError(
            "plugin_env_value_required",
            "plugin env var value is required",
            status_code=400,
        )
    encrypted_value = encrypt_secret(value)
    hint = key_hint(value)
    try:
        # Plugin secrets live ONLY in the plugin-scoped table (keyed by
        # plugin_id). The former dual-write into the global project_secrets
        # namespace is removed: it disclosed values across plugins/core and let
        # a plugin poison a global credential. No backfill of stale
        # dual-written rows is required — no shipped plugin declares a secret
        # (all bundled manifests carry "secrets": []), so no production
        # project_secrets rows were ever mirror-written by this path.
        project.db.execute(
            "INSERT INTO workbench_plugin_env_vars "
            "(plugin_id, name, encrypted, hint, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(plugin_id, name) DO UPDATE SET "
            "encrypted=excluded.encrypted, "
            "hint=excluded.hint, "
            "updated_at=excluded.updated_at",
            (
                plugin_id,
                normalized_name,
                encrypted_value,
                hint,
            ),
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    row = _workbench_plugin_env_var_rows(project, plugin_id=plugin_id)[normalized_name]
    return _plugin_env_var_metadata(
        project_id=project_id,
        plugin_id=plugin_id,
        name=normalized_name,
        row=row,
        required=normalized_name in _plugin_required_env_names(loaded),
    )


def delete_workbench_plugin_env_var(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    name: str,
) -> dict[str, Any]:
    _loaded_manifest_for_plugin_env(project, plugin_id=plugin_id)
    normalized_name = _normalize_plugin_env_name(name)
    try:
        # Plugin secrets are plugin-scoped only; deleting one never touches the
        # global project_secrets namespace (the former cascade delete could
        # remove a core-owned credential).
        cursor = project.db.execute(
            "DELETE FROM workbench_plugin_env_vars WHERE plugin_id=? AND name=?",
            (plugin_id, normalized_name),
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    return {
        "ok": True,
        "deleted": cursor.rowcount > 0,
        "projectId": project_id,
        "pluginId": plugin_id,
        "name": normalized_name,
    }


def _secret_like_setting_value(setting_id: str, value: Any) -> bool:
    haystack = f"{setting_id} {value if isinstance(value, str) else ''}".lower()
    return any(
        marker in haystack
        for marker in (
            "secret",
            "token",
            "password",
            "api_key",
            "apikey",
            "private_key",
        )
    )


def _plugin_setting_rows(project: Project, *, plugin_id: str) -> dict[str, Any]:
    rows = project.db.execute(
        "SELECT setting_id, value_json FROM workbench_plugin_settings WHERE plugin_id=?",
        (plugin_id,),
    ).fetchall()
    values: dict[str, Any] = {}
    for row in rows:
        try:
            values[str(row["setting_id"])] = json.loads(row["value_json"])
        except (TypeError, ValueError):
            values[str(row["setting_id"])] = None
    return values


def _validate_plugin_setting_value(setting: Any, value: Any) -> Any:
    setting_type = str(setting.type)
    if _secret_like_setting_value(setting.id, value):
        raise WorkbenchPluginLifecycleError(
            "invalid_plugin_settings",
            "plugin settings cannot store secret-looking values",
            status_code=422,
            details={"fields": [{"id": setting.id, "message": "secret-looking value"}]},
        )
    if setting_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if setting_type == "string":
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value
    if setting_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("must be a number")
        number = float(value)
        if setting.min is not None and number < setting.min:
            raise ValueError(f"must be >= {setting.min}")
        if setting.max is not None and number > setting.max:
            raise ValueError(f"must be <= {setting.max}")
        return number
    if setting_type == "enum":
        if value not in (setting.enum or []):
            raise ValueError("must be one of the declared options")
        return value
    raise ValueError("unsupported setting type")


def _plugin_setting_payload(
    setting: Any, stored_values: dict[str, Any]
) -> dict[str, Any]:
    has_project_value = setting.id in stored_values
    effective = stored_values.get(setting.id, setting.default)
    return {
        "id": setting.id,
        "title": setting.title,
        "type": setting.type,
        "description": setting.description,
        "defaultValue": setting.default,
        "effectiveValue": effective,
        "source": "project" if has_project_value else "default",
        "enum": setting.enum,
        "min": setting.min,
        "max": setting.max,
        "readOnly": False,
    }


def list_workbench_plugin_settings(
    project: Project, *, project_id: str, plugin_id: str
) -> dict[str, Any]:
    loaded = _loaded_manifest_for_plugin_env(project, plugin_id=plugin_id)
    return _workbench_plugin_settings_response(
        project,
        project_id=project_id,
        plugin_id=plugin_id,
        loaded=loaded,
    )


def _workbench_plugin_settings_response(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    loaded: LoadedPluginManifest,
) -> dict[str, Any]:
    stored_values = _plugin_setting_rows(project, plugin_id=plugin_id)
    return {
        "schemaVersion": "frisket.workbench_plugin_settings.v1",
        "projectId": project_id,
        "pluginId": plugin_id,
        "canMutate": True,
        "settings": [
            _plugin_setting_payload(setting, stored_values)
            for setting in loaded.manifest.settings
        ],
    }


def patch_workbench_plugin_settings(
    project: Project,
    *,
    project_id: str,
    plugin_id: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    loaded = _loaded_manifest_for_plugin_env(project, plugin_id=plugin_id)
    settings = {setting.id: setting for setting in loaded.manifest.settings}
    field_errors: list[dict[str, str]] = []
    normalized: dict[str, Any] = {}
    for setting_id, value in values.items():
        setting = settings.get(setting_id)
        if setting is None:
            field_errors.append({"id": setting_id, "message": "unknown setting"})
            continue
        try:
            normalized[setting_id] = _validate_plugin_setting_value(setting, value)
        except ValueError as exc:
            field_errors.append({"id": setting_id, "message": str(exc)})
        except WorkbenchPluginLifecycleError as exc:
            raise exc
    if field_errors:
        raise WorkbenchPluginLifecycleError(
            "invalid_plugin_settings",
            "plugin settings did not validate",
            status_code=422,
            details={"fields": field_errors},
        )
    try:
        for setting_id, value in normalized.items():
            project.db.execute(
                "INSERT INTO workbench_plugin_settings "
                "(plugin_id, setting_id, value_json, updated_at) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(plugin_id, setting_id) DO UPDATE SET "
                "value_json=excluded.value_json, updated_at=excluded.updated_at",
                (plugin_id, setting_id, json.dumps(value, sort_keys=True)),
            )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    # Reuse the manifest already admitted above. Calling the public list helper
    # here would re-run the composition check (and re-hash a reviewed bundle)
    # after this request has already crossed that boundary once.
    return _workbench_plugin_settings_response(
        project,
        project_id=project_id,
        plugin_id=plugin_id,
        loaded=loaded,
    )


def _loaded_manifest_for_plugin_env(
    project: Project, *, plugin_id: str
) -> LoadedPluginManifest:
    # ENABLEMENT gate stays project-scoped: this project must have the plugin
    # installed/enabled to read or set its env/settings. IDENTITY
    # (the manifest evidence + digests) comes from the workspace catalog.
    install_state = _workbench_plugin_install_state(project, plugin_id=plugin_id)
    if install_state is None or install_state.get("install_state") not in (
        PLUGIN_ENV_INSTALL_STATES
    ):
        raise WorkbenchPluginLifecycleError(
            "plugin_env_plugin_not_installed",
            "project plugin env vars require an installed workbench plugin",
            status_code=404,
        )
    catalog = plugin_package_catalog_for_project(project)
    entry = catalog.get(plugin_id)
    manifest_ref = catalog.manifest_ref(plugin_id)
    if entry is None or manifest_ref is None:
        raise WorkbenchPluginLifecycleError(
            "plugin_env_manifest_mismatch",
            "workspace catalog has no package identity for this plugin",
            status_code=409,
        )
    if not workspace_permits_plugin(project, plugin_id, catalog_entry=entry):
        raise WorkbenchPluginLifecycleError(
            "plugin_not_available_under_composition_policy",
            "this workbench plugin is not available under the active composition policy",
            status_code=404,
        )
    _receipt_id, manifest_sha256, package_sha256 = _catalog_identity_refs(entry)
    if manifest_ref.get("plugin_id") != plugin_id:
        raise WorkbenchPluginLifecycleError(
            "plugin_env_manifest_mismatch",
            "workspace catalog package does not match the requested plugin id",
            status_code=409,
        )
    loaded = _loaded_manifest_from_ref(manifest_ref)
    if not project_accepts_current_plugin_capabilities(install_state, loaded.manifest):
        raise WorkbenchPluginLifecycleError(
            "plugin_env_permissions_missing",
            "plugin env and settings require acceptance of current manifest capabilities",
            status_code=409,
        )
    if loaded.sha256 != manifest_sha256:
        raise WorkbenchPluginLifecycleError(
            "plugin_env_manifest_mismatch",
            "workspace catalog manifest sha does not match its manifest evidence",
            status_code=409,
        )
    if str(manifest_ref.get("package_sha256") or "") != package_sha256:
        raise WorkbenchPluginLifecycleError(
            "plugin_env_package_mismatch",
            "workspace catalog package sha does not match its manifest evidence",
            status_code=409,
        )
    return loaded


def _plugin_required_env_names(loaded: LoadedPluginManifest) -> set[str]:
    return {
        _normalize_plugin_env_name(name) for name in loaded.manifest.requires.secrets
    }


def _normalize_plugin_env_name(name: str) -> str:
    clean = name.strip().upper()
    if not PLUGIN_ENV_NAME_RE.fullmatch(clean):
        raise WorkbenchPluginLifecycleError(
            "plugin_env_name_invalid",
            "env var names must match [A-Z_][A-Z0-9_]* and be at most 64 chars",
            status_code=400,
        )
    return clean


def _workbench_plugin_env_var_rows(
    project: Project, *, plugin_id: str
) -> dict[str, dict[str, Any]]:
    rows = project.db.execute(
        "SELECT plugin_id, name, encrypted, hint, updated_at "
        "FROM workbench_plugin_env_vars WHERE plugin_id=? ORDER BY name",
        (plugin_id,),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        result[str(data["name"])] = data
    return result


def _plugin_env_var_metadata(
    *,
    project_id: str,
    plugin_id: str,
    name: str,
    row: dict[str, Any] | None,
    required: bool,
) -> dict[str, Any]:
    configured = row is not None
    return {
        "schemaVersion": PLUGIN_ENV_VAR_SCHEMA_VERSION,
        "projectId": project_id,
        "pluginId": plugin_id,
        "name": name,
        "hint": str(row.get("hint") or "") if row is not None else None,
        "required": required,
        "configured": configured,
        "updatedAt": str(row.get("updated_at") or "") if row is not None else None,
    }

"""Shared environment and process coordinator for plugin subprocesses."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from frisket.team.security.secrets import decrypt_secret
from frisket.engine.sandbox.shim import (
    run_sandboxed,
    run_sandboxed_stdout_lines,
)
from frisket.engine.store import Project
from frisket.plugins.process_client import (
    PluginProcessClient,
    run_sandboxed_sync,
)

_DEFAULT_RUN_SANDBOXED = run_sandboxed


_DEFAULT_RUN_SANDBOXED_STDOUT_LINES = run_sandboxed_stdout_lines


_PROJECT_SECRET_FALLBACKS: dict[str, Callable[[str, str], str | None]] = {}


RESERVED_CORE_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    }
)


def is_reserved_core_secret_name(name: str) -> bool:
    """True when ``name`` is a core-owned provider credential a plugin must not
    touch through its plugin-scoped secret path."""
    return name.strip().upper() in RESERVED_CORE_SECRET_NAMES


def register_project_secret_fallback(
    workspace_root: str | Path,
    resolver: Callable[[str, str], str | None] | None,
) -> None:
    key = str(Path(workspace_root).resolve())
    if resolver is None:
        _PROJECT_SECRET_FALLBACKS.pop(key, None)
        return
    _PROJECT_SECRET_FALLBACKS[key] = resolver


def project_secret_fallback_plaintext(project: Project, name: str) -> str | None:
    resolver = _PROJECT_SECRET_FALLBACKS.get(str(project.path.parent.resolve()))
    if resolver is None:
        return None
    value = resolver(project.path.stem, name)
    return value if value else None


def _plugin_scoped_env_encrypted(project: Project, *, plugin_id: str) -> dict[str, str]:
    """Encrypted env values owned by exactly this plugin (finding #6 scoping).

    Reads the plugin-scoped ``workbench_plugin_env_vars`` table keyed by
    ``plugin_id`` — never the global ``project_secrets`` namespace — so a plugin
    resolves only credentials configured for it, and two plugins declaring the
    same env name receive isolated values."""
    rows = project.db.execute(
        "SELECT name, encrypted FROM workbench_plugin_env_vars WHERE plugin_id=?",
        (plugin_id,),
    ).fetchall()
    return {str(row["name"]): str(row["encrypted"]) for row in rows}


def _project_plugin_secrets(
    project: Project,
    *,
    plugin_id: str,
    metadata: dict[str, Any],
) -> dict[str, str] | dict[str, Any]:
    required = _required_plugin_env_names(metadata)
    if not required:
        return {}
    missing = _missing_project_plugin_secrets(
        project,
        plugin_id=plugin_id,
        metadata=metadata,
    )
    if missing:
        return {
            "_error": _failed(
                "plugin_env_missing",
                "Configure required plugin env vars before running this action",
                field="plugin_env",
                details={"missing": missing},
            )
        }
    encrypted = _plugin_scoped_env_encrypted(project, plugin_id=plugin_id)
    env: dict[str, str] = {}
    for name in required:
        # Reserved core credential names are never resolvable through the plugin
        # path (they are reported missing above and refused on write).
        if is_reserved_core_secret_name(name):
            continue
        if name in encrypted:
            env[name] = decrypt_secret(encrypted[name])
            continue
        fallback = project_secret_fallback_plaintext(project, name)
        if fallback is not None:
            env[name] = fallback
    return env


def _required_plugin_env_names(metadata: dict[str, Any]) -> list[str]:
    return [
        str(item)
        for item in metadata.get("requires_secrets", [])
        if isinstance(item, str) and item
    ]


def _missing_project_plugin_secrets(
    project: Project,
    *,
    plugin_id: str,
    metadata: dict[str, Any],
) -> list[str]:
    required = _required_plugin_env_names(metadata)
    if not required:
        return []
    configured = set(_plugin_scoped_env_encrypted(project, plugin_id=plugin_id))
    missing: list[str] = []
    for name in required:
        # A plugin can never satisfy a reserved core credential name — it is
        # fail-closed missing rather than resolved from core/org.
        if is_reserved_core_secret_name(name):
            missing.append(name)
            continue
        if name in configured:
            continue
        if project_secret_fallback_plaintext(project, name):
            continue
        missing.append(name)
    return missing


def _run_sandboxed_sync(
    argv: list[str],
    *,
    policy: Any,
    stdin_data: bytes | None = None,
    stdin_file: Any | None = None,
    extra_env: dict[str, str],
    scratch_dir: str | Path | None = None,
) -> Any:
    return run_sandboxed_sync(
        run_sandboxed,
        argv,
        policy=policy,
        stdin_data=stdin_data,
        stdin_file=stdin_file,
        extra_env=extra_env,
        scratch_dir=scratch_dir,
    )


def _plugin_process_client(plugin_root: str) -> PluginProcessClient:
    return PluginProcessClient(
        plugin_root,
        run_sandboxed_call=run_sandboxed,
        run_sandboxed_stdout_lines_call=_run_plugin_stdout_lines,
    )


async def _run_plugin_stdout_lines(
    argv: list[str],
    *,
    on_stdout_line: Callable[[str], None],
    policy: Any,
    stdin_data: bytes | None = None,
    extra_env: dict[str, str] | None = None,
    scratch_dir: str | Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Any:
    """Preserve the historical buffered-runner test seam during cutover."""

    if run_sandboxed_stdout_lines is not _DEFAULT_RUN_SANDBOXED_STDOUT_LINES:
        return await run_sandboxed_stdout_lines(
            argv,
            on_stdout_line=on_stdout_line,
            policy=policy,
            stdin_data=stdin_data,
            extra_env=extra_env,
            scratch_dir=scratch_dir,
            should_cancel=should_cancel,
        )
    if run_sandboxed is not _DEFAULT_RUN_SANDBOXED:
        result = await run_sandboxed(
            argv,
            policy=policy,
            stdin_data=stdin_data,
            extra_env=extra_env,
            scratch_dir=scratch_dir,
            should_cancel=should_cancel,
        )
        for line in result.stdout.splitlines():
            on_stdout_line(line)
        return result
    return await run_sandboxed_stdout_lines(
        argv,
        on_stdout_line=on_stdout_line,
        policy=policy,
        stdin_data=stdin_data,
        extra_env=extra_env,
        scratch_dir=scratch_dir,
        should_cancel=should_cancel,
    )


def _failed(
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if field:
        error["field"] = field
    if details:
        error["details"] = details
    return {"status": "failed", "outputs": [], "errors": [error], "warnings": []}

"""Persisted local-instance runtime settings.

The environment seeds a fresh workspace, but once a user deliberately changes
AI call mode in Preferences the workspace-owned value wins on later starts.
This is intentionally separate from ``provider_keys.json``: cache mode is not
a secret, and a malformed runtime settings file must never make a later secret
write lossy.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from filelock import FileLock

from frisket.ai.llm import CACHE_MODES, CacheMode, resolve_env_cache_mode
from frisket.local_runtime_config import (
    DEFAULT_COST_PREAPPROVAL_USD,
    InvalidLocalRuntimeConfigError,
    normalize_cost_preapproval_usd,
    resolve_local_cost_preapproval_usd,
)


class InvalidRuntimeSettingsError(ValueError):
    """A persisted runtime settings file cannot be read without data loss."""


def runtime_settings_path(root: str | Path) -> Path:
    return Path(root) / ".frisket" / "runtime_settings.json"


def _read_valid_settings(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise InvalidRuntimeSettingsError(
            f"cannot read runtime settings at {path}"
        ) from exc
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidRuntimeSettingsError(
            f"runtime settings at {path} are not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise InvalidRuntimeSettingsError(
            f"runtime settings at {path} must be a JSON object"
        )
    unknown = sorted(set(value) - {"cache_mode", "cost_preapproval_usd"})
    if unknown:
        raise InvalidRuntimeSettingsError(
            f"runtime settings at {path} contain unsupported fields: "
            f"{', '.join(unknown)}"
        )
    mode = value.get("cache_mode")
    if mode is not None and mode not in CACHE_MODES:
        raise InvalidRuntimeSettingsError(
            f"runtime settings at {path} contain invalid cache_mode {mode!r}"
        )
    threshold = value.get("cost_preapproval_usd")
    if threshold is not None:
        _validated_cost_preapproval_usd(threshold)
    return value


def _validated_cost_preapproval_usd(value: object) -> str:
    """Return a canonical, non-negative USD amount safe for the identity wire."""

    try:
        return normalize_cost_preapproval_usd(value)
    except InvalidLocalRuntimeConfigError as exc:
        raise InvalidRuntimeSettingsError(f"runtime settings {exc}") from exc


def resolve_workspace_cache_mode(
    root: str | Path,
    env: Mapping[str, str] | None = None,
) -> CacheMode:
    """Return persisted preference, else the validated environment/default."""

    stored = _read_valid_settings(runtime_settings_path(root)).get("cache_mode")
    if stored is not None:
        return stored
    return resolve_env_cache_mode(env)


def resolve_workspace_cost_preapproval_usd(root: str | Path) -> str:
    """Return this local installation's pre-approval amount, defaulting to $2."""

    # Keep this server-owned settings document strict about unrelated fields;
    # the shared reader below is intentionally narrower for execution paths.
    _read_valid_settings(runtime_settings_path(root))
    try:
        return resolve_local_cost_preapproval_usd(root)
    except InvalidLocalRuntimeConfigError as exc:
        raise InvalidRuntimeSettingsError(str(exc)) from exc


def save_workspace_cache_mode(root: str | Path, mode: CacheMode) -> None:
    if mode not in CACHE_MODES:
        raise ValueError(
            f"invalid cache mode {mode!r} (expected one of: {', '.join(CACHE_MODES)})"
        )
    path = runtime_settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitignore = path.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    lock_path = path.with_name(f".{path.name}.lock")
    with FileLock(str(lock_path), timeout=-1):
        current = _read_valid_settings(path)
        current["cache_mode"] = mode
        fd, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(current, indent=2))
                handle.write("\n")
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def save_workspace_cost_preapproval_usd(root: str | Path, amount: str) -> str:
    """Persist a local installation's threshold without overwriting other settings."""

    normalized = _validated_cost_preapproval_usd(amount)
    path = runtime_settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitignore = path.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    lock_path = path.with_name(f".{path.name}.lock")
    with FileLock(str(lock_path), timeout=-1):
        current = _read_valid_settings(path)
        current["cost_preapproval_usd"] = normalized
        fd, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(current, indent=2))
                handle.write("\n")
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    return normalized


__all__ = [
    "InvalidRuntimeSettingsError",
    "DEFAULT_COST_PREAPPROVAL_USD",
    "resolve_workspace_cache_mode",
    "resolve_workspace_cost_preapproval_usd",
    "runtime_settings_path",
    "save_workspace_cache_mode",
    "save_workspace_cost_preapproval_usd",
]

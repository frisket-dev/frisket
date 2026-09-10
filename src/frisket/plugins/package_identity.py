"""Trusted-local plugin package identity helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.contracts.plugin import LoadedPluginManifest


PLUGIN_PACKAGE_IDENTITY_SCHEMA_VERSION = "frisket.plugin_package_identity.v1"
_IGNORED_DIR_NAMES = {
    ".cache",
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    ".vscode",
    "__pycache__",
    "coverage",
    "env",
    "node_modules",
    "venv",
}
_IGNORED_FILE_NAMES = {
    ".DS_Store",
    ".coverage",
    ".env",
    ".env.local",
}
_IGNORED_SUFFIXES = {
    ".log",
    ".pyc",
    ".pyo",
}


@dataclass(frozen=True)
class PluginPackageIdentityError(ValueError):
    code: str
    message: str
    path: str | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class PluginPackageFileIdentity:
    path: str
    roles: tuple[str, ...]
    sha256: str
    byte_count: int

    def to_ref(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "roles": list(self.roles),
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True)
class PluginPackageIdentity:
    schema_version: str
    package_sha256: str
    files: tuple[PluginPackageFileIdentity, ...]

    def to_ref(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "package_sha256": self.package_sha256,
            "files": [item.to_ref() for item in self.files],
        }


def build_plugin_package_identity(
    loaded: LoadedPluginManifest,
    manifest_path: Path,
    *,
    descriptor_path: Path | None = None,
) -> PluginPackageIdentity:
    root = manifest_path.resolve().parent
    file_roles: dict[str, set[str]] = {}
    _add_all_package_files(file_roles, root=root)
    _add_package_file(file_roles, root=root, path=manifest_path, role="manifest")
    if descriptor_path is not None and descriptor_path.is_file():
        _add_package_file(
            file_roles,
            root=root,
            path=descriptor_path,
            role="workbench_descriptor_package",
        )
    for binding in (
        *loaded.manifest.runtime.actions,
        *loaded.manifest.runtime.importers,
        *loaded.manifest.runtime.operators,
        *loaded.manifest.runtime.projections,
        *loaded.manifest.runtime.job_handlers,
    ):
        module_path = getattr(binding, "module_path", None)
        if module_path is None:
            continue
        _add_package_file(
            file_roles,
            root=root,
            path=root / module_path,
            role=f"backend:{getattr(binding, 'kind', '')}",
        )
    for binding in loaded.manifest.runtime.workbench_components:
        if binding.module_path is None:
            continue
        _add_package_file(
            file_roles,
            root=root,
            path=root / binding.module_path,
            role=f"frontend:{binding.contribution_id}",
        )

    files = tuple(
        _file_identity(root=root, relative_path=relative_path, roles=roles)
        for relative_path, roles in sorted(file_roles.items())
    )
    payload = {
        "schema_version": PLUGIN_PACKAGE_IDENTITY_SCHEMA_VERSION,
        "files": [item.to_ref() for item in files],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return PluginPackageIdentity(
        schema_version=PLUGIN_PACKAGE_IDENTITY_SCHEMA_VERSION,
        package_sha256="sha256:" + hashlib.sha256(encoded).hexdigest(),
        files=files,
    )


def live_plugin_package_identity_from_ref(
    root: Path,
    package_ref: dict[str, Any] | None,
) -> PluginPackageIdentity:
    """Recompute package identity with the original role annotations preserved."""
    root = root.resolve()
    file_roles: dict[str, set[str]] = {}
    _add_all_package_files(file_roles, root=root)
    if isinstance(package_ref, dict):
        raw_files = package_ref.get("files")
        if isinstance(raw_files, list):
            for item in raw_files:
                if not isinstance(item, dict):
                    continue
                path = item.get("path")
                roles = item.get("roles")
                if not isinstance(path, str) or not isinstance(roles, list):
                    continue
                role_set = file_roles.setdefault(path, set())
                for role in roles:
                    if isinstance(role, str) and role != "package_file":
                        role_set.add(role)
    files = tuple(
        _file_identity(root=root, relative_path=relative_path, roles=roles)
        for relative_path, roles in sorted(file_roles.items())
    )
    payload = {
        "schema_version": PLUGIN_PACKAGE_IDENTITY_SCHEMA_VERSION,
        "files": [item.to_ref() for item in files],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return PluginPackageIdentity(
        schema_version=PLUGIN_PACKAGE_IDENTITY_SCHEMA_VERSION,
        package_sha256="sha256:" + hashlib.sha256(encoded).hexdigest(),
        files=files,
    )


def live_package_file_identity(
    root: Path, relative_path: str
) -> PluginPackageFileIdentity:
    return _file_identity(root=root.resolve(), relative_path=relative_path, roles=())


def package_file_identity_from_ref(
    package_ref: dict[str, Any] | None,
    relative_path: str,
) -> PluginPackageFileIdentity | None:
    if not isinstance(package_ref, dict):
        return None
    files = package_ref.get("files")
    if not isinstance(files, list):
        return None
    for item in files:
        if not isinstance(item, dict) or item.get("path") != relative_path:
            continue
        sha256 = item.get("sha256")
        byte_count = item.get("byte_count")
        roles_raw = item.get("roles")
        if (
            not isinstance(sha256, str)
            or not sha256.startswith("sha256:")
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or not isinstance(roles_raw, list)
        ):
            return None
        roles = tuple(str(role) for role in roles_raw if isinstance(role, str))
        return PluginPackageFileIdentity(
            path=relative_path,
            roles=roles,
            sha256=sha256,
            byte_count=byte_count,
        )
    return None


def _add_all_package_files(file_roles: dict[str, set[str]], *, root: Path) -> None:
    root = root.resolve()
    if not root.is_dir():
        raise PluginPackageIdentityError(
            "invalid_plugin_package_source",
            "plugin package root must be a directory",
            path=str(root),
        )
    for path in sorted(root.rglob("*")):
        if _is_ignored_package_path(path, root=root):
            continue
        if path.is_file():
            _add_package_file(file_roles, root=root, path=path, role="package_file")


def _is_ignored_package_path(path: Path, *, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    parts = relative.parts
    if any(part in _IGNORED_DIR_NAMES for part in parts[:-1]):
        return True
    name = path.name
    return name in _IGNORED_FILE_NAMES or path.suffix in _IGNORED_SUFFIXES


def _add_package_file(
    file_roles: dict[str, set[str]],
    *,
    root: Path,
    path: Path,
    role: str,
) -> None:
    resolved = path.resolve()
    root = root.resolve()
    if not resolved.is_relative_to(root):
        raise PluginPackageIdentityError(
            "invalid_plugin_package_source",
            "plugin package file path must stay inside the plugin package",
            path=str(path),
        )
    try:
        relative_path = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise PluginPackageIdentityError(
            "invalid_plugin_package_source",
            "plugin package file path must stay inside the plugin package",
            path=str(path),
        ) from exc
    roles = file_roles.setdefault(relative_path, set())
    roles.add(role)


def _file_identity(
    *,
    root: Path,
    relative_path: str,
    roles: set[str] | tuple[str, ...],
) -> PluginPackageFileIdentity:
    path = (root / relative_path).resolve()
    root = root.resolve()
    if not path.is_relative_to(root):
        raise PluginPackageIdentityError(
            "invalid_plugin_package_source",
            "plugin package file path must stay inside the plugin package",
            path=relative_path,
        )
    if not path.is_file():
        raise PluginPackageIdentityError(
            "invalid_plugin_package_source",
            "plugin package file path must point to a regular file",
            path=relative_path,
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PluginPackageIdentityError(
            "invalid_plugin_package_source",
            str(exc),
            path=relative_path,
        ) from exc
    return PluginPackageFileIdentity(
        path=relative_path,
        roles=tuple(sorted(roles)),
        sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
    )

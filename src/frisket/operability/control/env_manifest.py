"""Read the Frisket environment/secret classification manifest."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MANIFEST = ROOT / "docs" / "env-secrets.json"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_CLASSES = {
    "app-runtime",
    "app-runtime-optional",
    "github-actions",
    "infra-only",
    "legacy",
    "local-only",
    "local-provider",
    "prod-local",
    "runtime-provider",
}
ALLOWED_SURFACES = {"app-runtime", "github-actions"}
ALLOWED_ENVIRONMENTS = {"ci", "full-e2e", "litestream", "local", "prod", "sidecar"}
ALLOWED_RESTART_TARGETS = {
    "app",
    "litestream",
    "models",
    "postgres-backup",
    "project-deletion-purger",
    "worker",
}


class ManifestError(ValueError):
    pass


def _expect_bool(value: Any, field: str, name: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{name}.{field} must be boolean")
    return value


def _expect_string_list(value: Any, field: str, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError(f"{name}.{field} must be a list of strings")
    return value


def _validate_allowed(
    values: list[str], allowed: set[str], field: str, name: str
) -> None:
    invalid = sorted(set(values) - allowed)
    if invalid:
        if field == "class":
            raise ManifestError(f"{name} has invalid class: {', '.join(invalid)}")
        if field == "restart":
            raise ManifestError(
                f"{name} has invalid restart target(s): {', '.join(invalid)}"
            )
        raise ManifestError(f"{name} has invalid {field}: {', '.join(invalid)}")


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest JSON is invalid: {exc}") from exc

    if manifest.get("version") != 1:
        raise ManifestError("manifest.version must be 1")
    variables = manifest.get("variables")
    if not isinstance(variables, list):
        raise ManifestError("manifest.variables must be a list")
    seen: set[str] = set()
    for raw in variables:
        if not isinstance(raw, dict):
            raise ManifestError("manifest variables must be objects")
        name = raw.get("name")
        if not isinstance(name, str) or not ENV_NAME_RE.match(name):
            raise ManifestError(f"invalid variable name: {name!r}")
        if name in seen:
            raise ManifestError(f"duplicate variable name: {name}")
        seen.add(name)
        cls = raw.get("class")
        if not isinstance(cls, str) or not cls:
            raise ManifestError(f"{name}.class must be a non-empty string")
        _validate_allowed([cls], ALLOWED_CLASSES, "class", name)
        surfaces = _expect_string_list(raw.get("surfaces"), "surfaces", name)
        _validate_allowed(surfaces, ALLOWED_SURFACES, "surface", name)
        environments = _expect_string_list(
            raw.get("required_environments"), "required_environments", name
        )
        _validate_allowed(environments, ALLOWED_ENVIRONMENTS, "environment", name)
        _expect_bool(raw.get("remote_required"), "remote_required", name)
        _expect_bool(raw.get("supports_empty"), "supports_empty", name)
        consumers = _expect_string_list(raw.get("consumers"), "consumers", name)
        if not consumers:
            raise ManifestError(f"{name}.consumers must not be empty")
        restart = _expect_string_list(raw.get("restart"), "restart", name)
        _validate_allowed(restart, ALLOWED_RESTART_TARGETS, "restart", name)

    prefix_rules = manifest.get("prefix_rules", [])
    if not isinstance(prefix_rules, list):
        raise ManifestError("manifest.prefix_rules must be a list")
    for raw in prefix_rules:
        if not isinstance(raw, dict):
            raise ManifestError("prefix rules must be objects")
        prefix = raw.get("prefix")
        if not isinstance(prefix, str) or not prefix:
            raise ManifestError(f"invalid prefix rule: {prefix!r}")
        cls = raw.get("class")
        if not isinstance(cls, str) or not cls:
            raise ManifestError(f"{prefix}.class must be a non-empty string")
        _validate_allowed([cls], ALLOWED_CLASSES, "class", prefix)
        surfaces = _expect_string_list(raw.get("surfaces"), "surfaces", prefix)
        _validate_allowed(surfaces, ALLOWED_SURFACES, "surface", prefix)
        _expect_bool(raw.get("remote_required"), "remote_required", prefix)
        _expect_bool(raw.get("supports_empty"), "supports_empty", prefix)
        consumers = _expect_string_list(raw.get("consumers"), "consumers", prefix)
        if not consumers:
            raise ManifestError(f"{prefix}.consumers must not be empty")
        restart = _expect_string_list(raw.get("restart"), "restart", prefix)
        _validate_allowed(restart, ALLOWED_RESTART_TARGETS, "restart", prefix)

    return manifest


def entries_by_name(
    manifest: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    manifest = load_manifest() if manifest is None else manifest
    return {entry["name"]: entry for entry in manifest["variables"]}


def names_for_surface(
    surface: str, manifest: dict[str, Any] | None = None
) -> list[str]:
    if surface not in ALLOWED_SURFACES:
        raise ManifestError(f"unknown surface: {surface}")
    manifest = load_manifest() if manifest is None else manifest
    return sorted(
        entry["name"] for entry in manifest["variables"] if surface in entry["surfaces"]
    )


def names_for_class(
    class_name: str, manifest: dict[str, Any] | None = None
) -> list[str]:
    if class_name not in ALLOWED_CLASSES:
        raise ManifestError(f"unknown class: {class_name}")
    manifest = load_manifest() if manifest is None else manifest
    return sorted(
        entry["name"] for entry in manifest["variables"] if entry["class"] == class_name
    )


def remote_required_names(manifest: dict[str, Any] | None = None) -> list[str]:
    manifest = load_manifest() if manifest is None else manifest
    return sorted(
        entry["name"] for entry in manifest["variables"] if entry["remote_required"]
    )


def known_names(manifest: dict[str, Any] | None = None) -> set[str]:
    return set(entries_by_name(manifest))


def classify_name(name: str, manifest: dict[str, Any] | None = None) -> str | None:
    manifest = load_manifest() if manifest is None else manifest
    entry = entries_by_name(manifest).get(name)
    if entry:
        return entry["class"]
    for rule in manifest.get("prefix_rules", []):
        if name.startswith(rule["prefix"]):
            return rule["class"]
    return None


def supports_empty(name: str, manifest: dict[str, Any] | None = None) -> bool:
    manifest = load_manifest() if manifest is None else manifest
    entry = entries_by_name(manifest).get(name)
    if entry:
        return bool(entry["supports_empty"])
    for rule in manifest.get("prefix_rules", []):
        if name.startswith(rule["prefix"]):
            return bool(rule["supports_empty"])
    return False


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    subparsers = parser.add_subparsers(dest="command", required=True)

    names = subparsers.add_parser("names", help="print names, one per line")
    selector = names.add_mutually_exclusive_group(required=True)
    selector.add_argument("--surface")
    selector.add_argument("--class", dest="class_name")
    selector.add_argument("--remote-required", action="store_true")

    subparsers.add_parser("validate", help="validate the manifest")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        manifest = load_manifest(Path(args.manifest))
        if args.command == "validate":
            print(f"manifest OK: {args.manifest}")
            return 0
        if args.surface:
            selected = names_for_surface(args.surface, manifest)
        elif args.class_name:
            selected = names_for_class(args.class_name, manifest)
        else:
            selected = remote_required_names(manifest)
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for name in selected:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Content-addressed local action artifact registry.

The v1 registry is deliberately local and deterministic: publishing an action
builds a portable artifact, validates it, attaches an eval receipt, and stores
the exact public object by content digest.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from filelock import FileLock

from frisket.authoring.action_metadata import canonical_action_kind
from frisket.actions.core import ModelRows
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import discover_references
from frisket.ops.builtin import get_recipe

ARTIFACT_SCHEMA_VERSION = "frisket.action_artifact.v1"
RECEIPT_SCHEMA_VERSION = "frisket.action_eval_receipt.v1"
REGISTRY_SCHEMA_VERSION = "frisket.action_registry.v1"

LOCAL_SPEC_KEYS = {"sheet_id", "row_ids", "confirmed", "overwrite"}
UNSAFE_ACTION_KINDS = {"map.python", "map.mcp_extract", "research.answer"}
ARTIFACT_TOP_LEVEL_KEYS = {
    "schema_version",
    "artifact_id",
    "digest",
    "name",
    "description",
    "action",
    "spec",
    "publisher",
    "published_at",
    "receipts",
}
RECEIPT_TOP_LEVEL_KEYS = {
    "schema_version",
    "receipt_id",
    "artifact_digest",
    "action",
    "dataset",
    "checks",
    "created_at",
}
CORE_RECEIPT_CHECKS = {
    "action.registered",
    "spec.portable",
    "spec.outputs",
    "safety.policy",
}
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
REMOVED_ACTION_KEYS = frozenset(
    {
        "sheetId",
        "targetColumnId",
        "targetColumnName",
        "inputColumns",
        "inputTemplate",
        "outputFields",
        "targetLanguage",
        "includeConfidence",
        "includeJustification",
        "includeLatLon",
        "rowIds",
        "previewRows",
        "debug",
        "recipe",
        "recipeKind",
        "recipeName",
        "recipe_version",
        "blocked_recipes",
        "unsafe_recipe",
        "fields",
    }
)


class ActionRegistryError(ValueError):
    """Raised when an action artifact cannot be published or imported."""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ActionRegistryError("artifact payload must be JSON serializable") from e


def _digest_payload(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _digest(value: Any) -> str:
    return f"sha256:{_digest_payload(value)}"


def _json_clone(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _artifact_string(value: Any, *, field: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ActionRegistryError(f"{field} must be a string")
    return value.strip()


def _normalize_dataset(dataset: dict[str, Any] | None, spec: dict[str, Any]) -> dict:
    if dataset is None:
        dataset = {}
    if not isinstance(dataset, dict):
        raise ActionRegistryError("dataset must be an object")
    normalized = {
        "name": str(dataset.get("name") or "local-validation").strip(),
        "version": str(dataset.get("version") or "1").strip(),
        "row_count": dataset.get("row_count", 0),
        "metadata": dataset.get("metadata") or {},
    }
    basis = {
        **normalized,
        "spec_digest": _digest(spec),
    }
    normalized["fingerprint"] = dataset.get("fingerprint") or _digest(basis)
    return _json_clone(normalized)


def _normalize_checks(checks: list[dict[str, Any]] | None) -> list[dict]:
    if checks is None:
        checks = []
    if not isinstance(checks, list) or any(
        not isinstance(check, dict) for check in checks
    ):
        raise ActionRegistryError("checks must be a list")
    return _json_clone(checks)


def _validate_source_columns(spec: dict[str, Any]) -> None:
    columns = spec.get("input_columns")
    if columns is None:
        return
    if not isinstance(columns, list) or any(
        not isinstance(name, str) or not name.strip() for name in columns
    ):
        raise ActionRegistryError("spec.input_columns must be a list of strings")


def _portable_spec(
    spec: dict[str, Any], *, strip_local_keys: bool
) -> tuple[dict[str, Any], list[str], str, list[dict]]:
    if not isinstance(spec, dict):
        raise ActionRegistryError("spec must be an object")
    forbidden = sorted(REMOVED_ACTION_KEYS.intersection(spec))
    if forbidden:
        raise ActionRegistryError(
            "artifact spec uses removed keys: " + ", ".join(forbidden)
        )
    action_kind = spec.get("action_kind")
    if not isinstance(action_kind, str) or not action_kind.strip():
        raise ActionRegistryError("spec.action_kind is required")
    action_kind = action_kind.strip()
    if "." not in action_kind or canonical_action_kind(action_kind) != action_kind:
        raise ActionRegistryError(f"action_kind '{action_kind}' is not canonical")
    if action_kind in UNSAFE_ACTION_KINDS:
        raise ActionRegistryError(
            f"action '{action_kind}' is not importable through the shared registry"
        )

    local_keys = sorted(k for k in spec if k in LOCAL_SPEC_KEYS)
    if local_keys and not strip_local_keys:
        raise ActionRegistryError(
            "artifact spec contains project-local keys: " + ", ".join(local_keys)
        )
    clean = {k: v for k, v in spec.items() if k not in LOCAL_SPEC_KEYS}
    clean["action_kind"] = action_kind
    clean = _json_clone(clean)

    try:
        registered = ACTION_REGISTRY.get(action_kind)
    except KeyError:
        registered = None
    try:
        if registered is not None:
            unknown = clean.keys() - {
                "action_kind",
                "action_name",
                "params",
                "output_names",
            }
            if unknown:
                raise ValueError(
                    "unknown typed artifact fields: " + ", ".join(sorted(unknown))
                )
            terminal = registered.definition.run
            output_names = clean.get("output_names", {})
            if not isinstance(output_names, dict):
                raise ValueError("output_names must be an object")
            params, fields = registered.bind_params(
                params=clean.get("params", {}), output_names=output_names
            )
            output_fields = [
                {
                    "name": field.materialized_name(output_names),
                    "column_type": field.column_type,
                }
                for field in (fields or ())
                if not field.hidden
            ]
            source_columns = [
                reference.column for reference in discover_references(params)
            ]
            # Typed action programs share the current host revision; there is
            # no separately registered recipe version for a builtin action.
            version = "1"
            llm = (
                terminal.uses_model(params)
                if isinstance(terminal, ModelRows)
                else "model:complete"
                in registered.for_execution(params).catalog_entry()[
                    "required_capabilities"
                ]
            )
        else:
            implementation = get_recipe(action_kind)
            _validate_source_columns(clean)
            runtime_spec = dict(clean)
            if isinstance(runtime_spec.get("output_fields"), list):
                runtime_spec["fields"] = runtime_spec["output_fields"]
            output_fields = implementation.output_fields(runtime_spec)
            source_columns = implementation.source_columns(runtime_spec)
            version = implementation.version
            llm = bool(implementation.llm)
    except Exception as e:
        raise ActionRegistryError(f"invalid action spec: {e}") from e
    if not output_fields:
        raise ActionRegistryError("spec declares no output fields")
    field_names = []
    for idx, field in enumerate(output_fields):
        name = field.get("name") if isinstance(field, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ActionRegistryError(f"output field {idx} is missing a name")
        field_names.append(
            {
                "name": name,
                "column_type": str(field.get("column_type") or "text"),
            }
        )

    checks = [
        {
            "name": "action.registered",
            "status": "passed",
            "evidence": {
                "action_kind": action_kind,
                "action_version": version,
                "llm": llm,
            },
        },
        {
            "name": "spec.portable",
            "status": "passed",
            "evidence": {
                "removed_project_keys": local_keys,
                "spec_digest": _digest(clean),
            },
        },
        {
            "name": "spec.outputs",
            "status": "passed",
            "evidence": {"output_fields": field_names},
        },
        {
            "name": "spec.sources",
            "status": "passed",
            "evidence": {"source_columns": [str(c) for c in source_columns]},
        },
        {
            "name": "safety.policy",
            "status": "passed",
            "evidence": {
                "blocked_actions": sorted(UNSAFE_ACTION_KINDS),
                "unsafe_action": False,
            },
        },
    ]
    return clean, local_keys, version, checks


def _artifact_identity(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "name": artifact["name"],
        "description": artifact.get("description", ""),
        "action": artifact["action"],
        "spec": artifact["spec"],
        "publisher": artifact.get("publisher", {}),
    }


def _receipt_identity(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "artifact_digest": receipt["artifact_digest"],
        "action": receipt["action"],
        "dataset": receipt["dataset"],
        "checks": receipt["checks"],
    }


def build_action_artifact(
    *,
    name: str,
    spec: dict[str, Any],
    description: str = "",
    publisher: dict[str, Any] | None = None,
    dataset: dict[str, Any] | None = None,
    checks: list[dict[str, Any]] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Validate and package a portable action spec with one eval receipt."""
    timestamp = now or _utc_now()
    portable, _removed, version, validation_checks = _portable_spec(
        spec, strip_local_keys=True
    )
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "name": name.strip(),
        "description": description.strip(),
        "action": {
            "kind": portable["action_kind"],
            "version": version,
        },
        "spec": portable,
        "publisher": _json_clone(publisher or {}),
    }
    artifact_id = _digest_payload(_artifact_identity(artifact))
    digest = f"sha256:{artifact_id}"
    dataset_payload = _normalize_dataset(dataset, portable)
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "artifact_digest": digest,
        "action": {
            "kind": portable["action_kind"],
            "version": version,
        },
        "dataset": dataset_payload,
        "checks": validation_checks + _normalize_checks(checks),
    }
    receipt["receipt_id"] = _digest(_receipt_identity(receipt))
    receipt["created_at"] = timestamp
    artifact.update(
        {
            "artifact_id": artifact_id,
            "digest": digest,
            "published_at": timestamp,
            "receipts": [receipt],
        }
    )
    return validate_action_artifact(artifact)


def validate_action_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized canonical action artifact or raise."""
    if not isinstance(artifact, dict):
        raise ActionRegistryError("artifact must be an object")
    extra = sorted(set(artifact) - ARTIFACT_TOP_LEVEL_KEYS)
    if extra:
        raise ActionRegistryError("artifact has unknown keys: " + ", ".join(extra))
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ActionRegistryError("unsupported action artifact schema_version")

    normalized = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "name": _artifact_string(artifact.get("name"), field="name"),
        "description": _artifact_string(
            artifact.get("description"), field="description"
        ),
        "action": artifact.get("action"),
        "spec": artifact.get("spec"),
        "publisher": _json_clone(artifact.get("publisher") or {}),
        "published_at": _artifact_string(
            artifact.get("published_at"), field="published_at", default=_utc_now()
        ),
    }
    portable, _removed, version, _validation_checks = _portable_spec(
        normalized["spec"], strip_local_keys=False
    )
    normalized["spec"] = portable
    expected_action = {
        "kind": portable["action_kind"],
        "version": version,
    }
    if normalized["action"] != expected_action:
        raise ActionRegistryError("artifact action metadata does not match spec")

    artifact_id = _digest_payload(_artifact_identity(normalized))
    if artifact.get("artifact_id") != artifact_id:
        raise ActionRegistryError("artifact_id does not match artifact content")
    digest = f"sha256:{artifact_id}"
    if artifact.get("digest") != digest:
        raise ActionRegistryError("artifact digest does not match artifact content")

    receipts_raw = artifact.get("receipts")
    if not isinstance(receipts_raw, list) or not receipts_raw:
        raise ActionRegistryError("artifact must include at least one eval receipt")
    receipts: list[dict] = []
    core_passed = False
    for idx, receipt in enumerate(receipts_raw):
        if not isinstance(receipt, dict):
            raise ActionRegistryError(f"receipts[{idx}] must be an object")
        extra = sorted(set(receipt) - RECEIPT_TOP_LEVEL_KEYS)
        if extra:
            raise ActionRegistryError(
                f"receipts[{idx}] has unknown keys: " + ", ".join(extra)
            )
        if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
            raise ActionRegistryError(f"receipts[{idx}] has unsupported schema_version")
        if receipt.get("artifact_digest") != digest:
            raise ActionRegistryError(f"receipts[{idx}] artifact_digest mismatch")
        if receipt.get("action") != expected_action:
            raise ActionRegistryError(f"receipts[{idx}] action metadata mismatch")
        dataset = _normalize_dataset(receipt.get("dataset"), portable)
        checks = _normalize_checks(receipt.get("checks"))
        body = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "artifact_digest": digest,
            "action": expected_action,
            "dataset": dataset,
            "checks": checks,
        }
        receipt_id = _digest(_receipt_identity(body))
        if receipt.get("receipt_id") != receipt_id:
            raise ActionRegistryError(f"receipts[{idx}] receipt_id mismatch")
        created_at = _artifact_string(
            receipt.get("created_at"), field=f"receipts[{idx}].created_at"
        )
        receipts.append({**body, "receipt_id": receipt_id, "created_at": created_at})
        passed = {
            check.get("name")
            for check in checks
            if isinstance(check.get("name"), str)
            and check.get("status") == "passed"
            and check.get("name") in CORE_RECEIPT_CHECKS
        }
        core_passed = core_passed or CORE_RECEIPT_CHECKS <= passed
    if not core_passed:
        raise ActionRegistryError("artifact receipts lack core validation evidence")

    return {
        **normalized,
        "artifact_id": artifact_id,
        "digest": digest,
        "receipts": sorted(receipts, key=lambda r: r["receipt_id"]),
    }


def action_registry_entry_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact["artifact_id"],
        "digest": artifact["digest"],
        "name": artifact["name"],
        "description": artifact.get("description", ""),
        "action": artifact["action"],
        "publisher": artifact.get("publisher", {}),
        "published_at": artifact.get("published_at"),
        "receipt_count": len(artifact.get("receipts") or []),
    }


class ActionRegistryStore:
    """Filesystem-backed local registry under a workspace root."""

    def __init__(self, workspace_root: Path):
        self.root = workspace_root / "action_registry"
        self.artifacts_dir = self.root / "artifacts"
        self.lock_path = self.root / ".lock"

    def _artifact_path(self, artifact_id: str) -> Path:
        if not isinstance(artifact_id, str) or not _HEX64.match(artifact_id):
            raise ActionRegistryError("artifact_id must be 64 lowercase hex chars")
        return self.artifacts_dir / f"{artifact_id}.json"

    def _read_artifact_unlocked(self, artifact_id: str) -> dict[str, Any] | None:
        path = self._artifact_path(artifact_id)
        if not path.exists():
            return None
        try:
            return validate_action_artifact(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            raise ActionRegistryError(
                f"could not read registry artifact {artifact_id}"
            ) from e

    def publish(self, artifact: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_action_artifact(artifact)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.lock_path), timeout=-1):
            existing = self._read_artifact_unlocked(normalized["artifact_id"])
            if existing is not None:
                receipts = {
                    receipt["receipt_id"]: receipt
                    for receipt in existing.get("receipts", [])
                }
                for receipt in normalized.get("receipts", []):
                    # Identical evaluation content has one receipt identity.
                    # Republishing it must retain its original evaluation time.
                    receipts.setdefault(receipt["receipt_id"], receipt)
                normalized["published_at"] = existing.get(
                    "published_at", normalized["published_at"]
                )
                normalized["receipts"] = [receipts[key] for key in sorted(receipts)]
                normalized = validate_action_artifact(normalized)
            path = self._artifact_path(normalized["artifact_id"])
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
            tmp.replace(path)
            return normalized

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        return self._read_artifact_unlocked(artifact_id)

    def list(self) -> dict[str, Any]:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifacts = []
        for path in sorted(self.artifacts_dir.glob("*.json")):
            artifact_id = path.stem
            if not _HEX64.match(artifact_id):
                continue
            artifact = self._read_artifact_unlocked(artifact_id)
            if artifact is not None:
                artifacts.append(action_registry_entry_summary(artifact))
        return {"schema_version": REGISTRY_SCHEMA_VERSION, "artifacts": artifacts}

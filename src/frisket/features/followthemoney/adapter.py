"""Thin wrappers around the official FollowTheMoney Python SDK."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from frisket.features.followthemoney._sdk_import import require_followthemoney_sdk

with require_followthemoney_sdk():
    from followthemoney import model
    from followthemoney.exc import InvalidData

FOLLOWTHEMONEY_ENTITY_SCHEMA_VERSION = "frisket.followthemoney.entity_stream.v1"


class FollowTheMoneyAdapterError(Exception):
    """SDK-backed FtM adapter failure with a stable diagnostic code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def validate_entity(entity: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an FtM entity dict through the SDK/model."""

    schema_name = entity.get("schema")
    if not isinstance(schema_name, str) or not schema_name:
        return _invalid_entity_result(
            entity,
            code="missing_schema",
            message="FtM entity is missing schema",
        )
    schema = model.get(schema_name)
    if schema is None:
        return _invalid_entity_result(
            entity,
            code="unsupported_schema",
            message=f"unsupported FtM schema: {schema_name}",
        )
    entity_id = entity.get("id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        return _invalid_entity_result(
            entity,
            code="missing_id",
            message="FtM entity is missing id",
        )
    properties = entity.get("properties", {})
    if properties is not None and not isinstance(properties, Mapping):
        return _invalid_entity_result(
            entity,
            code="invalid_entity",
            message="FtM entity properties must be an object",
        )
    try:
        proxy = model.get_proxy(dict(entity))
        normalized = proxy.to_dict()
        schema.validate(normalized)
    except InvalidData as exc:
        return _invalid_entity_result(
            entity,
            code="invalid_entity",
            message=str(exc),
            errors=getattr(exc, "errors", None),
        )
    except Exception as exc:  # noqa: BLE001 - normalize SDK failures to diagnostics
        return _invalid_entity_result(
            entity,
            code="invalid_entity",
            message=str(exc),
        )

    missing_required = [
        prop
        for prop in schema.required or []
        if not normalized.get("properties", {}).get(prop)
    ]
    if missing_required:
        return _invalid_entity_result(
            normalized,
            code="missing_required_property",
            message="FtM entity is missing required properties",
            errors={"properties": {prop: "Required" for prop in missing_required}},
        )

    return {
        "valid": True,
        "entity": normalized,
        "diagnostics": [],
    }


def serialize_entities_jsonl(entities: Iterable[Mapping[str, Any]]) -> str:
    """Validate and serialize FtM entities as deterministic JSONL."""

    lines: list[str] = []
    for index, entity in enumerate(entities, start=1):
        validation = validate_entity(entity)
        if not validation["valid"]:
            raise FollowTheMoneyAdapterError(
                "invalid_entity",
                f"cannot serialize invalid FtM entity at line {index}",
                details={"index": index, "diagnostics": validation["diagnostics"]},
            )
        lines.append(
            json.dumps(
                validation["entity"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return "\n".join(lines) + ("\n" if lines else "")


def parse_entities_jsonl(payload: str | bytes) -> dict[str, Any]:
    """Parse JSONL entity streams through the SDK/model with diagnostics."""

    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    entities: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            diagnostics.append(
                _diagnostic(
                    "invalid_json",
                    "error",
                    f"line {line_number} is not valid JSON: {exc.msg}",
                    line_number=line_number,
                )
            )
            continue
        if not isinstance(raw, dict):
            diagnostics.append(
                _diagnostic(
                    "invalid_entity",
                    "error",
                    f"line {line_number} is not a JSON object",
                    line_number=line_number,
                )
            )
            continue
        validation = validate_entity(raw)
        if validation["valid"]:
            entities.append(validation["entity"])
            continue
        for item in validation["diagnostics"]:
            diagnostics.append({**item, "line_number": line_number})
    return {
        "schema_version": FOLLOWTHEMONEY_ENTITY_SCHEMA_VERSION,
        "entities": entities,
        "diagnostics": diagnostics,
        "entity_count": len(entities),
        "diagnostic_count": len(diagnostics),
    }


def _invalid_entity_result(
    entity: Mapping[str, Any],
    *,
    code: str,
    message: str,
    errors: Any | None = None,
) -> dict[str, Any]:
    diagnostic = _diagnostic(
        code,
        "error",
        message,
        schema=entity.get("schema"),
        entity_id=entity.get("id"),
    )
    if errors is not None:
        diagnostic["errors"] = errors
    return {"valid": False, "entity": dict(entity), "diagnostics": [diagnostic]}


def _diagnostic(
    code: str,
    severity: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    diagnostic = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    diagnostic.update({key: value for key, value in extra.items() if value is not None})
    return diagnostic

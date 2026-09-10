"""Map resolved investigative rowsets into FollowTheMoney entities."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from frisket.features.followthemoney._sdk_import import require_followthemoney_sdk

with require_followthemoney_sdk():
    from followthemoney import model
    from followthemoney.exc import InvalidData

from frisket.features.followthemoney.adapter import validate_entity

FOLLOWTHEMONEY_MAPPING_SCHEMA_VERSION = "frisket.followthemoney.mapping.v1"

_MISSING = object()
_MAX_RAW_VALUE_LABEL_LENGTH = 240


def deterministic_row_ref_id(
    row_ref: Mapping[str, Any],
    *,
    project_id: str | None = None,
    prefix: str = "frisket",
) -> str:
    """Build a deterministic local FtM id from a Frisket row ref."""

    sheet_id = row_ref.get("sheet_id")
    row_id = row_ref.get("row_id")
    project_part = _id_part(project_id or "local")
    return (
        f"{_id_part(prefix)}:project:{project_part}:"
        f"sheet:{_id_part(sheet_id)}:row:{_id_part(row_id)}"
    )


def normalize_property_values(
    schema_name: str, property_name: str, value: Any
) -> dict[str, Any]:
    """Normalize a Frisket typed value into FtM string property values."""

    schema = model.get(schema_name)
    if schema is None:
        return {
            "values": [],
            "invalid_values": [_raw_value_label(value)],
            "diagnostic": f"unsupported FtM schema: {schema_name}",
        }
    prop = schema.get(property_name)
    if prop is None:
        return {
            "values": [],
            "invalid_values": [_raw_value_label(value)],
            "diagnostic": (
                f"unsupported FtM property for schema {schema_name}: {property_name}"
            ),
        }

    values: list[str] = []
    invalid_values: list[str] = []
    for raw in _value_items(value):
        temp = model.make_entity(schema)
        temp.id = "_frisket_value_probe"
        try:
            temp.add(prop, raw)
        except InvalidData:
            invalid_values.append(_raw_value_label(raw))
            continue
        normalized = [str(item) for item in temp.get(prop.name)]
        if normalized:
            for item in normalized:
                if item not in values:
                    values.append(item)
        elif _has_non_empty_value(raw):
            invalid_values.append(_raw_value_label(raw))
    return {
        "values": values,
        "invalid_values": invalid_values,
        "diagnostic": None,
    }


def map_rowset_to_entities(
    rowset_payload: Mapping[str, Any],
    mapping_spec: Mapping[str, Any],
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Map resolved investigative rowset records into FtM entity dictionaries."""

    schema_name = mapping_spec.get("schema")
    diagnostics: list[dict[str, Any]] = []
    if not isinstance(schema_name, str) or not schema_name:
        return _empty_result(
            rowset_payload,
            diagnostics=[
                _diagnostic(
                    "unsupported_schema",
                    "error",
                    "mapping schema must be a non-empty FtM schema name",
                    schema=schema_name,
                )
            ],
        )

    schema = model.get(schema_name)
    if schema is None:
        return _empty_result(
            rowset_payload,
            diagnostics=[
                _diagnostic(
                    "unsupported_schema",
                    "error",
                    f"unsupported FtM schema: {schema_name}",
                    schema=schema_name,
                )
            ],
        )

    property_mappings = _resolved_property_mappings(mapping_spec, schema, diagnostics)
    id_policy = _id_policy(mapping_spec)
    entities: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    skipped_rows = 0
    rows_with_source_refs = 0
    rows_with_evidence_refs = 0
    records = list(rowset_payload.get("records") or [])
    seen_entity_ids: set[str] = set()

    for record in records:
        if not isinstance(record, Mapping):
            continue
        row_ref = record.get("row_ref")
        row_diagnostics: list[dict[str, Any]] = []
        entity_id = _entity_id_for_record(
            record, id_policy, project_id=project_id, diagnostics=row_diagnostics
        )
        if not entity_id:
            _skip_row(
                row_diagnostics,
                "skipped row because no deterministic FtM id could be resolved",
                row_ref=row_ref,
            )
            skipped_rows += 1
            diagnostics.extend(row_diagnostics)
            continue

        if entity_id in seen_entity_ids:
            row_diagnostics.append(
                _diagnostic(
                    "duplicate_id",
                    "error",
                    f"duplicate FtM entity id: {entity_id}",
                    row_ref=row_ref,
                    entity_id=entity_id,
                )
            )
            _skip_row(
                row_diagnostics,
                "skipped row because FtM entity id was already emitted",
                row_ref=row_ref,
            )
            skipped_rows += 1
            diagnostics.extend(row_diagnostics)
            continue
        seen_entity_ids.add(entity_id)

        entity = model.make_entity(schema)
        entity.id = entity_id
        source_refs: list[dict[str, Any]] = []
        evidence_refs: list[dict[str, Any]] = []
        for prop_name, value_spec in property_mappings.items():
            value = _value_from_spec(record, value_spec)
            if value is _MISSING:
                continue
            normalized = normalize_property_values(schema_name, prop_name, value)
            if normalized["diagnostic"]:
                diagnostics.append(
                    _diagnostic(
                        "unsupported_property",
                        "warning",
                        normalized["diagnostic"],
                        schema=schema_name,
                        property=prop_name,
                    )
                )
                continue
            if normalized["invalid_values"]:
                diagnostics.append(
                    _diagnostic(
                        "invalid_value",
                        "warning",
                        "FtM SDK rejected one or more mapped values",
                        schema=schema_name,
                        property=prop_name,
                        row_ref=row_ref,
                        values=normalized["invalid_values"],
                    )
                )
            if normalized["values"]:
                entity.add(prop_name, normalized["values"], cleaned=True)
                source_ref = _source_ref_for_property(record, prop_name, value_spec)
                if source_ref is not None:
                    source_refs.append(source_ref)
                    evidence_refs.extend(source_ref.get("evidence_refs") or [])

        missing_required = _missing_required_properties(entity.to_dict(), schema)
        for prop_name in missing_required:
            code = _missing_ref_code(schema, prop_name)
            row_diagnostics.append(
                _diagnostic(
                    code,
                    "error",
                    f"missing required FtM property {prop_name}",
                    schema=schema_name,
                    property=prop_name,
                    row_ref=row_ref,
                )
            )
        if missing_required:
            _skip_row(
                row_diagnostics,
                "skipped row because required FtM references/properties are missing",
                row_ref=row_ref,
            )
            skipped_rows += 1
            diagnostics.extend(row_diagnostics)
            continue

        entity_dict = entity.to_dict()
        validation = validate_entity(entity_dict)
        if not validation["valid"]:
            diagnostics.extend(
                {**item, "row_ref": row_ref} for item in validation["diagnostics"]
            )
            _skip_row(
                diagnostics,
                "skipped row because FtM entity validation failed",
                row_ref=row_ref,
            )
            skipped_rows += 1
            continue

        if source_refs:
            rows_with_source_refs += 1
        if evidence_refs:
            rows_with_evidence_refs += 1
        entity_dict = validation["entity"]
        entities.append(entity_dict)
        rows.append(
            {
                "entity_id": entity_dict["id"],
                "schema": entity_dict["schema"],
                "row_ref": row_ref,
                "source_row_ref": record.get("source_row_ref"),
                "lineage": record.get("lineage"),
                "source_refs": source_refs,
                "evidence_refs": evidence_refs,
                "raw_values": dict(record.get("values") or {}),
            }
        )

    coverage = {
        "row_count": len(records),
        "entity_count": len(entities),
        "skipped_count": skipped_rows,
        "rows_with_source_refs": rows_with_source_refs,
        "rows_with_evidence_refs": rows_with_evidence_refs,
        "source_ref_coverage": _ratio(rows_with_source_refs, len(records)),
        "evidence_ref_coverage": _ratio(rows_with_evidence_refs, len(records)),
    }
    diagnostics.append(
        _diagnostic(
            "source_ref_coverage",
            "info",
            "mapped entities with source refs",
            **coverage,
        )
    )
    diagnostics.append(
        _diagnostic(
            "evidence_ref_coverage",
            "info",
            "mapped entities with evidence refs",
            **coverage,
        )
    )
    return {
        "schema_version": FOLLOWTHEMONEY_MAPPING_SCHEMA_VERSION,
        "schema": schema_name,
        "rowset": rowset_payload.get("rowset"),
        "entities": entities,
        "rows": rows,
        "diagnostics": diagnostics,
        "coverage": coverage,
        "entity_count": len(entities),
        "skipped_count": skipped_rows,
        "diagnostic_count": len(diagnostics),
    }


def _resolved_property_mappings(
    mapping_spec: Mapping[str, Any], schema: Any, diagnostics: list[dict[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    properties: dict[str, Mapping[str, Any]] = {}
    for prop_name, value_spec in (mapping_spec.get("properties") or {}).items():
        if not isinstance(prop_name, str) or not isinstance(value_spec, Mapping):
            continue
        if schema.get(prop_name) is None:
            diagnostics.append(
                _diagnostic(
                    "unsupported_property",
                    "warning",
                    f"unsupported FtM property for schema {schema.name}: {prop_name}",
                    schema=schema.name,
                    property=prop_name,
                )
            )
            continue
        properties[prop_name] = value_spec

    source_prop = getattr(schema, "source_prop", None)
    target_prop = getattr(schema, "target_prop", None)
    if source_prop is not None and isinstance(
        mapping_spec.get("source_entity"), Mapping
    ):
        properties.setdefault(source_prop.name, mapping_spec["source_entity"])
    if target_prop is not None and isinstance(
        mapping_spec.get("target_entity"), Mapping
    ):
        properties.setdefault(target_prop.name, mapping_spec["target_entity"])
    return properties


def _id_policy(mapping_spec: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = mapping_spec.get("id_policy")
    if isinstance(policy, Mapping):
        return policy
    return {"kind": "row_ref"}


def _entity_id_for_record(
    record: Mapping[str, Any],
    id_policy: Mapping[str, Any],
    *,
    project_id: str | None,
    diagnostics: list[dict[str, Any]],
) -> str | None:
    kind = id_policy.get("kind", "row_ref")
    if kind == "row_ref":
        row_ref = record.get("row_ref")
        if not isinstance(row_ref, Mapping):
            diagnostics.append(
                _diagnostic(
                    "missing_id",
                    "error",
                    "row_ref id policy requires row_ref metadata",
                    row_ref=row_ref,
                )
            )
            return None
        return deterministic_row_ref_id(
            row_ref,
            project_id=project_id,
            prefix=str(id_policy.get("prefix") or "frisket"),
        )
    if kind == "column":
        column = id_policy.get("column")
        if not isinstance(column, str) or not column:
            diagnostics.append(
                _diagnostic(
                    "missing_id",
                    "error",
                    "column id policy requires id_policy.column",
                    row_ref=record.get("row_ref"),
                )
            )
            return None
        value = (record.get("values") or {}).get(column)
        values = _value_items(value)
        if not values:
            diagnostics.append(
                _diagnostic(
                    "missing_id",
                    "error",
                    f"id column {column} is empty",
                    row_ref=record.get("row_ref"),
                    column=column,
                )
            )
            return None
        id_value = _json_scalar(values[0])
        if isinstance(id_value, str):
            return id_value.strip()
        return str(id_value)
    diagnostics.append(
        _diagnostic(
            "missing_id",
            "error",
            f"unsupported id policy: {kind}",
            row_ref=record.get("row_ref"),
        )
    )
    return None


def _value_from_spec(record: Mapping[str, Any], value_spec: Mapping[str, Any]) -> Any:
    values = record.get("values") or {}
    if "literal" in value_spec:
        return value_spec["literal"]
    column = value_spec.get("column")
    if isinstance(column, str):
        return values.get(column, _MISSING)
    columns = value_spec.get("columns")
    if isinstance(columns, list):
        separator = str(value_spec.get("separator", " "))
        parts = [
            str(_json_scalar(values[column]))
            for column in columns
            if isinstance(column, str) and _has_non_empty_value(values.get(column))
        ]
        return separator.join(parts) if parts else _MISSING
    return _MISSING


def _source_ref_for_property(
    record: Mapping[str, Any], prop_name: str, value_spec: Mapping[str, Any]
) -> dict[str, Any] | None:
    column = value_spec.get("column")
    if isinstance(column, str):
        for cell in record.get("cells") or []:
            if cell.get("column_name") != column:
                continue
            return {
                "property": prop_name,
                "column_name": column,
                "source_cell_ref": cell.get("source_cell_ref"),
                "value_ref": cell.get("value_ref"),
                "evidence_refs": list(cell.get("evidence_refs") or []),
            }
        return None
    columns = value_spec.get("columns")
    if isinstance(columns, list):
        requested = {item for item in columns if isinstance(item, str)}
        matched_cells = [
            cell
            for cell in record.get("cells") or []
            if cell.get("column_name") in requested
        ]
        if not matched_cells:
            return None
        evidence_refs: list[dict[str, Any]] = []
        for cell in matched_cells:
            evidence_refs.extend(cell.get("evidence_refs") or [])
        return {
            "property": prop_name,
            "column_names": [cell.get("column_name") for cell in matched_cells],
            "source_cell_refs": [cell.get("source_cell_ref") for cell in matched_cells],
            "value_refs": [cell.get("value_ref") for cell in matched_cells],
            "evidence_refs": evidence_refs,
        }
    return None


def _missing_required_properties(entity: Mapping[str, Any], schema: Any) -> list[str]:
    properties = entity.get("properties") or {}
    return [prop for prop in schema.required or [] if not properties.get(prop)]


def _missing_ref_code(schema: Any, prop_name: str) -> str:
    source_prop = getattr(schema, "source_prop", None)
    target_prop = getattr(schema, "target_prop", None)
    if source_prop is not None and prop_name == source_prop.name:
        return "missing_source_ref"
    if target_prop is not None and prop_name == target_prop.name:
        return "missing_target_ref"
    return "missing_required_property"


def _skip_row(
    diagnostics: list[dict[str, Any]], message: str, *, row_ref: Any | None
) -> None:
    diagnostics.append(_diagnostic("skipped_row", "warning", message, row_ref=row_ref))


def _value_items(value: Any) -> list[Any]:
    if not _has_non_empty_value(value):
        return []
    if isinstance(value, (list, tuple, set)):
        items: list[Any] = []
        iterable = sorted(value, key=str) if isinstance(value, set) else value
        for item in iterable:
            if _has_non_empty_value(item):
                items.append(_json_scalar(item))
        return items
    return [_json_scalar(value)]


def _json_scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, (list, tuple, set)):
        items = sorted(value, key=str) if isinstance(value, set) else list(value)
        return json.dumps(items, sort_keys=True, separators=(",", ":"))
    return value


def _has_non_empty_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Iterable) and not isinstance(
        value, (str, bytes, bytearray, Mapping)
    ):
        return any(_has_non_empty_value(item) for item in value)
    return True


def _raw_value_label(value: Any) -> str:
    if isinstance(value, (Mapping, list, tuple, set)):
        label = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    else:
        label = str(value)
    if len(label) <= _MAX_RAW_VALUE_LABEL_LENGTH:
        return label
    return label[: _MAX_RAW_VALUE_LABEL_LENGTH - 3] + "..."


def _id_part(value: Any) -> str:
    text = str(value)
    return quote(text, safe="")


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _empty_result(
    rowset_payload: Mapping[str, Any], *, diagnostics: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": FOLLOWTHEMONEY_MAPPING_SCHEMA_VERSION,
        "schema": None,
        "rowset": rowset_payload.get("rowset"),
        "entities": [],
        "rows": [],
        "diagnostics": diagnostics,
        "coverage": {
            "row_count": len(rowset_payload.get("records") or []),
            "entity_count": 0,
            "skipped_count": 0,
            "rows_with_source_refs": 0,
            "rows_with_evidence_refs": 0,
            "source_ref_coverage": 0.0,
            "evidence_ref_coverage": 0.0,
        },
        "entity_count": 0,
        "skipped_count": 0,
        "diagnostic_count": len(diagnostics),
    }


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
